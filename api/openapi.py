"""The published API description (/api/v1/openapi.json, rendered at /api/docs)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

DESCRIPTION = """
Turn meeting recordings into a cut video, a highlights reel, vertical shorts, a
removed-parts video, a PDF report, transcripts and YouTube chapters, from
your own automations (n8n, Zapier, Make, scripts).

**Authentication.** Create an API key on the site (*API keys* page) and send it
as `Authorization: Bearer hc_live_...` (or `X-API-Key: hc_live_...`). Keys carry
scopes: `jobs:read`, `jobs:write`, `zoom:read`. A short-lived access token from
`POST /api/v1/auth/login` works too (`Authorization: Bearer <token>`).

**Jobs.** Create one with `POST /api/v1/jobs/zoom`, `/jobs/youtube` or `/jobs`
(upload), then either poll `GET /api/v1/jobs/{job_id}` until `status` is
`done` / `failed` / `decided`, or pass `callback_url` and receive a signed
webhook. Download everything with `GET /api/v1/jobs/{job_id}/bundle`.

**Errors** always look like
`{"error_code": "...", "detail": ..., "retryable": bool, "retry_after_s": int|null}`;
retry only when `retryable` is true, after `retry_after_s` seconds.

**Webhooks** carry `X-HC-Signature: t=<unix>,v1=<hex>` =
HMAC-SHA256(your webhook secret, `"<t>." + raw body`). The site's docs page has
verification code for Python and Node.
""".strip()

TAGS = [
    {"name": "Jobs", "description": "Create jobs, follow their progress, download their outputs."},
    {"name": "Zoom", "description": "The workspace's Zoom cloud recordings."},
    {"name": "Accounts", "description": "Sign-up, login, sessions, email verification, password reset."},
    {"name": "API keys", "description": "Keys for automations (manage them with a signed-in session)."},
    {"name": "Account", "description": "Usage, webhook test, account deletion, public configuration."},
    {"name": "Admin", "description": "Users and jobs across the workspace (admins only)."},
    {"name": "Service", "description": "Health."},
]

PUBLIC_OPERATIONS = {
    ("/api/v1/health", "get"),
    ("/api/v1/public/config", "get"),
    ("/api/v1/auth/signup", "post"),
    ("/api/v1/auth/login", "post"),
    ("/api/v1/auth/refresh", "post"),
    ("/api/v1/auth/logout", "post"),
    ("/api/v1/auth/forgot", "post"),
    ("/api/v1/auth/reset", "post"),
    ("/api/v1/auth/verify", "post"),
}


def install_openapi(app: FastAPI) -> None:
    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=DESCRIPTION, routes=app.routes,
                             tags=TAGS)
        components = schema.setdefault("components", {})
        components["securitySchemes"] = {
            "BearerAuth": {"type": "http", "scheme": "bearer",
                           "description": "An API key (hc_live_...) or an access token from /auth/login"},
            "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key",
                             "description": "An API key (hc_live_...)"},
        }
        schema["security"] = [{"BearerAuth": []}, {"ApiKeyHeader": []}]
        for path, operations in schema.get("paths", {}).items():
            for method, operation in operations.items():
                if (path, method) in PUBLIC_OPERATIONS:
                    operation["security"] = []
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
