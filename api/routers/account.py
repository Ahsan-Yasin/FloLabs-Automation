"""/api/v1: account usage, webhook test, account deletion, public config."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.config import get_settings
from db.session import get_db
from ingest import zoom
from services import jobs_index, passwords, users, webhooks
from services.errors import ServiceError, rate_limited
from services.scopes import JOBS_READ
from services.usercache import invalidate_user

from ..auth import Principal, clear_session_cookies, require, require_user
from ..jobs import job_store
from ..queue import delete_job_files
from ..ratelimit import limiter
from .auth import load_user

router = APIRouter(tags=["Account"])

DB = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[Principal, Depends(require_user)]
Reader = Annotated[Principal, Depends(require(JOBS_READ))]


class WebhookTestIn(BaseModel):
    url: str = Field(max_length=2048, examples=["https://n8n.example.com/webhook/highlight-cutter"])


class DeleteAccountIn(BaseModel):
    password: str = Field(max_length=256)
    confirm_email: str = Field(max_length=320, description="Type the account's email address to confirm")


def plan_limits() -> dict:
    settings = get_settings()
    return {
        "jobs_per_month": settings.plan_jobs_per_month or None,
        "max_minutes": settings.plan_max_minutes or None,
        "max_queued_per_user": settings.max_queued_per_user or None,
        "max_api_keys": settings.max_api_keys_per_user or None,
    }


@router.get("/public/config", summary="What the site allows (no login needed)")
def public_config() -> dict:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "signup_enabled": settings.signup_enabled,
        "signup_domains": sorted(settings.allowed_signup_domain_set),
        "limits": plan_limits(),
        "zoom_configured": zoom.is_configured(settings),
        "legacy_api_enabled": settings.legacy_api_enabled,
        "contact_email": settings.contact_email or None,
    }


@router.get("/account/usage", summary="Your jobs, minutes processed and limits")
def usage(principal: Reader) -> dict:
    if not principal.user_id:
        raise ServiceError("the operator key has no account", code="session_required", status=403)
    return {**jobs_index.usage(principal.user_id), "limits": plan_limits()}


@router.post("/webhooks/test", summary="Send a signed test event (ping) to a URL now")
def test_webhook(body: WebhookTestIn, principal: CurrentUser) -> dict:
    wait = limiter.hit("webhook_test", principal.user_id)
    if wait:
        raise rate_limited(int(wait))
    return webhooks.send_test(body.url, principal.user_id)


@router.delete("/account", summary="Delete your account, its API keys and its jobs")
def delete_account(body: DeleteAccountIn, principal: CurrentUser, db: DB):
    user = load_user(db, principal)
    if not passwords.verify_password(user.password_hash, body.password):
        raise ServiceError("the password is wrong", code="invalid_credentials", status=403)
    if body.confirm_email.strip().lower() != user.email:
        raise ServiceError("type your account's email address to confirm", code="validation_error", status=422)
    if user.is_admin and users.admin_count(db) <= 1:
        raise ServiceError("you are the last admin; make someone else an admin first", code="last_admin",
                           status=409)
    if jobs_index.count_active(user.id):
        raise ServiceError("you have jobs waiting or running; cancel them or let them finish first",
                           code="jobs_running", status=409)
    job_ids = jobs_index.user_job_ids(user.id)
    for job_id in job_ids:  # the recordings and outputs go first; the account last
        delete_job_files(job_store, job_id)
    user_id = user.id
    db.delete(user)
    db.commit()
    invalidate_user(user_id)
    response = JSONResponse({"deleted": True, "jobs_deleted": len(job_ids)})
    clear_session_cookies(response)
    return response
