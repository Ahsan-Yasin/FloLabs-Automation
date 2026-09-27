"""Access tokens (short-lived JWT, HS256) and opaque tokens (refresh tokens,
email links) that are stored only as SHA-256 hashes (plan.MD §5.2)."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)

ALGORITHM = "HS256"
ACCESS_TYPE = "access"
LEEWAY_S = 10

_dev_secret_lock = threading.Lock()


def jwt_secret() -> str:
    """JWT_SECRET, or in dev a random secret kept in storage/dev_jwt_secret
    (so local sessions survive a restart). Prod refuses to start without
    JWT_SECRET (core.config.settings_problems)."""
    settings = get_settings()
    if settings.jwt_secret:
        return settings.jwt_secret
    if settings.is_prod:
        raise RuntimeError("JWT_SECRET is not set")
    path = settings.storage_dir / "dev_jwt_secret"
    with _dev_secret_lock:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                return value
        value = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.warning("JWT_SECRET is empty: using a development secret stored in %s", path)
        return value


def make_access_token(user_id: str, email: str, role: str) -> tuple[str, int]:
    """(token, lifetime in seconds)."""
    ttl = max(1, get_settings().jwt_access_ttl_min) * 60
    now = datetime.now(UTC)
    claims = {
        "sub": user_id,
        "email": email,
        "role": role,
        "type": ACCESS_TYPE,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, jwt_secret(), algorithm=ALGORITHM), ttl


def decode_access_token(token: str) -> dict | None:
    """The claims of a valid, unexpired access token, else None."""
    try:
        claims = jwt.decode(
            token,
            jwt_secret(),
            algorithms=[ALGORITHM],
            leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.PyJWTError:
        return None
    if claims.get("type") != ACCESS_TYPE or not isinstance(claims.get("sub"), str):
        return None
    return claims


def looks_like_jwt(value: str) -> bool:
    return value.count(".") == 2 and len(value) > 20


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_opaque_token(nbytes: int = 32) -> tuple[str, str]:
    """(raw token to hand out, its hash to store)."""
    raw = secrets.token_urlsafe(nbytes)
    return raw, hash_token(raw)
