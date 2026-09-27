"""Signed job webhooks (plan.MD §7.5).

A job created with a callback_url gets one POST when it reaches a finished
status (job.done, job.failed, job.decided, job.cancelled). The body is JSON;
the header X-HC-Signature: t=<unix seconds>,v1=<hex> carries
HMAC-SHA256(owner's webhook secret, "<t>." + body), like Stripe's, so the
receiver can check the call is genuine and fresh.

Deliveries are rows in webhook_deliveries, so they survive a restart. A
background thread sends due rows; a non-2xx answer or a network error is
retried after 1, 5 and 30 minutes, then given up (the job page shows it).

Callback URLs must be https:// and resolve to public addresses (checked when
the job is created and again before every attempt; redirects are not
followed), so a webhook can't be aimed at this server's own network.
WEBHOOK_ALLOW_HTTP / WEBHOOK_ALLOW_PRIVATE relax that for local receivers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import func, select

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord
from core.version import PIPELINE_VERSION
from db.base import new_id
from db.models import User, WebhookDelivery
from db.session import session_scope

from .errors import ServiceError

logger = get_logger(__name__)

EVENT_BY_STATUS = {"done": "job.done", "failed": "job.failed", "skipped_desync": "job.failed",
                   "decided": "job.decided", "cancelled": "job.cancelled"}
RETRY_DELAYS_S = (60, 300, 1800)
USER_AGENT = f"HighlightCutter-Webhooks/{PIPELINE_VERSION}"
MAX_URL_LENGTH = 2048
SIGNATURE_TOLERANCE_S = 300

# Fields of JobRecord that are server file paths: never sent to a receiver.
PATH_FIELDS = ("source_path", "native_transcript_path", "output_video_path", "edl_path", "removed_edl_path",
               "render_manifest_path", "transcript_path", "clean_transcript_path", "decisions_path",
               "selection_path", "chapters_path", "bundle_path")


def _invalid(message: str) -> ServiceError:
    return ServiceError(message, code="webhook_url_invalid", status=422)


# ------------------------------------------------------------------ URLs
def validate_callback_url(url: str | None) -> str:
    settings = get_settings()
    url = (url or "").strip()
    if not url:
        raise _invalid("callback_url is empty")
    if len(url) > MAX_URL_LENGTH:
        raise _invalid(f"callback_url is longer than {MAX_URL_LENGTH} characters")
    parts = urlsplit(url)
    if parts.scheme == "http" and not settings.webhook_allow_http:
        raise _invalid("callback_url must be an https:// address")
    if parts.scheme not in ("https", "http"):
        raise _invalid("callback_url must be an https:// address")
    if not parts.hostname:
        raise _invalid("callback_url has no host")
    if parts.username or parts.password:
        raise _invalid("callback_url must not contain a user name or password; put a token in the path or query")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise _invalid("callback_url has an invalid port") from exc
    if not settings.webhook_allow_private:
        _require_public(parts.hostname, port)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _require_public(host: str, port: int) -> None:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise _invalid(f"callback_url host {host!r} does not resolve") from exc
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            raise _invalid("callback_url points at a private, local or reserved address")


# ------------------------------------------------------------------ signing
def sign(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(secret: str, body: bytes, header: str, *, now: float | None = None,
                     tolerance_s: int = SIGNATURE_TOLERANCE_S) -> bool:
    """What a receiver does (also shown in the docs)."""
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(fields["t"])
    except (ValueError, KeyError):
        return False
    if abs((now if now is not None else time.time()) - timestamp) > tolerance_s:
        return False
    expected = sign(secret, body, timestamp).split("v1=", 1)[1]
    return hmac.compare_digest(expected, fields.get("v1", ""))


# ------------------------------------------------------------------ payloads
def links_for(job: JobRecord) -> dict[str, str]:
    base = get_settings().base_url
    api = f"{base}/api/v1/jobs/{job.job_id}"
    links = {"job": api, "app": f"{base}/app/jobs/{job.job_id}", "log": f"{api}/log"}
    if job.bundle_path:
        links["bundle"] = f"{api}/bundle"
    if job.output_video_path:
        links["video"] = f"{api}/video"
    if job.selection_path:
        links["selection"] = f"{api}/selection"
    if job.chapters_path:
        links["chapters"] = f"{api}/chapters"
    return links


def public_job(job: JobRecord) -> dict:
    """The job as API clients and receivers see it: no server file paths."""
    data = job.model_dump(mode="json", exclude=set(PATH_FIELDS))
    for name, info in (data.get("artifacts") or {}).items():
        info.pop("path", None)
        info["url"] = f"{get_settings().base_url}/api/v1/jobs/{job.job_id}/artifacts/{name}"
    data["links"] = links_for(job)
    return data


# ------------------------------------------------------------------ queue + worker
_cv = threading.Condition()
_thread: threading.Thread | None = None
_stopping = False
_autostart = True


def enqueue(job: JobRecord, url: str, user_id: str | None, event: str | None = None) -> str | None:
    event = event or EVENT_BY_STATUS.get(job.status.value)
    if not event:
        return None
    delivery_id = new_id()
    now = datetime.now(UTC)
    body = {"id": delivery_id, "event": event, "created_at": now.isoformat(), "job": public_job(job)}
    with session_scope() as db:
        db.add(WebhookDelivery(id=delivery_id, job_id=job.job_id, user_id=user_id, url=url, event=event,
                               payload=json.dumps(body, separators=(",", ":")), next_attempt_at=now))
    wake()
    return delivery_id


def start() -> None:
    global _thread, _stopping
    if not _autostart or not get_settings().webhooks_enabled:
        return
    with _cv:
        _stopping = False
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="webhooks", daemon=True)
            _thread.start()


def wake() -> None:
    start()
    with _cv:
        _cv.notify_all()


def stop(timeout: float = 5.0) -> None:
    global _stopping
    with _cv:
        _stopping = True
        _cv.notify_all()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def _loop() -> None:
    while True:
        with _cv:
            if _stopping:
                return
        try:
            wait_s = process_due()
        except Exception:
            logger.exception("webhook worker: unexpected error")
            wait_s = 30.0
        with _cv:
            if _stopping:
                return
            _cv.wait(timeout=min(max(wait_s, 1.0), 60.0))


def process_due(now: datetime | None = None) -> float:
    """Send every delivery that is due; seconds until the next one."""
    now = now or datetime.now(UTC)
    with session_scope() as db:
        due = list(db.scalars(select(WebhookDelivery.id).where(
            WebhookDelivery.delivered_at.is_(None), WebhookDelivery.next_attempt_at.is_not(None),
            WebhookDelivery.next_attempt_at <= now).order_by(WebhookDelivery.next_attempt_at).limit(20)))
    for delivery_id in due:
        _attempt(delivery_id)
    with session_scope() as db:
        upcoming = db.scalar(select(func.min(WebhookDelivery.next_attempt_at)).where(
            WebhookDelivery.delivered_at.is_(None), WebhookDelivery.next_attempt_at.is_not(None)))
    if upcoming is None:
        return 60.0
    return max(0.0, (upcoming - datetime.now(UTC)).total_seconds())


def _secret_for(db, user_id: str | None) -> str:
    user = db.get(User, user_id) if user_id else None
    return user.webhook_secret if user is not None else ""


def _attempt(delivery_id: str) -> None:
    with session_scope() as db:
        delivery = db.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.delivered_at is not None:
            return
        url, event, payload = delivery.url, delivery.event, delivery.payload
        secret = _secret_for(db, delivery.user_id)
    status, error = post(url, event, delivery_id, payload.encode(), secret)
    now = datetime.now(UTC)
    with session_scope() as db:
        delivery = db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return
        delivery.attempt += 1
        delivery.status_code = status
        if status is not None and 200 <= status < 300:
            delivery.delivered_at = now
            delivery.next_attempt_at = None
            delivery.error = None
        else:
            delivery.error = (error or f"HTTP {status}")[:500]
            if delivery.attempt > len(RETRY_DELAYS_S):
                delivery.next_attempt_at = None  # given up
                logger.warning("webhook %s for job %s given up after %d attempts: %s", event, delivery.job_id,
                               delivery.attempt, delivery.error)
            else:
                delivery.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS_S[delivery.attempt - 1])


def post(url: str, event: str, delivery_id: str, body: bytes, secret: str) -> tuple[int | None, str | None]:
    """One POST. (status code or None, error message or None)."""
    try:
        validate_callback_url(url)  # the address may resolve differently now
    except ServiceError as exc:
        return None, exc.message
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "X-HC-Event": event,
               "X-HC-Delivery": delivery_id}
    if secret:
        headers["X-HC-Signature"] = sign(secret, body, int(time.time()))
    try:
        response = httpx.post(url, content=body, headers=headers, timeout=get_settings().webhook_timeout_s,
                              follow_redirects=False)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300]
    if 200 <= response.status_code < 300:
        return response.status_code, None
    return response.status_code, f"HTTP {response.status_code}"


def send_test(url: str, user_id: str) -> dict:
    """POST a signed `ping` right now (the account page's Test button)."""
    url = validate_callback_url(url)
    delivery_id = new_id()
    body = json.dumps({"id": delivery_id, "event": "ping", "created_at": datetime.now(UTC).isoformat(),
                       "message": "Webhook test from Highlight Cutter"}, separators=(",", ":")).encode()
    with session_scope() as db:
        secret = _secret_for(db, user_id)
    status, error = post(url, "ping", delivery_id, body, secret)
    return {"delivered": status is not None and 200 <= status < 300, "status_code": status, "error": error}


def list_for_job(job_id: str) -> list[dict]:
    with session_scope() as db:
        rows = db.scalars(select(WebhookDelivery).where(WebhookDelivery.job_id == job_id)
                          .order_by(WebhookDelivery.created_at)).all()
        return [{
            "id": row.id, "event": row.event, "url": row.url, "attempts": row.attempt,
            "status_code": row.status_code, "error": row.error,
            "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
            "state": "delivered" if row.delivered_at else ("pending" if row.next_attempt_at else "failed"),
        } for row in rows]


def reset_for_tests() -> None:
    """Tests drive deliveries with process_due(); no background thread."""
    global _autostart
    stop(timeout=2.0)
    _autostart = False
