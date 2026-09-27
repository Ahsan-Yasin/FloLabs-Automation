"""/api/v1/auth: accounts and browser sessions (plan.MD §5.1, §8)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import TOKEN_VERIFY, User
from db.session import get_db
from services import notifications, sessions, users
from services.errors import ServiceError, rate_limited
from services.tokens import make_access_token

from ..auth import (
    REFRESH_COOKIE,
    Principal,
    clear_session_cookies,
    client_ip,
    require_user,
    set_access_cookie,
    set_session_cookies,
    user_agent,
)
from ..errors import service_error_response
from ..ratelimit import limiter

router = APIRouter(prefix="/auth", tags=["Accounts"])

DB = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[Principal, Depends(require_user)]


# ------------------------------------------------------------------ models
class SignupIn(BaseModel):
    email: str = Field(max_length=320, examples=["ada@example.com"])
    password: str = Field(max_length=256, description="10-128 characters")
    name: str = Field(default="", max_length=120)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=200,
                                      description="Browsers send the hc_refresh cookie instead")


class TokenIn(BaseModel):
    token: str = Field(max_length=200)


class EmailIn(BaseModel):
    email: str = Field(max_length=320)


class ResetIn(BaseModel):
    token: str = Field(max_length=200)
    new_password: str = Field(max_length=256)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class ProfileIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    notify_on_done: bool | None = None


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    is_admin: bool
    email_verified: bool
    email_verified_at: datetime | None
    created_at: datetime
    notify_on_done: bool
    webhook_secret_hint: str


class SessionOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def user_out(user: User) -> dict:
    secret = user.webhook_secret or ""
    return UserOut(
        id=user.id, email=user.email, name=user.name, role=user.role, is_admin=user.is_admin,
        email_verified=user.email_verified_at is not None, email_verified_at=user.email_verified_at,
        created_at=user.created_at, notify_on_done=user.notify_on_done,
        webhook_secret_hint=f"whsec_...{secret[-4:]}" if secret else "",
    ).model_dump(mode="json")


# ------------------------------------------------------------------ helpers
def _limit(name: str, key: str) -> None:
    wait = limiter.hit(name, key)
    if wait:
        raise rate_limited(int(wait))


def _session_response(user: User, issued: sessions.IssuedSession, status_code: int = 200,
                      extra: dict | None = None) -> JSONResponse:
    body = {"access_token": issued.access_token, "token_type": "bearer", "expires_in": issued.expires_in,
            "user": user_out(user), **(extra or {})}
    response = JSONResponse(body, status_code=status_code)
    set_session_cookies(response, issued.access_token, issued.expires_in, issued.refresh_token)
    return response


def load_user(db: Session, principal: Principal) -> User:
    user = db.get(User, principal.user_id) if principal.user_id else None
    if user is None or not user.is_active:
        raise ServiceError("log in again", code="unauthorized", status=401)
    return user


# ------------------------------------------------------------------ sign-up / login
@router.post("/signup", status_code=201, response_model=SessionOut,
             summary="Create an account (signs you in and emails a verification link)")
def signup(body: SignupIn, request: Request, db: DB):
    ip = client_ip(request)
    wait = limiter.check("signup_ip", ip)
    if wait:
        raise rate_limited(int(wait))
    user = users.signup(db, body.email, body.password, body.name)
    token = users.issue_email_token(db, user, TOKEN_VERIFY)
    issued = sessions.issue(db, user, user_agent(request), ip)
    db.commit()
    limiter.hit("signup_ip", ip)  # only accounts actually created count
    notifications.send_verification(user.email, user.name, token)
    return _session_response(user, issued, 201, {"verification_sent": True})


@router.post("/login", response_model=SessionOut, summary="Log in (sets the session cookies)")
def login(body: LoginIn, request: Request, db: DB):
    ip = client_ip(request)
    email_key = body.email.strip().lower()[:320]
    _limit("login_ip", ip)
    wait = limiter.check("login_email", email_key)
    if wait:
        raise rate_limited(int(wait))
    try:
        user = users.authenticate(db, body.email, body.password)
    except ServiceError as exc:
        if exc.code == "invalid_credentials":
            limiter.hit("login_email", email_key)
        raise
    issued = sessions.issue(db, user, user_agent(request), ip)
    db.commit()
    return _session_response(user, issued)


@router.post("/refresh", response_model=SessionOut, summary="Exchange the refresh token for a new access token")
def refresh(request: Request, db: DB, body: RefreshIn | None = None):
    _limit("refresh_ip", client_ip(request))
    raw = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    try:
        user, issued = sessions.rotate(db, raw, user_agent(request), client_ip(request))
    except ServiceError as exc:
        db.commit()  # a replayed token revoked its whole family: keep that
        response = service_error_response(exc)
        clear_session_cookies(response)
        return response
    db.commit()
    return _session_response(user, issued)


@router.post("/logout", summary="Log out this browser")
def logout(request: Request, db: DB, body: RefreshIn | None = None):
    raw = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    if sessions.revoke(db, raw):
        db.commit()
    response = JSONResponse({"ok": True})
    clear_session_cookies(response)
    return response


@router.post("/logout-all", summary="Log out every browser and invalidate access tokens")
def logout_all(principal: CurrentUser, db: DB):
    user = load_user(db, principal)
    users.logout_everywhere(db, user)
    db.commit()
    response = JSONResponse({"ok": True})
    clear_session_cookies(response)
    return response


# ------------------------------------------------------------------ profile
@router.get("/me", response_model=UserOut, summary="The signed-in user")
def me(principal: CurrentUser, db: DB):
    return user_out(load_user(db, principal))


@router.patch("/me", response_model=UserOut, summary="Update your name and email notifications")
def update_me(body: ProfileIn, principal: CurrentUser, db: DB):
    user = load_user(db, principal)
    users.update_profile(db, user, name=body.name, notify_on_done=body.notify_on_done)
    db.commit()
    return user_out(user)


@router.post("/change-password", summary="Change the password (signs out every other browser)")
def change_password(body: ChangePasswordIn, request: Request, principal: CurrentUser, db: DB):
    user = load_user(db, principal)
    family = sessions.family_of(db, request.cookies.get(REFRESH_COOKIE))
    users.change_password(db, user, body.current_password, body.new_password, keep_family=family)
    db.commit()
    notifications.send_password_changed(user.email, user.name)
    access, ttl = make_access_token(user.id, user.email, user.role)
    response = JSONResponse({"ok": True, "access_token": access, "token_type": "bearer", "expires_in": ttl})
    if family:  # this browser stays signed in with a fresh access cookie
        set_access_cookie(response, access, ttl)
    return response


# ------------------------------------------------------------------ email verification / reset
@router.post("/verify", summary="Confirm an email address with the token from the link")
def verify(body: TokenIn, db: DB):
    user, newly = users.verify_email(db, body.token)
    db.commit()
    if newly:
        notifications.send_welcome(user.email, user.name)
    return {"verified": True, "already_verified": not newly, "email": user.email}


@router.post("/resend-verification", summary="Send the verification email again")
def resend_verification(principal: CurrentUser, db: DB):
    user = load_user(db, principal)
    if user.email_verified_at is not None:
        raise ServiceError("your email address is already verified", code="already_verified", status=409)
    _limit("verify_resend", user.id)
    token = users.issue_email_token(db, user, TOKEN_VERIFY)
    db.commit()
    notifications.send_verification(user.email, user.name, token)
    return {"ok": True}


@router.post("/forgot", summary="Email a password-reset link (same answer whether or not the account exists)")
def forgot(body: EmailIn, request: Request, db: DB):
    _limit("forgot_ip", client_ip(request))
    found = users.request_password_reset(db, body.email)
    if found is not None:
        user, token = found
        if limiter.hit("forgot_email", user.email) is None:
            db.commit()
            notifications.send_reset(user.email, user.name, token)
        else:
            db.rollback()
    return {"ok": True}


@router.post("/reset", summary="Set a new password with the token from the reset link")
def reset(body: ResetIn, db: DB):
    user = users.reset_password(db, body.token, body.new_password)
    db.commit()
    notifications.send_password_changed(user.email, user.name)
    response = JSONResponse({"ok": True, "email": user.email})
    clear_session_cookies(response)
    return response


# ------------------------------------------------------------------ sessions / webhook secret
@router.get("/sessions", summary="Browsers signed in to this account")
def list_sessions(request: Request, principal: CurrentUser, db: DB):
    current = sessions.family_of(db, request.cookies.get(REFRESH_COOKIE))
    seen: set[str] = set()
    items = []
    for token in sessions.list_active(db, principal.user_id):
        if token.family_id in seen:
            continue
        seen.add(token.family_id)
        items.append({"id": token.id, "user_agent": token.user_agent, "ip": token.ip,
                      "last_active_at": (token.last_used_at or token.created_at).isoformat(),
                      "current": token.family_id == current})
    return {"items": items}


@router.delete("/sessions/{session_id}", summary="Sign out one browser")
def revoke_session(session_id: str, principal: CurrentUser, db: DB):
    if not sessions.revoke_session(db, principal.user_id, session_id):
        raise ServiceError("no such session", code="not_found", status=404)
    db.commit()
    return {"ok": True}


@router.get("/webhook-secret", summary="Reveal the secret that signs your job webhooks")
def webhook_secret(principal: CurrentUser, db: DB):
    return {"webhook_secret": load_user(db, principal).webhook_secret}


@router.post("/webhook-secret/rotate", summary="Replace the webhook signing secret")
def rotate_webhook_secret(principal: CurrentUser, db: DB):
    user = load_user(db, principal)
    secret = users.rotate_webhook_secret(user)
    db.commit()
    return {"webhook_secret": secret}
