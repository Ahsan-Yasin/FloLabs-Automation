"""In-process rate limiting (plan.MD §7.4): token buckets keyed by
(limit name, key). One process serves the app, so no Redis is needed; with
several processes each would enforce its own share.

A bucket holds `count` tokens and refills at count/window per second, so a
limit of 5 per hour allows a burst of 5, then one more every 12 minutes."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from core.config import get_settings

# name -> (count, window seconds)
LIMITS: dict[str, tuple[int, float]] = {
    "login_ip": (20, 900),          # all login attempts from one address
    "login_email": (8, 900),        # failed logins for one account
    "signup_ip": (5, 3600),
    "forgot_ip": (10, 3600),
    "forgot_email": (3, 3600),      # reset emails to one address
    "verify_resend": (3, 3600),     # verification emails for one account
    "refresh_ip": (120, 60),
    "api_key": (120, 60),           # requests per API key
    "user": (600, 60),              # requests per signed-in user
    "keys_create": (20, 3600),
    "webhook_test": (10, 3600),
}


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}

    def _level(self, name: str, key: str, now: float) -> tuple[float, int, float]:
        count, window = LIMITS[name]
        rate = count / window
        tokens, last = self._buckets.get((name, key), (float(count), now))
        tokens = min(float(count), tokens + (now - last) * rate)
        return tokens, count, rate

    def check(self, name: str, key: str) -> float | None:
        """Seconds to wait if the next hit would be refused, else None
        (does not consume)."""
        if not get_settings().rate_limit_enabled:
            return None
        with self._lock:
            tokens, _, rate = self._level(name, key, self.clock())
        return None if tokens >= 1 else math.ceil((1 - tokens) / rate)

    def hit(self, name: str, key: str, cost: float = 1.0) -> float | None:
        """Consume; seconds to wait if refused (nothing consumed), else None."""
        if not get_settings().rate_limit_enabled:
            return None
        with self._lock:
            now = self.clock()
            tokens, _, rate = self._level(name, key, now)
            if tokens < cost:
                self._buckets[(name, key)] = (tokens, now)
                return math.ceil((cost - tokens) / rate)
            self._buckets[(name, key)] = (tokens - cost, now)
            if len(self._buckets) > 100_000:  # bounded memory under a flood of keys
                self._buckets.clear()
            return None

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


limiter = RateLimiter()


def reset_for_tests() -> None:
    limiter.reset()
    limiter.clock = time.monotonic
