"""X-API-Key check for every route except /health and the static UI pages
(plan D14). The service holds account-wide Zoom scopes on a public IP, so it
must not be callable anonymously once deployed.

The key is accepted from the `X-API-Key` header (n8n, curl) or from the
`hc_api_key` cookie, which the web UI sets so that <video>/<a download> links
work without custom headers. An empty HC_API_TOKEN disables the check (local
development only).
"""

from __future__ import annotations

import hmac
from urllib.parse import unquote

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from core.config import get_settings

API_KEY_HEADER = "X-API-Key"
API_KEY_COOKIE = "hc_api_key"
PUBLIC_PATHS = frozenset({"/health", "/", "/app", "/styles.css", "/favicon.ico"})


def auth_enabled() -> bool:
    return bool(get_settings().hc_api_token)


def _authorized(scope: Scope) -> bool:
    token = get_settings().hc_api_token
    conn = HTTPConnection(scope)  # headers and cookies only; never touches the body
    if not token or conn.url.path in PUBLIC_PATHS:
        return True
    supplied = conn.headers.get(API_KEY_HEADER) or unquote(conn.cookies.get(API_KEY_COOKIE) or "")
    return hmac.compare_digest(supplied.encode(), token.encode())


class ApiKeyMiddleware:
    """Pure ASGI middleware, so the key is checked before routing and before
    anything calls `receive`. As an app-level dependency the check ran only
    after FastAPI had parsed the request: an anonymous multi-GB upload was
    spooled to disk just to be answered 401. Being ahead of the router it also
    covers paths no route matches (no route enumeration without a key)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or _authorized(scope):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=401,
            content={
                "detail": {"error_code": "unauthorized", "message": f"missing or wrong {API_KEY_HEADER}"}
            },
        )
        await response(scope, receive, send)
