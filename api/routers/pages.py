"""The website: public pages, sign-in pages and the app (plan.MD §9, §10).

Server-rendered Jinja templates (web/templates) + one stylesheet + small
vanilla scripts (web/static). The app pages talk to /api/v1 from the browser
with the session cookies; the server only decides who may see a page.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from core.config import get_settings
from core.logging import get_logger
from core.models import ErrorCode, JobOptions, JobStatus
from core.version import PIPELINE_VERSION
from db.session import session_scope
from services import notifications, sessions, users
from services.auth import REFRESH_COOKIE, Principal
from services.errors import ServiceError

from ..auth import set_session_cookies
from ..errors import set_html_error_renderer

logger = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

router = APIRouter(include_in_schema=False)


# ------------------------------------------------------------------ assets
@lru_cache(maxsize=256)
def _asset_hash(path: str, mtime_ns: int) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]


def asset(path: str) -> str:
    """/static/<path>?v=<content hash>: browsers cache it for a year and pick
    up a new version the moment the file changes."""
    file = STATIC_DIR / path
    try:
        return f"/static/{path}?v={_asset_hash(path, file.stat().st_mtime_ns)}"
    except OSError:
        return f"/static/{path}"


# ------------------------------------------------------------------ templates
def _page_principal(request: Request) -> Principal | None:
    principal = getattr(request.state, "principal", None)
    if principal is None or not principal.is_user or principal.via != "session":
        return None
    return principal


def _context(request: Request) -> dict:
    settings = get_settings()
    principal = _page_principal(request)
    user = None
    if principal is not None:
        user = {"id": principal.user_id, "email": principal.email, "name": principal.name or "",
                "first_name": (principal.name or principal.email or "").split(" ")[0].split("@")[0],
                "role": principal.role, "is_admin": principal.is_admin, "email_verified": principal.email_verified,
                "initials": _initials(principal.name, principal.email)}
    return {
        "current_user": user,
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "path": request.url.path,
        "app_name": settings.app_name,
        "base_url": settings.base_url,
        "contact_email": settings.contact_email,
        "signup_enabled": settings.signup_enabled,
        "hero_3d": settings.hero_3d,
        "version": PIPELINE_VERSION,
        "year": datetime.now(UTC).year,
        "limits": _limits(),
    }


def _initials(name: str | None, email: str | None) -> str:
    words = [w for w in (name or "").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    if words:
        return words[0][:2].upper()
    return (email or "?")[:2].upper()


def _limits() -> dict:
    settings = get_settings()
    return {"jobs_per_month": settings.plan_jobs_per_month, "max_minutes": settings.plan_max_minutes,
            "max_queued_per_user": settings.max_queued_per_user, "max_api_keys": settings.max_api_keys_per_user}


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_context])
templates.env.globals["asset"] = asset
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True


def render(request: Request, name: str, status_code: int = 200, **context) -> Response:
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    _top_up_session(request, response)
    return response


def _top_up_session(request: Request, response: Response) -> None:
    """A page loaded with only a valid refresh cookie (the 15-minute access
    cookie had expired) gets fresh cookies, so the page's own API calls
    don't all start with a 401."""
    principal = _page_principal(request)
    if principal is None or not principal.refresh_only:
        return
    try:
        with session_scope() as db:
            _, issued = sessions.rotate(db, request.cookies.get(REFRESH_COOKIE),
                                        request.headers.get("user-agent", ""),
                                        request.client.host if request.client else "")
            set_session_cookies(response, issued.access_token, issued.expires_in, issued.refresh_token)
    except ServiceError:
        pass  # the page still renders; its API calls will ask the user to log in


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


def safe_next(value: str | None, default: str = "/app") -> str:
    """Only same-site paths: /login?next=//evil.example must not redirect out."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    return value


# ------------------------------------------------------------------ public pages
@router.get("/")
def home(request: Request):
    return render(request, "pages/home.html")


@router.get("/about")
def about(request: Request):
    return render(request, "pages/about.html")


@router.get("/pricing")
def pricing(request: Request):
    return render(request, "pages/pricing.html")


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "pages/privacy.html")


@router.get("/terms")
def terms(request: Request):
    return render(request, "pages/terms.html")


@router.get("/docs")
def docs(request: Request):
    return render(request, "pages/docs.html", reference=_api_reference(request), statuses=STATUS_DOCS,
                  errors=_error_docs(), options=_option_docs())


# ------------------------------------------------------------------ sign-in pages
@router.get("/login")
def login_page(request: Request, next: str | None = None):
    if _page_principal(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "auth/login.html", next=safe_next(next))


@router.get("/signup")
def signup_page(request: Request, next: str | None = None):
    if _page_principal(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "auth/signup.html", next=safe_next(next))


@router.get("/forgot")
def forgot_page(request: Request):
    return render(request, "auth/forgot.html")


@router.get("/reset")
def reset_page(request: Request, token: str = ""):
    with session_scope() as db:
        valid = users.reset_token_is_valid(db, token)
    return render(request, "auth/reset.html", token=token if valid else "", token_valid=valid)


@router.get("/verify")
def verify_page(request: Request, token: str = ""):
    """Opening the link verifies the address (mail scanners opening it first
    is fine: an already-verified account just shows success)."""
    state, email = "invalid", None
    try:
        with session_scope() as db:
            user, newly = users.verify_email(db, token)
            email, name = user.email, user.name
        state = "verified" if newly else "already"
        if newly:
            notifications.send_welcome(email, name)
    except ServiceError as exc:
        state = "expired" if "expired" in exc.message else "invalid"
    return render(request, "auth/verify.html", state=state, email=email)


# ------------------------------------------------------------------ the app
def _require_page_user(request: Request) -> Principal | Response:
    principal = _page_principal(request)
    return principal if principal is not None else _login_redirect(request)


@router.get("/app")
def dashboard(request: Request, job: str | None = None):
    if job:  # links from the old single-page UI: /app?job=<id>
        return RedirectResponse(f"/app/jobs/{quote(job, safe='')}", status_code=301)
    principal = _require_page_user(request)
    if isinstance(principal, Response):
        return principal
    return render(request, "app/dashboard.html", active="dashboard")


@router.get("/app/jobs/{job_id}")
def job_page(request: Request, job_id: str):
    principal = _require_page_user(request)
    if isinstance(principal, Response):
        return principal
    from .. import main  # late: main imports this module

    job = main.job_store.get(job_id) if main.is_valid_job_id(job_id) else None
    if job is None or not main._can_see(job, principal):
        return render(request, "pages/error.html", status_code=404, code=404, title="Job not found",
                      message="This job doesn't exist, was deleted, or belongs to another account.")
    return render(request, "app/job.html", active="dashboard", job_id=job_id, job_title=job.title)


@router.get("/app/keys")
def keys_page(request: Request):
    principal = _require_page_user(request)
    if isinstance(principal, Response):
        return principal
    return render(request, "app/keys.html", active="keys")


@router.get("/app/account")
def account_page(request: Request):
    principal = _require_page_user(request)
    if isinstance(principal, Response):
        return principal
    return render(request, "app/account.html", active="account")


@router.get("/app/admin")
def admin_page(request: Request):
    principal = _require_page_user(request)
    if isinstance(principal, Response):
        return principal
    if not principal.is_admin:
        return render(request, "pages/error.html", status_code=404, code=404, title="Page not found",
                      message="There is nothing at this address.")
    return render(request, "app/admin.html", active="admin")


# ------------------------------------------------------------------ small files
@router.get("/robots.txt")
def robots() -> PlainTextResponse:
    base = get_settings().base_url
    return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /app\nDisallow: /api/\n"
                             f"Sitemap: {base}/sitemap.xml\n")


@router.get("/sitemap.xml")
def sitemap() -> Response:
    base = get_settings().base_url
    pages = ["/", "/about", "/pricing", "/docs", "/signup", "/login", "/privacy", "/terms"]
    urls = "".join(f"<url><loc>{base}{page}</loc></url>" for page in pages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")


@router.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "img" / "favicon.svg", media_type="image/svg+xml")


@router.get("/styles.css")
def legacy_stylesheet() -> RedirectResponse:
    return RedirectResponse(asset("css/site.css"), status_code=301)


# ------------------------------------------------------------------ error pages
def render_error_page(request: Request, status_code: int, detail: str) -> Response | None:
    if status_code == 404:
        return render(request, "pages/error.html", status_code=404, code=404, title="Page not found",
                      message="There is nothing at this address. It may have moved, or the link is mistyped.")
    if status_code >= 500:
        return render(request, "pages/error.html", status_code=status_code, code=status_code,
                      title="Something went wrong on our side",
                      message="The error was logged. Try again in a moment.")
    if status_code in (401, 403):
        return render(request, "pages/error.html", status_code=status_code, code=status_code,
                      title="You can't open this page", message=detail or "Log in with an account that has access.")
    return None


set_html_error_renderer(render_error_page)


# ------------------------------------------------------------------ docs data
def _api_reference(request: Request) -> list[dict]:
    """Endpoint table for /docs, from the live OpenAPI schema."""
    schema = request.app.openapi()
    groups: dict[str, list[dict]] = {}
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            tag = (operation.get("tags") or ["Other"])[0]
            groups.setdefault(tag, []).append({
                "method": method.upper(), "path": path, "summary": operation.get("summary", ""),
                "public": operation.get("security") == [],
            })
    order = ["Jobs", "Zoom", "API keys", "Account", "Accounts", "Admin", "Service"]
    return [{"tag": tag, "endpoints": sorted(groups[tag], key=lambda e: (e["path"], e["method"]))}
            for tag in sorted(groups, key=lambda t: order.index(t) if t in order else len(order))]


STATUS_DOCS = [
    ("queued", "Waiting for its turn (one job renders at a time)."),
    ("waiting_transcript", "Zoom jobs: Zoom is still producing the transcript."),
    ("downloading", "Fetching the recording from Zoom or YouTube."),
    ("transcribing", "Reading the platform transcript (or transcribing it when there is none)."),
    ("deciding", "The AI judges every sentence (progress: sentences judged)."),
    ("building_edl", "Turning the decisions into frame-exact cuts."),
    ("decided", "Finished with decide_only: review the picks, then POST /jobs/{job_id}/render."),
    ("slicing", "Rendering the cleaned meeting."),
    ("rendering_highlights", "Rendering the highlights reel."),
    ("assembling", "Joining reel, title card and meeting into final.mp4."),
    ("rendering_removed", "Rendering the removed-parts video."),
    ("rendering_shorts", "Rendering the vertical shorts."),
    ("reporting", "Writing transcripts, chapters and the PDF report."),
    ("bundling", "Packing bundle.zip."),
    ("done", "Finished. Download the bundle or single outputs."),
    ("failed", "Stopped with an error_code; retry only when retryable is true."),
    ("cancelled", "Cancelled with DELETE ?force=true."),
    ("skipped_desync", "The source's audio and video are out of sync; nothing was cut."),
]
assert {s for s, _ in STATUS_DOCS} == {s.value for s in JobStatus}, "document every JobStatus"

ERROR_DOCS = {
    "unauthorized": "No valid credentials (log in, or send an API key).",
    "insufficient_scope": "The API key lacks a scope this route needs.",
    "email_not_verified": "Verify the account's email address before creating jobs or keys.",
    "session_required": "Only a signed-in person can do this (not an API key).",
    "csrf": "A cookie-authenticated browser request without X-Requested-With: hc-web.",
    "rate_limited": "Too many requests; wait retry_after_s seconds.",
    "validation_error": "The request body or parameters are invalid (detail lists the fields).",
    "not_found": "No such resource, or it belongs to another account.",
    "busy": "The queue is full; retry after retry_after_s.",
    "too_many_jobs": "You already have the maximum number of jobs waiting or running.",
    "plan_limit": "Your plan doesn't allow this (jobs per month, or recording length).",
    "webhook_url_invalid": "callback_url must be https:// and reach a public address.",
    "recording_not_ready": "Zoom is still processing the recording; retry later.",
    "transcript_not_ready": "Zoom hasn't produced the transcript yet (retryable), or there is none.",
    "zoom_auth": "Zoom isn't set up on this server, or refused its credentials.",
    "zoom_not_found": "Zoom doesn't know this recording (deleted?).",
    "zoom_unavailable": "Zoom is having trouble; retry later.",
    "zoom_download_invalid": "The file Zoom sent isn't a valid recording.",
    "source_unsupported": "The recording can't be read (format, corrupt file, no audio/video).",
    "insufficient_disk": "The server is out of disk space; retry later.",
    "llm_quota_exhausted": "The AI provider's quota ran out; retry after retry_after_s.",
    "llm_error": "The AI provider failed.",
    "render_assert_failed": "A rendered file didn't match the edit list (a bug; report it).",
    "timeout": "A step took too long; usually retryable.",
    "interrupted": "The server restarted during the job; resubmit it (or render it again).",
    "cancelled": "The job was cancelled.",
    "internal": "Unexpected error; the job log has details.",
}


def _error_docs() -> list[tuple[str, str]]:
    codes = list(ERROR_DOCS)
    for code in ErrorCode.__args__:
        if code not in ERROR_DOCS:
            codes.append(code)
    return [(code, ERROR_DOCS.get(code, "")) for code in codes]


OPTION_DOCS = {
    "decide_only": "Stop after the AI's picks (status decided) so you can review them; render later.",
    "transitions": "Dissolves between kept parts (default on); false = hard cuts.",
    "highlights_target_s": "Length of the highlights reel in seconds (0-900; default 300).",
    "shorts_count": "How many vertical shorts to make (0-10; default 4).",
    "highlights_criteria": "What counts as a highlight, in your words (overrides the default).",
    "shorts_criteria": "What makes a good short, in your words.",
    "cut_silence": "Shorten pauses where nobody speaks (default on).",
}


def _option_docs() -> list[tuple[str, str, str]]:
    rows = []
    for name, field in JobOptions.model_fields.items():
        annotation = str(field.annotation).replace("typing.", "").replace("<class '", "").replace("'>", "")
        rows.append((name, annotation, OPTION_DOCS.get(name, "")))
    return rows
