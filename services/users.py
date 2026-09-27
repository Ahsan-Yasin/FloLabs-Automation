"""Accounts: sign-up, login, email verification, password reset and change,
profile, admin operations (plan.MD §5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.config import get_settings
from db.base import new_webhook_secret
from db.models import ROLE_ADMIN, ROLE_USER, TOKEN_RESET, TOKEN_VERIFY, EmailToken, User

from . import passwords, sessions
from .errors import ServiceError, invalid_credentials
from .tokens import hash_token, new_opaque_token
from .usercache import invalidate_user

VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
MAX_NAME = 120


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    """Trimmed, validated and lower-cased (one account per address however
    it is capitalised)."""
    value = (email or "").strip()
    if not value or len(value) > 320:
        raise ServiceError("enter a valid email address", code="invalid_email", status=422)
    try:
        info = validate_email(value, check_deliverability=False)
    except EmailNotValidError as exc:
        raise ServiceError(f"enter a valid email address ({exc})", code="invalid_email", status=422) from exc
    return info.normalized.lower()


def clean_name(name: str | None) -> str:
    return " ".join((name or "").split())[:MAX_NAME]


def _weak(reason: str) -> ServiceError:
    return ServiceError(reason, code="weak_password", status=422)


def _check_password(password: str, email: str) -> None:
    reason = passwords.check_policy(password or "", email)
    if reason:
        raise _weak(reason)


def get_by_email(db: Session, email: str) -> User | None:
    try:
        email = normalize_email(email)
    except ServiceError:
        return None
    return db.scalar(select(User).where(User.email == email))


# ------------------------------------------------------------------ sign-up / login
def signup(db: Session, email: str, password: str, name: str = "") -> User:
    settings = get_settings()
    if not settings.signup_enabled:
        raise ServiceError("sign-up is closed; ask an admin for an account", code="signup_closed", status=403)
    email = normalize_email(email)
    domains = settings.allowed_signup_domain_set
    if domains and email.rsplit("@", 1)[1] not in domains:
        raise ServiceError(f"sign-up is limited to {', '.join(sorted(domains))} addresses",
                           code="signup_domain_not_allowed", status=403)
    _check_password(password, email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise email_taken()
    user = User(email=email, password_hash=passwords.hash_password(password), name=clean_name(name))
    try:
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError as exc:  # two sign-ups for the same address at once
        raise email_taken() from exc
    return user


def email_taken() -> ServiceError:
    return ServiceError("an account with this email already exists; log in instead", code="email_taken",
                        status=409)


def authenticate(db: Session, email: str, password: str) -> User:
    try:
        email = normalize_email(email)
    except ServiceError:
        passwords.burn_time(password or "")
        raise invalid_credentials() from None
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        passwords.burn_time(password or "")
        raise invalid_credentials()
    if not passwords.verify_password(user.password_hash, password or ""):
        raise invalid_credentials()
    if not user.is_active:
        raise ServiceError("this account is disabled; contact the site admin", code="account_disabled", status=403)
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    user.last_login_at = _now()
    return user


# ------------------------------------------------------------------ email tokens
def issue_email_token(db: Session, user: User, kind: str) -> str:
    """A new one-time link token; older unused links of the same kind stop
    working. Returns the raw token (only its hash is stored)."""
    now = _now()
    for old in db.scalars(select(EmailToken).where(EmailToken.user_id == user.id, EmailToken.kind == kind,
                                                   EmailToken.used_at.is_(None))):
        old.used_at = now
    raw, digest = new_opaque_token()
    ttl = VERIFY_TTL if kind == TOKEN_VERIFY else RESET_TTL
    db.add(EmailToken(user_id=user.id, kind=kind, token_hash=digest, expires_at=now + ttl))
    return raw


def _find_token(db: Session, raw: str | None, kind: str) -> EmailToken | None:
    if not raw or len(raw) > 200:
        return None
    return db.scalar(select(EmailToken).where(EmailToken.token_hash == hash_token(raw), EmailToken.kind == kind))


def token_invalid(message: str = "this link is invalid or has expired") -> ServiceError:
    return ServiceError(message, code="token_invalid", status=400)


def verify_email(db: Session, raw: str | None) -> tuple[User, bool]:
    """(user, newly_verified). Opening an old link of an already verified
    account is a success, not an error: mail scanners often open links
    before the person does."""
    token = _find_token(db, raw, TOKEN_VERIFY)
    if token is None:
        raise token_invalid()
    user = db.get(User, token.user_id)
    if user is None:
        raise token_invalid()
    now = _now()
    if user.email_verified_at is not None:
        token.used_at = token.used_at or now
        return user, False
    if token.used_at is not None or token.expires_at <= now:
        raise token_invalid("this verification link has expired; request a new one from your account")
    token.used_at = now
    _mark_verified(user, now)
    return user, True


def _mark_verified(user: User, when: datetime) -> None:
    user.email_verified_at = when
    if user.email in get_settings().admin_email_set and user.role != ROLE_ADMIN:
        user.role = ROLE_ADMIN
    invalidate_user(user.id)


def request_password_reset(db: Session, email: str) -> tuple[User, str] | None:
    """(user, raw token) when the address belongs to an active account; None
    otherwise (the API answers the same either way)."""
    user = get_by_email(db, email)
    if user is None or not user.is_active:
        return None
    return user, issue_email_token(db, user, TOKEN_RESET)


def reset_token_is_valid(db: Session, raw: str | None) -> bool:
    token = _find_token(db, raw, TOKEN_RESET)
    return token is not None and token.used_at is None and token.expires_at > _now()


def reset_password(db: Session, raw: str | None, new_password: str) -> User:
    token = _find_token(db, raw, TOKEN_RESET)
    now = _now()
    if token is None or token.used_at is not None or token.expires_at <= now:
        raise token_invalid("this reset link is invalid or has expired; request a new one")
    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise token_invalid()
    _check_password(new_password, user.email)
    token.used_at = now
    _set_password(db, user, new_password)
    if user.email_verified_at is None:  # the link reached the inbox: the address is theirs
        _mark_verified(user, now)
    return user


# ------------------------------------------------------------------ account
def _set_password(db: Session, user: User, password: str, keep_family: str | None = None) -> None:
    user.password_hash = passwords.hash_password(password)
    # access tokens issued before this second are refused (services.auth)
    user.tokens_valid_after = _now()
    sessions.revoke_all(db, user.id, except_family=keep_family)
    invalidate_user(user.id)


def change_password(db: Session, user: User, current: str, new: str, keep_family: str | None = None) -> None:
    if not passwords.verify_password(user.password_hash, current or ""):
        raise ServiceError("the current password is wrong", code="invalid_credentials", status=403)
    _check_password(new, user.email)
    if passwords.verify_password(user.password_hash, new):
        raise ServiceError("the new password is the same as the current one", code="weak_password", status=422)
    _set_password(db, user, new, keep_family=keep_family)


def update_profile(db: Session, user: User, *, name: str | None = None, zoom_host_email: str | None = None,
                   notify_on_done: bool | None = None) -> User:
    if name is not None:
        user.name = clean_name(name)
    if zoom_host_email is not None:
        user.zoom_host_email = normalize_email(zoom_host_email) if zoom_host_email.strip() else None
    if notify_on_done is not None:
        user.notify_on_done = bool(notify_on_done)
    invalidate_user(user.id)
    return user


def rotate_webhook_secret(user: User) -> str:
    user.webhook_secret = new_webhook_secret()
    return user.webhook_secret


def logout_everywhere(db: Session, user: User) -> None:
    user.tokens_valid_after = _now()
    sessions.revoke_all(db, user.id)
    invalidate_user(user.id)


# ------------------------------------------------------------------ admin
def admin_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == ROLE_ADMIN,
                                                                 User.is_active.is_(True))) or 0


def create_admin(db: Session, email: str, password: str, name: str = "") -> User:
    email = normalize_email(email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise ValueError(f"{email} already has an account; use `promote` (and `set-password`) instead")
    _check_password(password, email)
    now = _now()
    user = User(email=email, password_hash=passwords.hash_password(password), name=clean_name(name),
                role=ROLE_ADMIN, email_verified_at=now)
    db.add(user)
    db.flush()
    return user


def _require(db: Session, email: str) -> User:
    user = get_by_email(db, email)
    if user is None:
        raise LookupError(f"no account for {email}")
    return user


def set_role(db: Session, email: str, role: str) -> User:
    if role not in (ROLE_USER, ROLE_ADMIN):
        raise ValueError("role must be user or admin")
    user = _require(db, email)
    if role == ROLE_USER and user.role == ROLE_ADMIN and admin_count(db) <= 1:
        raise ValueError("that is the last admin; promote someone else first")
    user.role = role
    invalidate_user(user.id)
    return user


def set_active(db: Session, email: str, active: bool) -> User:
    user = _require(db, email)
    if not active and user.role == ROLE_ADMIN and admin_count(db) <= 1:
        raise ValueError("that is the last active admin")
    user.is_active = active
    if not active:
        logout_everywhere(db, user)
    invalidate_user(user.id)
    return user


def set_password_by_email(db: Session, email: str, password: str) -> User:
    user = _require(db, email)
    _check_password(password, user.email)
    _set_password(db, user, password)
    return user


def promote_configured_admins(db: Session) -> int:
    """Verified users listed in ADMIN_EMAILS become admins (startup)."""
    emails = get_settings().admin_email_set
    if not emails:
        return 0
    promoted = 0
    for user in db.scalars(select(User).where(User.email.in_(emails), User.role != ROLE_ADMIN,
                                              User.email_verified_at.is_not(None))):
        user.role = ROLE_ADMIN
        invalidate_user(user.id)
        promoted += 1
    return promoted


def first_admin_id(db: Session) -> str | None:
    return db.scalar(select(User.id).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
                     .order_by(User.created_at).limit(1))
