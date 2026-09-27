"""/api/v1/admin: users, jobs and service stats across the workspace."""

from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import ROLE_ADMIN, ROLE_USER, ApiKey, JobIndex, User
from db.session import get_db
from services import jobs_index, users
from services.errors import ServiceError, not_found
from services.usercache import invalidate_user

from ..auth import Principal, require_admin

router = APIRouter(prefix="/admin", tags=["Admin"])

DB = Annotated[Session, Depends(get_db)]
Admin = Annotated[Principal, Depends(require_admin)]


class UserPatchIn(BaseModel):
    role: str | None = None
    is_active: bool | None = None


def _user_row(user: User, jobs: int) -> dict:
    return {
        "id": user.id, "email": user.email, "name": user.name, "role": user.role, "is_active": user.is_active,
        "email_verified": user.email_verified_at is not None,
        "created_at": user.created_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "jobs": jobs,
    }


@router.get("/users", summary="Every account (search by email or name)")
def list_users(_admin: Admin, db: DB, q: str = "", limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0)) -> dict:
    query = select(User)
    count = select(func.count()).select_from(User)
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        query = query.where(or_(func.lower(User.email).like(needle), func.lower(User.name).like(needle)))
        count = count.where(or_(func.lower(User.email).like(needle), func.lower(User.name).like(needle)))
    rows = db.scalars(query.order_by(User.created_at.desc()).limit(limit).offset(offset)).all()
    counts = dict(db.execute(select(JobIndex.user_id, func.count()).where(
        JobIndex.user_id.in_([u.id for u in rows])).group_by(JobIndex.user_id)).all())
    return {"items": [_user_row(u, counts.get(u.id, 0)) for u in rows], "total": db.scalar(count) or 0}


@router.patch("/users/{user_id}", summary="Change a user's role or block/unblock them")
def update_user(user_id: str, body: UserPatchIn, _admin: Admin, db: DB) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise not_found("no such user")
    if body.role is not None:
        if body.role not in (ROLE_USER, ROLE_ADMIN):
            raise ServiceError("role must be user or admin", code="validation_error", status=422)
        if body.role == ROLE_USER and user.role == ROLE_ADMIN and users.admin_count(db) <= 1:
            raise ServiceError("that is the last admin", code="last_admin", status=409)
        user.role = body.role
    if body.is_active is not None and body.is_active != user.is_active:
        if not body.is_active and user.role == ROLE_ADMIN and users.admin_count(db) <= 1:
            raise ServiceError("that is the last active admin", code="last_admin", status=409)
        user.is_active = body.is_active
        if not body.is_active:
            users.logout_everywhere(db, user)
    invalidate_user(user.id)
    db.commit()
    jobs = db.scalar(select(func.count()).select_from(JobIndex).where(JobIndex.user_id == user.id)) or 0
    return _user_row(user, jobs)


@router.get("/jobs", summary="Every job, newest first")
def list_all_jobs(_admin: Admin, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  status: str | None = None) -> dict:
    items, total = jobs_index.list_jobs(None, limit=limit, offset=offset, status=status, everyone=True)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/stats", summary="Accounts, jobs and the queue at a glance")
def stats(_admin: Admin, db: DB) -> dict:
    from .. import main  # late: main imports this module

    settings = get_settings()
    by_status = dict(db.execute(select(JobIndex.status, func.count()).group_by(JobIndex.status)).all())
    try:
        disk_free = shutil.disk_usage(settings.storage_dir).free
    except OSError:
        disk_free = None
    return {
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
        "verified_users": db.scalar(select(func.count()).select_from(User)
                                    .where(User.email_verified_at.is_not(None))) or 0,
        "admins": users.admin_count(db),
        "active_api_keys": db.scalar(select(func.count()).select_from(ApiKey)
                                     .where(ApiKey.revoked_at.is_(None))) or 0,
        "jobs_by_status": by_status,
        "queue": {"running_job_id": main.job_queue.running_job_id, "pending": main.job_queue.pending_ids(),
                  "depth": main.job_queue.depth(), "max_depth": settings.max_queue_depth},
        "disk_free_bytes": disk_free,
    }
