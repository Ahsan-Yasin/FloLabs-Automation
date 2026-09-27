"""Response hardening for every request (plan.MD §10, §12).

- A per-request nonce: pages may only run scripts from this site or inline
  scripts carrying the nonce (Content-Security-Policy).
- Standard headers: nosniff, no framing, a strict referrer policy, HSTS when
  the site is served over https.
- X-Request-ID on every response (taken from the proxy when it sent a sane
  one), so a user's report can be matched with the log line.
- Caching: versioned static files (?v=<hash>) are immutable for a year,
  JSON API answers are never stored.
"""

from __future__ import annotations

import re
import secrets
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.config import get_settings
from core.logging import request_id_var

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

FONT_CSS = "https://fonts.googleapis.com"
FONT_FILES = "https://fonts.gstatic.com"
SWAGGER_CDN = "https://cdn.jsdelivr.net"


def page_csp(nonce: str, https: bool) -> str:
    parts = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        f"style-src 'self' 'unsafe-inline' {FONT_CSS}",
        f"font-src 'self' {FONT_FILES} data:",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ]
    if https:
        parts.append("upgrade-insecure-requests")
    return "; ".join(parts)


def docs_csp() -> str:
    """The interactive API docs (FastAPI's Swagger UI) load their bundle from
    jsDelivr and start it with an inline script."""
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'unsafe-inline' {SWAGGER_CDN}",
        f"style-src 'self' 'unsafe-inline' {SWAGGER_CDN}",
        f"img-src 'self' data: {SWAGGER_CDN} https://fastapi.tiangolo.com",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ])


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex[:16]
        nonce = secrets.token_urlsafe(16)
        state = scope.setdefault("state", {})
        state["csp_nonce"] = nonce
        state["request_id"] = request_id
        path: str = scope.get("path", "")
        versioned = b"v=" in scope.get("query_string", b"")
        https = get_settings().cookie_secure_effective

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
                if https:
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                content_type = headers.get("content-type", "")
                if content_type.startswith("text/html"):
                    headers["Content-Security-Policy"] = docs_csp() if path.startswith("/api/docs") \
                        else page_csp(nonce, https)
                if path.startswith("/static/"):
                    headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned \
                        else "public, max-age=300"
                elif content_type.startswith("application/json"):
                    headers.setdefault("Cache-Control", "no-store")
                elif content_type.startswith("text/html"):
                    headers.setdefault("Cache-Control", "no-cache")
            await send(message)

        token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            request_id_var.reset(token)
