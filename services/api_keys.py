"""Per-user API keys for automations (n8n, Zapier, Make, scripts), plan.MD §7.1.

Format: hc_live_<8-char prefix>_<32-char secret>. The prefix identifies the
key (and is shown in the UI); the database stores only the SHA-256 of the
whole key, so a leaked database does not leak usable keys. The full key is
shown once, when it is created."""

from __future__ import annotations

import hmac
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.config import get_settings
from db.base import new_id
from db.models import ApiKey, User
from db.session import session_scope

from .errors import ServiceError, not_found
from .scopes import ALL_SCOPES
from .tokens import hash_token
from .usercache import UserSnapshot, get_user_snapshot

KEY_PREFIX = "hc_live_"
KEY_RE = re.compile(r"^hc_live_([0-9A-Za-z]{8})_([A-Za-z0-9_-]{32})$")
_ALPHABET = string.digits + string.ascii_letters
LAST_USED_EVERY_S = 60.0


@dataclass(frozen=True)
class KeyIdentity:
    key_id: str
    user: UserSnapshot
    scopes: frozenset[str]


def generate() -> tuple[str, str, str]:
    """(full key, prefix, SHA-256 of the full key)."""
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    full = f"{KEY_PREFIX}{prefix}_{secrets.token_urlsafe(24)}"
    return full, prefix, hash_token(full)


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_scopes(scopes: list[str] | None) -> list[str]:
    if not scopes:
        return sorted(ALL_SCOPES)
    unknown = sorted(set(scopes) - ALL_SCOPES)
    if unknown:
        raise ServiceError(f"unknown scope(s): {', '.join(unknown)}; use {', '.join(sorted(ALL_SCOPES))}",
                           code="validation_error", status=422)
    return sorted(set(scopes))


def active_count(db: Session, user_id: str) -> int:
    now = _now()
    return db.scalar(select(func.count()).select_from(ApiKey).where(
        ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None),
        (ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now),
    )) or 0


def create(db: Session, user: User, name: str, scopes: list[str] | None = None,
           expires_in_days: int | None = None) -> tuple[ApiKey, str]:
    if user.email_verified_at is None:
        raise ServiceError("verify your email address before creating API keys", code="email_not_verified",
                           status=403)
    name = " ".join((name or "").split())[:80]
    if not name:
        raise ServiceError("give the key a name (e.g. \"n8n production\")", code="validation_error", status=422)
    if expires_in_days is not None and not (1 <= expires_in_days <= 3650):
        raise ServiceError("expires_in_days must be between 1 and 3650", code="validation_error", status=422)
    limit = get_settings().max_api_keys_per_user
    if limit and active_count(db, user.id) >= limit:
        raise ServiceError(f"you already have {limit} active keys; revoke one first", code="too_many_keys",
                           status=409)
    clean = _clean_scopes(scopes)
    for _ in range(5):  # an 8-character prefix collision is astronomically rare, but handle it
        full, prefix, digest = generate()
        key = ApiKey(id=new_id(), user_id=user.id, name=name, prefix=prefix, key_hash=digest,
                     expires_at=_now() + timedelta(days=expires_in_days) if expires_in_days else None)
        key.scopes = clean
        try:
            with db.begin_nested():
                db.add(key)
                db.flush()
        except IntegrityError:
            continue
        return key, full
    raise ServiceError("could not create a key; try again", code="internal", status=500, retryable=True)


def list_for_user(db: Session, user_id: str, include_revoked: bool = False) -> list[ApiKey]:
    query = select(ApiKey).where(ApiKey.user_id == user_id)
    if not include_revoked:
        query = query.where(ApiKey.revoked_at.is_(None))
    return list(db.scalars(query.order_by(ApiKey.created_at.desc())).all())


def _own_key(db: Session, user_id: str, key_id: str) -> ApiKey:
    key = db.get(ApiKey, key_id)
    if key is None or key.user_id != user_id or key.revoked_at is not None:
        raise not_found("no such API key")
    return key


def revoke(db: Session, user_id: str, key_id: str) -> ApiKey:
    key = _own_key(db, user_id, key_id)
    key.revoked_at = _now()
    return key


def rotate(db: Session, user: User, key_id: str) -> tuple[ApiKey, str]:
    """Revoke a key and create its replacement (same name and scopes)."""
    old = _own_key(db, user.id, key_id)
    # scopes that no longer exist (zoom:read) are dropped. A key left with none
    # is refused: an empty list would mean "every scope" to create().
    scopes = [s for s in old.scopes if s in ALL_SCOPES]
    if not scopes:
        raise ServiceError("this key has no scopes left (zoom:read was removed); create a new key instead",
                           code="validation_error", status=422)
    old.revoked_at = _now()
    remaining_days = None
    if old.expires_at is not None:
        remaining_days = max(1, (old.expires_at - _now()).days)
    db.flush()
    return create(db, user, old.name, scopes, remaining_days)


def revoke_all(db: Session, user_id: str) -> None:
    db.execute(update(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
               .values(revoked_at=_now()))


# ------------------------------------------------------------------ authenticate
_last_used_lock = threading.Lock()
_last_used_written: dict[str, float] = {}


def authenticate(raw: str) -> KeyIdentity | None:
    """The key's owner and scopes when `raw` is a valid, unrevoked, unexpired
    key of an active user; else None."""
    match = KEY_RE.match(raw or "")
    if not match:
        return None
    digest = hash_token(raw)
    now = _now()
    with session_scope() as db:
        key = db.scalar(select(ApiKey).where(ApiKey.prefix == match.group(1)))
        if key is None or not hmac.compare_digest(key.key_hash, digest):
            return None
        if key.revoked_at is not None or (key.expires_at is not None and key.expires_at <= now):
            return None
        key_id, user_id, scopes = key.id, key.user_id, frozenset(key.scopes) & ALL_SCOPES
        if _should_write_last_used(key_id):
            key.last_used_at = now
    user = get_user_snapshot(user_id)
    if user is None or not user.is_active:
        return None
    return KeyIdentity(key_id=key_id, user=user, scopes=scopes)


def _should_write_last_used(key_id: str) -> bool:
    now = time.monotonic()
    with _last_used_lock:
        last = _last_used_written.get(key_id)
        if last is not None and now - last < LAST_USED_EVERY_S:
            return False
        _last_used_written[key_id] = now
        return True


def reset_for_tests() -> None:
    with _last_used_lock:
        _last_used_written.clear()
