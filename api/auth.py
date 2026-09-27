"""Request authentication for the API and pages (plan.MD §5.3, §5.6, §5.7).

AuthMiddleware is pure ASGI and runs before routing and before anything reads
the request body:
  - it resolves the caller (services.auth.resolve) and stores it in
    request.state.principal;
  - API paths (/api/..., and the older unprefixed /jobs and /zoom/...) need
    a caller except for a few public ones (health, login, sign-up, ...): an
    anonymous multi-GB upload is answered 401 without being spooled to disk;
  - a state-changing request authenticated by cookies must carry
    X-Requested-With: hc-web (the site's own fetch wrapper sends it; a form
    on another site can't), on top of SameSite=Lax cookies;
  - requests are rate-limited per API key and per signed-in user.

Routes then declare what they need with the dependencies below."""

from __future__ import annotations

import anyio
from fastapi import Request, Response
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from core.config import get_settings
from services.auth import (
    ACCESS_COOKIE,
    OPS,
    REFRESH_COOKIE,
    SAFE_METHODS,
    Principal,
    auth_enabled,
    credentials_from,
    open_dev_mode,
    resolve,
)
from services.errors import ServiceError

from .errors import error_body, error_response
from .ratelimit import limiter

__all__ = ["ACCESS_COOKIE", "REFRESH_COOKIE", "AuthMiddleware", "Principal", "auth_enabled"]

API_V1 = "/api/v1"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "hc-web"

PUBLIC_API_PATHS = frozenset({
    "/health",
    f"{API_V1}/health",
    f"{API_V1}/openapi.json",
    "/api/docs",
    "/api/docs/oauth2-redirect",
    f"{API_V1}/auth/signup",
    f"{API_V1}/auth/login",
    f"{API_V1}/auth/refresh",
    f"{API_V1}/auth/logout",
    f"{API_V1}/auth/forgot",
    f"{API_V1}/auth/reset",
    f"{API_V1}/auth/verify",
    f"{API_V1}/public/config",
})


def is_legacy_api_path(path: str) -> bool:
    return path == "/jobs" or path.startswith(("/jobs/", "/zoom/"))


def is_api_path(path: str) -> bool:
    return path.startswith("/api/") or is_legacy_api_path(path)


class AuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith("/static/"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        headers = Headers(scope=scope)
        creds = credentials_from(headers, cookie_parser(headers.get("cookie", "")))
        principal = await anyio.to_thread.run_sync(resolve, creds, method) if creds.present else None

        api = is_api_path(path)
        public = path in PUBLIC_API_PATHS
        if api and is_legacy_api_path(path) and not get_settings().legacy_api_enabled:
            await _respond(scope, receive, send, error_response(
                404, "not_found", f"this route moved to {API_V1}{path} (the unprefixed API is switched off)"))
            return
        if principal is None and api and not public and open_dev_mode():
            principal = OPS
        state = scope.setdefault("state", {})
        state["principal"] = principal

        if api and not public and principal is None:
            message = "log in, or send an API key (Authorization: Bearer hc_live_... or X-API-Key)"
            await _respond(scope, receive, send, _unauthorized(message))
            return
        if method not in SAFE_METHODS and creds.uses_cookies and \
                headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
            await _respond(scope, receive, send, error_response(
                403, "csrf", f"cookie-authenticated requests must send the header X-Requested-With: {CSRF_VALUE}"))
            return
        if api and principal is not None:
            wait = None
            if principal.via == "api_key" and principal.api_key_id:
                wait = limiter.hit("api_key", principal.api_key_id)
            elif principal.user_id:
                wait = limiter.hit("user", principal.user_id)
            if wait:
                await _respond(scope, receive, send, error_response(
                    429, "rate_limited", "too many requests; slow down", retryable=True, retry_after_s=int(wait)))
                return
        await self.app(scope, receive, send)


def _unauthorized(message: str):
    response = error_response(401, "unauthorized", {"error_code": "unauthorized", "message": message},
                              headers={"WWW-Authenticate": "Bearer"})
    return response


async def _respond(scope: Scope, receive: Receive, send: Send, response: Response) -> None:
    await response(scope, receive, send)


# ------------------------------------------------------------------ dependencies
def current_principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


def require(*scopes: str, write: bool = False):
    """Dependency factory: a caller holding every scope in `scopes`; with
    write=True a signed-in user must also have verified their email."""

    def dependency(request: Request) -> Principal:
        principal = current_principal(request)
        if principal is None:
            raise ServiceError("log in or send an API key", code="unauthorized", status=401)
        missing = [scope for scope in scopes if not principal.can(scope)]
        if missing:
            raise ServiceError(f"this API key lacks the scope(s) {', '.join(missing)}", code="insufficient_scope",
                               status=403)
        if write and principal.is_user and not principal.email_verified:
            raise ServiceError("verify your email address first (check your inbox, or resend it from your account)",
                               code="email_not_verified", status=403)
        return principal

    return dependency


def require_user(request: Request) -> Principal:
    """A signed-in person (browser session or bearer access token), not an
    API key and not the operator key: account settings and key management."""
    principal = current_principal(request)
    if principal is None:
        raise ServiceError("log in first", code="unauthorized", status=401)
    if not principal.is_user or principal.via not in ("session", "bearer"):
        raise ServiceError("this needs a signed-in user, not an API key", code="session_required", status=403)
    return principal


def require_admin(request: Request) -> Principal:
    principal = current_principal(request)
    if principal is None:
        raise ServiceError("log in first", code="unauthorized", status=401)
    if not principal.is_admin:
        raise ServiceError("admins only", code="forbidden", status=403)
    return principal


# ------------------------------------------------------------------ cookies / client
def set_access_cookie(response: Response, access_token: str, access_ttl_s: int) -> None:
    response.set_cookie(ACCESS_COOKIE, access_token, max_age=access_ttl_s, httponly=True,
                        secure=get_settings().cookie_secure_effective, samesite="lax", path="/")


def set_session_cookies(response: Response, access_token: str, access_ttl_s: int, refresh_token: str) -> None:
    settings = get_settings()
    secure = settings.cookie_secure_effective
    set_access_cookie(response, access_token, access_ttl_s)
    response.set_cookie(REFRESH_COOKIE, refresh_token, max_age=max(1, settings.jwt_refresh_ttl_days) * 86400,
                        httponly=True, secure=secure, samesite="lax", path="/")


def clear_session_cookies(response: Response) -> None:
    secure = get_settings().cookie_secure_effective
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1][:64]  # the address our own proxy saw
    return (request.client.host if request.client else "")[:64]


def user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:300]


def error_json(code: str, message: str) -> dict:
    return error_body(code, message)
