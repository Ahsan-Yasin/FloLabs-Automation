"""Tables of the product layer (plan.MD §4). Ids are uuid4 hex strings like
job ids; every timestamp is timezone-aware UTC (db.types.UTCDateTime)."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, new_id, new_webhook_secret, utcnow
from .types import UTCDateTime

ROLE_USER = "user"
ROLE_ADMIN = "admin"

TOKEN_VERIFY = "verify"
TOKEN_RESET = "reset"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # stored lower-cased and trimmed (services.users.normalize_email)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(16), default=ROLE_USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # default host for the Zoom recordings picker (the workspace Zoom app is shared)
    zoom_host_email: Mapped[str | None] = mapped_column(String(320), default=None)
    # signs this user's job webhooks (X-HC-Signature); rotatable
    webhook_secret: Mapped[str] = mapped_column(String(128), default=new_webhook_secret)
    notify_on_done: Mapped[bool] = mapped_column(Boolean, default=True)
    # access tokens issued before this moment are rejected (password change,
    # reset, "log out everywhere")
    tokens_valid_after: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user", cascade="all, delete-orphan",
                                                  passive_deletes=True)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None


class EmailToken(Base):
    """One-time links sent by email: address verification and password reset.
    Only the SHA-256 of the token is stored."""

    __tablename__ = "email_tokens"
    __table_args__ = (Index("ix_email_tokens_user_kind", "user_id", "kind"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RefreshToken(Base):
    """A browser session. Rotated on every refresh; all tokens descending from
    one login share a family_id, so reuse of a replaced token revokes the
    whole family (a stolen token dies as soon as either party uses it)."""

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[str] = mapped_column(String(32), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    replaced_by_id: Mapped[str | None] = mapped_column(String(32), default=None)
    replaced_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class ApiKey(Base):
    """A programmatic credential: hc_live_<prefix>_<secret>. Only the prefix
    (for lookup and display) and the SHA-256 of the full key are stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    user: Mapped[User] = relationship(back_populates="api_keys")

    @property
    def scopes(self) -> list[str]:
        try:
            value = json.loads(self.scopes_json or "[]")
        except ValueError:
            return []
        return [s for s in value if isinstance(s, str)]

    @scopes.setter
    def scopes(self, value: list[str]) -> None:
        self.scopes_json = json.dumps(sorted(set(value)))


class JobIndex(Base):
    """Who owns which job, plus a mirror of its status for fast listing.
    storage/jobs/{job_id}/job.json stays the source of truth for the job."""

    __tablename__ = "jobs_index"
    __table_args__ = (Index("ix_jobs_index_user_created", "user_id", "created_at"),)

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    status: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    # upload | youtube | zoom
    source_kind: Mapped[str] = mapped_column(String(16), default="upload")
    # web | api_key | bearer | ops
    created_via: Mapped[str] = mapped_column(String(16), default="web")
    api_key_id: Mapped[str | None] = mapped_column(String(32), default=None)
    callback_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    source_duration_s: Mapped[float | None] = mapped_column(Float, default=None)
    bundle_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    error_code: Mapped[str | None] = mapped_column(String(48), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class WebhookDelivery(Base):
    """One job event to POST to a job's callback_url, with retry bookkeeping."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    url: Mapped[str] = mapped_column(String(2048))
    event: Mapped[str] = mapped_column(String(32))
    payload: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
