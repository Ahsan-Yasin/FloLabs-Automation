"""/api/v1/keys: API keys for automations (plan.MD §7.1). Managed with a
signed-in session only: a key can never mint or revoke keys."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.models import ApiKey
from db.session import get_db
from services import api_keys
from services.errors import rate_limited
from services.scopes import SCOPE_DESCRIPTIONS

from ..auth import Principal, require_user
from ..ratelimit import limiter
from .auth import load_user

router = APIRouter(prefix="/keys", tags=["API keys"])

DB = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[Principal, Depends(require_user)]


class KeyCreateIn(BaseModel):
    name: str = Field(max_length=80, examples=["n8n production"])
    scopes: list[str] | None = Field(default=None, description="Default: every scope",
                                     examples=[["jobs:read", "jobs:write"]])
    expires_in_days: int | None = Field(default=None, ge=1, le=3650, description="Default: never expires")


class KeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    display: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class NewKeyOut(KeyOut):
    key: str = Field(description="The full key. Shown only now: store it in your automation's credentials.")


def key_out(key: ApiKey) -> dict:
    return KeyOut(id=key.id, name=key.name, prefix=key.prefix, display=f"{api_keys.KEY_PREFIX}{key.prefix}_…",
                  scopes=key.scopes, created_at=key.created_at, last_used_at=key.last_used_at,
                  expires_at=key.expires_at, revoked_at=key.revoked_at).model_dump(mode="json")


@router.get("", summary="Your API keys (never the secret part)")
def list_keys(principal: CurrentUser, db: DB) -> dict:
    return {"items": [key_out(key) for key in api_keys.list_for_user(db, principal.user_id)],
            "scopes": SCOPE_DESCRIPTIONS}


@router.post("", status_code=201, response_model=NewKeyOut, summary="Create a key (the full key is shown once)")
def create_key(body: KeyCreateIn, principal: CurrentUser, db: DB):
    wait = limiter.hit("keys_create", principal.user_id)
    if wait:
        raise rate_limited(int(wait))
    key, full = api_keys.create(db, load_user(db, principal), body.name, body.scopes, body.expires_in_days)
    db.commit()
    return {**key_out(key), "key": full}


@router.delete("/{key_id}", summary="Revoke a key (automations using it stop working at once)")
def revoke_key(key_id: str, principal: CurrentUser, db: DB) -> dict:
    key = api_keys.revoke(db, principal.user_id, key_id)
    db.commit()
    return {"revoked": True, "id": key.id}


@router.post("/{key_id}/rotate", status_code=201, response_model=NewKeyOut,
             summary="Replace a key with a new one (same name and scopes)")
def rotate_key(key_id: str, principal: CurrentUser, db: DB):
    key, full = api_keys.rotate(db, load_user(db, principal), key_id)
    db.commit()
    return {**key_out(key), "key": full}
