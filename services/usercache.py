"""A read-through cache of the user fields that authorisation needs.

Every request with an access token needs the user's current role, whether
the account is active, and when its tokens were last invalidated. Caching
that for a minute keeps a database read off almost every request; anything
that changes those fields calls invalidate_user() so this process sees the
change at once."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime

from db.models import User
from db.session import session_scope

TTL_S = 60.0


@dataclass(frozen=True)
class UserSnapshot:
    id: str
    email: str
    name: str
    role: str
    is_active: bool
    email_verified: bool
    tokens_valid_after: datetime | None

    @classmethod
    def of(cls, user: User) -> UserSnapshot:
        return cls(id=user.id, email=user.email, name=user.name, role=user.role, is_active=user.is_active,
                   email_verified=user.email_verified_at is not None, tokens_valid_after=user.tokens_valid_after)


_lock = threading.Lock()
_cache: dict[str, tuple[float, UserSnapshot | None]] = {}


def get_user_snapshot(user_id: str) -> UserSnapshot | None:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(user_id)
        if hit is not None and hit[0] > now:
            return hit[1]
    with session_scope() as db:
        user = db.get(User, user_id)
        snapshot = UserSnapshot.of(user) if user is not None else None
    with _lock:
        _cache[user_id] = (now + TTL_S, snapshot)
        if len(_cache) > 10_000:  # never grows without bound
            _cache.clear()
    return snapshot


def invalidate_user(user_id: str) -> None:
    with _lock:
        _cache.pop(user_id, None)


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()
