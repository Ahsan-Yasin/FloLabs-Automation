"""Browser sessions: an opaque refresh token (30 days, stored hashed) that
mints short-lived access JWTs (plan.MD §5.1-5.2).

Rotation: every refresh replaces the refresh token. Presenting a token that
was already replaced means two parties hold it (it was copied): the whole
family (every token descending from that login) is revoked. A short grace
window covers a browser firing two refreshes at once."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.config import get_settings
from db.base import new_id
from db.models import RefreshToken, User

from .errors import ServiceError
from .tokens import hash_token, make_access_token, new_opaque_token

REUSE_GRACE = timedelta(seconds=20)
MAX_TOKEN_LENGTH = 200


@dataclass
class IssuedSession:
    access_token: str
    expires_in: int
    refresh_token: str
    record: RefreshToken


def _now() -> datetime:
    return datetime.now(UTC)


def session_expired() -> ServiceError:
    return ServiceError("your session has expired; log in again", code="session_expired", status=401)


def session_revoked() -> ServiceError:
    return ServiceError("this session was signed out; log in again", code="session_revoked", status=401)


def issue(db: Session, user: User, user_agent: str = "", ip: str = "", family_id: str | None = None) -> IssuedSession:
    raw, digest = new_opaque_token(48)
    record = RefreshToken(
        id=new_id(),
        user_id=user.id,
        token_hash=digest,
        family_id=family_id or new_id(),
        expires_at=_now() + timedelta(days=max(1, get_settings().jwt_refresh_ttl_days)),
        user_agent=(user_agent or "")[:300],
        ip=(ip or "")[:64],
    )
    db.add(record)
    access, ttl = make_access_token(user.id, user.email, user.role)
    return IssuedSession(access_token=access, expires_in=ttl, refresh_token=raw, record=record)


def _lookup(db: Session, raw: str | None) -> RefreshToken | None:
    if not raw or len(raw) > MAX_TOKEN_LENGTH:
        return None
    return db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))


def rotate(db: Session, raw: str | None, user_agent: str = "", ip: str = "") -> tuple[User, IssuedSession]:
    """Exchange a refresh token for a new access token and a new refresh
    token. On reuse the family is revoked in `db` (the caller must commit
    even though this raises)."""
    token = _lookup(db, raw)
    now = _now()
    if token is None or token.expires_at <= now:
        raise session_expired()
    if token.revoked_at is not None:
        raise session_revoked()
    if token.replaced_at is not None and now - token.replaced_at > REUSE_GRACE:
        revoke_family(db, token.family_id)
        raise session_revoked()
    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise session_expired()
    issued = issue(db, user, user_agent or token.user_agent, ip or token.ip, family_id=token.family_id)
    if token.replaced_at is None:
        token.replaced_at = now
        token.replaced_by_id = issued.record.id
    token.last_used_at = now
    return user, issued


def user_id_for_refresh(db: Session, raw: str | None) -> str | None:
    """The user a still-valid refresh token belongs to (read-only; used to
    authenticate GET requests whose access cookie has just expired, such as
    a <video> element's range requests)."""
    token = _lookup(db, raw)
    now = _now()
    if token is None or token.revoked_at is not None or token.expires_at <= now:
        return None
    if token.replaced_at is not None and now - token.replaced_at > REUSE_GRACE:
        return None
    return token.user_id


def family_of(db: Session, raw: str | None) -> str | None:
    token = _lookup(db, raw)
    return token.family_id if token is not None else None


def revoke_family(db: Session, family_id: str) -> None:
    db.execute(update(RefreshToken)
               .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
               .values(revoked_at=_now()))


def revoke(db: Session, raw: str | None) -> bool:
    """Log out one browser session (the whole family of the given token)."""
    token = _lookup(db, raw)
    if token is None:
        return False
    revoke_family(db, token.family_id)
    return True


def revoke_all(db: Session, user_id: str, except_family: str | None = None) -> None:
    query = update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
    if except_family:
        query = query.where(RefreshToken.family_id != except_family)
    db.execute(query.values(revoked_at=_now()))


def list_active(db: Session, user_id: str) -> list[RefreshToken]:
    """The current token of every signed-in browser, newest first."""
    now = _now()
    rows = db.scalars(select(RefreshToken).where(
        RefreshToken.user_id == user_id,
        RefreshToken.revoked_at.is_(None),
        RefreshToken.replaced_at.is_(None),
        RefreshToken.expires_at > now,
    ).order_by(RefreshToken.created_at.desc())).all()
    return list(rows)


def revoke_session(db: Session, user_id: str, session_id: str) -> bool:
    token = db.get(RefreshToken, session_id)
    if token is None or token.user_id != user_id:
        return False
    revoke_family(db, token.family_id)
    return True
