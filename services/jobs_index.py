"""The jobs index: who owns which job, and a mirror of its status for fast
per-user listing (plan.MD A8). storage/jobs/{id}/job.json stays the truth.

api/jobs.JobStore calls sync() after every save of a job, so the index
follows the pipeline without the pipeline knowing about it. When a job
reaches a finished status, sync() announces it: the owner's "job finished"
email and the job's webhook (callback_url)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select, update

from core.config import get_settings
from core.logging import get_logger
from core.models import TERMINAL_STATUSES, JobRecord, JobStatus
from db.models import ROLE_ADMIN, JobIndex, User
from db.session import session_scope

logger = get_logger(__name__)

# Statuses that end a run and are announced (email + webhook).
ANNOUNCED = frozenset({JobStatus.DONE, JobStatus.FAILED, JobStatus.DECIDED, JobStatus.CANCELLED,
                       JobStatus.SKIPPED_DESYNC})
ACTIVE_STATUSES = tuple(s.value for s in JobStatus if s not in TERMINAL_STATUSES)

_lock = threading.Lock()
# job_id -> the fields last written, so heartbeats and progress ticks that
# change nothing indexed never touch the database
_last: dict[str, tuple] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def source_kind_of(job: JobRecord) -> str:
    if job.source_url:
        return "youtube"
    return "upload"


def _fingerprint(job: JobRecord) -> tuple:
    return (job.status.value, job.title, job.error_code, job.bundle_bytes, job.source_duration_s, job.owner_id)


def record_new(job: JobRecord, *, created_via: str, api_key_id: str | None = None) -> None:
    """Index a job the API is about to create (before JobStore.create)."""
    now = _now()
    with session_scope() as db:
        row = db.get(JobIndex, job.job_id)
        if row is None:
            row = JobIndex(job_id=job.job_id, created_at=job.created_at or now)
            db.add(row)
        row.user_id = job.owner_id
        row.status = job.status.value
        row.title = (job.title or "")[:200]
        row.source_kind = source_kind_of(job)
        row.created_via = created_via
        row.api_key_id = api_key_id
        row.callback_url = job.callback_url
        row.updated_at = now
    with _lock:
        _last[job.job_id] = _fingerprint(job)


def sync(job: JobRecord) -> None:
    """JobStore hook. Never raises: the index must never fail a job."""
    try:
        _sync(job)
    except Exception:
        logger.exception("jobs index: could not sync job %s", job.job_id)


def _sync(job: JobRecord) -> None:
    fingerprint = _fingerprint(job)
    with _lock:
        if _last.get(job.job_id) == fingerprint:
            return
        _last[job.job_id] = fingerprint
    now = _now()
    announce = None
    with session_scope() as db:
        row = db.get(JobIndex, job.job_id)
        previous = row.status if row is not None else None
        if row is None:  # a job the API didn't create (tools, tests, old folders)
            row = JobIndex(job_id=job.job_id, user_id=job.owner_id, created_at=job.created_at or now,
                           source_kind=source_kind_of(job), created_via="ops", callback_url=job.callback_url)
            db.add(row)
        row.status = job.status.value
        row.title = (job.title or "")[:200]
        row.error_code = job.error_code
        row.bundle_bytes = job.bundle_bytes
        row.source_duration_s = job.source_duration_s
        if job.owner_id and row.user_id is None:
            row.user_id = job.owner_id
        row.updated_at = now
        if job.status in TERMINAL_STATUSES:
            if previous != job.status.value:
                row.finished_at = now
                if job.status in ANNOUNCED and previous is not None:
                    announce = (row.user_id, row.callback_url)
        else:
            row.finished_at = None
    if announce is not None:
        _announce(job, *announce)


def _announce(job: JobRecord, user_id: str | None, callback_url: str | None) -> None:
    from . import (  # late: webhooks imports this module's helpers
        notifications,
        webhooks,
    )

    if callback_url and get_settings().webhooks_enabled:
        try:
            webhooks.enqueue(job, callback_url, user_id)
        except Exception:
            logger.exception("could not queue the webhook for job %s", job.job_id)
    if not user_id or job.status == JobStatus.CANCELLED:
        return
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active or not user.notify_on_done:
            return
        to, name = user.email, user.name
    outputs = [name for name, info in job.artifacts.items() if info.status == "ok"]
    status = "failed" if job.status == JobStatus.SKIPPED_DESYNC else job.status.value
    notifications.send_job_finished(to, name, job_id=job.job_id, title=job.title, status=status,
                                    error_code=job.error_code, error=job.error, retryable=job.retryable,
                                    outputs=outputs)


def delete_entry(job_id: str) -> None:
    try:
        with session_scope() as db:
            db.execute(delete(JobIndex).where(JobIndex.job_id == job_id))
    except Exception:
        logger.exception("jobs index: could not remove job %s", job_id)
    with _lock:
        _last.pop(job_id, None)


# ------------------------------------------------------------------ queries
def owner_of(job_id: str) -> str | None:
    with session_scope() as db:
        row = db.get(JobIndex, job_id)
        return row.user_id if row is not None else None


def _filtered(query, status: str | None):
    if status == "active":
        return query.where(JobIndex.status.in_(ACTIVE_STATUSES))
    if status:
        return query.where(JobIndex.status == status)
    return query


def list_jobs(user_id: str | None, *, limit: int = 50, offset: int = 0, status: str | None = None,
              everyone: bool = False) -> tuple[list[dict], int]:
    """(rows as dicts, total) newest first; one user's jobs, or every job."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with session_scope() as db:
        query = select(JobIndex, User.email).outerjoin(User, User.id == JobIndex.user_id)
        count = select(func.count()).select_from(JobIndex)
        if not everyone:
            query = query.where(JobIndex.user_id == user_id)
            count = count.where(JobIndex.user_id == user_id)
        query = _filtered(query, status)
        count = _filtered(count, status)
        total = db.scalar(count) or 0
        rows = db.execute(query.order_by(JobIndex.created_at.desc()).limit(limit).offset(offset)).all()
        return [_row_dict(row, owner_email) for row, owner_email in rows], total


def _row_dict(row: JobIndex, owner_email: str | None) -> dict:
    return {
        "job_id": row.job_id,
        "status": row.status,
        "title": row.title,
        "source_kind": row.source_kind,
        "created_via": row.created_via,
        "error_code": row.error_code,
        "source_duration_s": row.source_duration_s,
        "bundle_bytes": row.bundle_bytes,
        "has_callback": bool(row.callback_url),
        "owner_id": row.user_id,
        "owner_email": owner_email,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def count_active(user_id: str) -> int:
    with session_scope() as db:
        return db.scalar(select(func.count()).select_from(JobIndex).where(
            JobIndex.user_id == user_id, JobIndex.status.in_(ACTIVE_STATUSES))) or 0


def month_start(now: datetime | None = None) -> datetime:
    now = now or _now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def count_created_since(user_id: str, since: datetime) -> int:
    with session_scope() as db:
        return db.scalar(select(func.count()).select_from(JobIndex).where(
            JobIndex.user_id == user_id, JobIndex.created_at >= since)) or 0


def usage(user_id: str) -> dict:
    with session_scope() as db:
        base = select(func.count()).select_from(JobIndex).where(JobIndex.user_id == user_id)
        total = db.scalar(base) or 0
        this_month = db.scalar(base.where(JobIndex.created_at >= month_start())) or 0
        active = db.scalar(base.where(JobIndex.status.in_(ACTIVE_STATUSES))) or 0
        done = JobIndex.status == JobStatus.DONE.value
        seconds = db.scalar(select(func.coalesce(func.sum(JobIndex.source_duration_s), 0.0))
                            .where(JobIndex.user_id == user_id, done)) or 0.0
        stored = db.scalar(select(func.coalesce(func.sum(JobIndex.bundle_bytes), 0))
                           .where(JobIndex.user_id == user_id, done)) or 0
    return {"jobs_total": total, "jobs_this_month": this_month, "jobs_active": active,
            "minutes_processed": round(seconds / 60, 1), "bundle_bytes": int(stored)}


def user_job_ids(user_id: str) -> list[str]:
    with session_scope() as db:
        return list(db.scalars(select(JobIndex.job_id).where(JobIndex.user_id == user_id)))


# ------------------------------------------------------------------ maintenance
def index_orphans() -> int:
    """Index job folders the database doesn't know (jobs from before
    accounts existed, or created by tools). They go to the first admin, or
    stay unowned (visible to admins only) when there is none yet. Also hands
    unowned rows to that admin."""
    jobs_dir = get_settings().jobs_dir
    if not jobs_dir.exists():
        return 0
    with session_scope() as db:
        admin_id = db.scalar(select(User.id).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
                             .order_by(User.created_at).limit(1))
        known = set(db.scalars(select(JobIndex.job_id)))
        added = 0
        for path in sorted(jobs_dir.glob("*/job.json")):
            job_id = path.parent.name
            if job_id in known:
                continue
            job = _read_job(path)
            if job is None:
                continue
            created = job.created_at or datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            db.add(JobIndex(job_id=job_id, user_id=job.owner_id or admin_id, status=job.status.value,
                            title=(job.title or "")[:200], source_kind=source_kind_of(job), created_via="ops",
                            error_code=job.error_code, bundle_bytes=job.bundle_bytes,
                            source_duration_s=job.source_duration_s, created_at=created, updated_at=created))
            added += 1
        if admin_id:
            db.execute(update(JobIndex).where(JobIndex.user_id.is_(None)).values(user_id=admin_id))
    return added


def _read_job(path: Path) -> JobRecord | None:
    try:
        return JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 — one bad folder must not stop indexing
        logger.warning("jobs index: skipping unreadable %s: %s", path, exc)
        return None


def reset_for_tests() -> None:
    with _lock:
        _last.clear()
