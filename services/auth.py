"""Who is calling: turn a request's credentials into a Principal
(plan.MD §5.3). Pure logic; the ASGI middleware in api/auth.py calls
resolve() and attaches the result to the request.

Order (the first credential present decides; an invalid one does NOT fall
back to the next, so a bad key is always a clear 401):
  1. Authorization: Bearer hc_live_...   an API key
  2. Authorization: Bearer <jwt>         an access token (from /auth/login)
  3. X-API-Key: hc_live_...              an API key
  4. X-API-Key: <HC_API_TOKEN>           the operator (admin, all scopes)
  5. cookie hc_api_key = HC_API_TOKEN     the operator, from the old web UI
  6. cookie hc_access                     a browser session
  7. cookie hc_refresh (GET/HEAD only)    a browser session whose access
                                          cookie just expired (video range
                                          requests, downloads)
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import unquote

from core.config import get_settings
from db.session import session_scope

from . import api_keys, sessions
from .scopes import ALL_SCOPES
from .tokens import decode_access_token, looks_like_jwt
from .usercache import UserSnapshot, get_user_snapshot

ACCESS_COOKIE = "hc_access"
REFRESH_COOKIE = "hc_refresh"
LEGACY_KEY_COOKIE = "hc_api_key"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Principal:
    # session | bearer | api_key | ops
    via: str
    user_id: str | None = None
    email: str | None = None
    name: str = ""
    role: str = "user"
    email_verified: bool = False
    scopes: frozenset[str] = ALL_SCOPES
    api_key_id: str | None = None
    # authenticated by a cookie the browser sends on its own (CSRF applies)
    cookie_auth: bool = False
    # only the refresh cookie was valid (the access cookie had expired): a
    # page load hands out fresh cookies (api/routers/pages.py)
    refresh_only: bool = False

    @property
    def is_admin(self) -> bool:
        return self.via == "ops" or self.role == "admin"

    @property
    def is_user(self) -> bool:
        return self.user_id is not None

    def can(self, scope: str) -> bool:
        return scope in self.scopes


OPS = Principal(via="ops", role="admin", email_verified=True)
OPS_FROM_COOKIE = Principal(via="ops", role="admin", email_verified=True, cookie_auth=True)


@dataclass(frozen=True)
class Credentials:
    bearer: str | None = None
    api_key_header: str | None = None
    legacy_cookie: str | None = None
    access_cookie: str | None = None
    refresh_cookie: str | None = None

    @property
    def present(self) -> bool:
        return any((self.bearer, self.api_key_header, self.legacy_cookie, self.access_cookie, self.refresh_cookie))

    @property
    def uses_cookies(self) -> bool:
        return not (self.bearer or self.api_key_header) and bool(
            self.legacy_cookie or self.access_cookie or self.refresh_cookie)


def credentials_from(headers: Mapping[str, str], cookies: Mapping[str, str]) -> Credentials:
    bearer = None
    authorization = headers.get("authorization") or ""
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        bearer = value.strip()
    legacy = cookies.get(LEGACY_KEY_COOKIE)
    return Credentials(
        bearer=bearer,
        api_key_header=(headers.get("x-api-key") or "").strip() or None,
        legacy_cookie=unquote(legacy) if legacy else None,
        access_cookie=cookies.get(ACCESS_COOKIE) or None,
        refresh_cookie=cookies.get(REFRESH_COOKIE) or None,
    )


def _is_ops_token(value: str | None) -> bool:
    token = get_settings().hc_api_token
    return bool(token and value) and hmac.compare_digest(value.encode(), token.encode())


def _from_user(user: UserSnapshot, via: str, *, cookie_auth: bool = False, refresh_only: bool = False) -> Principal:
    return Principal(via=via, user_id=user.id, email=user.email, name=user.name, role=user.role,
                     email_verified=user.email_verified, cookie_auth=cookie_auth, refresh_only=refresh_only)


def principal_from_access_token(token: str, via: str = "bearer", cookie_auth: bool = False) -> Principal | None:
    claims = decode_access_token(token)
    if claims is None:
        return None
    user = get_user_snapshot(claims["sub"])
    if user is None or not user.is_active:
        return None
    if user.tokens_valid_after is not None and int(claims["iat"]) < int(user.tokens_valid_after.timestamp()):
        return None  # issued before a password change / "log out everywhere"
    return _from_user(user, via, cookie_auth=cookie_auth)


def _from_api_key(raw: str) -> Principal | None:
    identity = api_keys.authenticate(raw)
    if identity is None:
        return None
    user = identity.user
    return Principal(via="api_key", user_id=user.id, email=user.email, name=user.name, role=user.role,
                     email_verified=user.email_verified, scopes=identity.scopes, api_key_id=identity.key_id)


def _from_refresh_cookie(raw: str) -> Principal | None:
    with session_scope() as db:
        user_id = sessions.user_id_for_refresh(db, raw)
    if user_id is None:
        return None
    user = get_user_snapshot(user_id)
    if user is None or not user.is_active:
        return None
    return _from_user(user, "session", cookie_auth=True, refresh_only=True)


def resolve(creds: Credentials, method: str) -> Principal | None:
    """The caller, or None (anonymous or invalid credentials). Touches the
    database, so the middleware runs it in a worker thread."""
    if creds.bearer:
        if creds.bearer.startswith(api_keys.KEY_PREFIX):
            return _from_api_key(creds.bearer)
        if looks_like_jwt(creds.bearer):
            return principal_from_access_token(creds.bearer, via="bearer")
        return OPS if _is_ops_token(creds.bearer) else None
    if creds.api_key_header:
        if creds.api_key_header.startswith(api_keys.KEY_PREFIX):
            return _from_api_key(creds.api_key_header)
        return OPS if _is_ops_token(creds.api_key_header) else None
    if creds.legacy_cookie and _is_ops_token(creds.legacy_cookie):
        return OPS_FROM_COOKIE
    if creds.access_cookie:
        principal = principal_from_access_token(creds.access_cookie, via="session", cookie_auth=True)
        if principal is not None:
            return principal
    if creds.refresh_cookie and method.upper() in SAFE_METHODS:
        return _from_refresh_cookie(creds.refresh_cookie)
    return None


def open_dev_mode() -> bool:
    """Pre-accounts local development: no operator key configured and
    DEV_OPEN_API=true -> requests without credentials act as the operator.
    Never in prod (settings_problems refuses to start)."""
    settings = get_settings()
    return settings.dev_open_api and not settings.hc_api_token and not settings.is_prod


def auth_enabled() -> bool:
    return not open_dev_mode()
