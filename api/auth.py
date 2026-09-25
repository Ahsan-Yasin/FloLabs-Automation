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

from fastapi import HTTPException, Request

from core.config import get_settings

API_KEY_HEADER = "X-API-Key"
API_KEY_COOKIE = "hc_api_key"
PUBLIC_PATHS = frozenset({"/health", "/", "/app", "/styles.css", "/favicon.ico"})


def auth_enabled() -> bool:
    return bool(get_settings().hc_api_token)


async def require_api_key(request: Request) -> None:
    token = get_settings().hc_api_token
    if not token or request.url.path in PUBLIC_PATHS:
        return
    supplied = request.headers.get(API_KEY_HEADER) or unquote(request.cookies.get(API_KEY_COOKIE) or "")
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(
            status_code=401,
            detail={"error_code": "unauthorized", "message": f"missing or wrong {API_KEY_HEADER}"},
        )
