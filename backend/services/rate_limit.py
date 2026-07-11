"""
In-process rate limiting for the auth endpoints (SECURITY.md §8.6).

Single-process by design: the backend runs as one uvicorn container, so an
in-memory sliding window is sufficient — no Redis/shared store. Counters reset
on restart, which is acceptable for brute-force / enumeration protection (an
attacker cannot trigger restarts).

Rules are env-configurable as "<attempts>/<window seconds>":
  LOGIN_RATE_LIMIT    (default 10/60   — 10 attempts per minute per IP)
  REGISTER_RATE_LIMIT (default 10/3600 — 10 registrations per hour per IP)
"""
import logging
import os
import time
from collections import deque

from fastapi import HTTPException, Request

logger = logging.getLogger("amphive.rate_limit")


class SlidingWindowRateLimiter:
    """Allow at most `max_attempts` calls per `window_sec` per key.

    Blocked calls are not recorded, so a client that keeps retrying is not
    punished beyond the window — the guarantee is simply "no more than
    max_attempts succeed per window". The clock is injectable for tests.
    """

    def __init__(self, max_attempts: int, window_sec: float, clock=time.monotonic):
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self._clock = clock
        self._hits: dict = {}
        self._last_sweep = clock()

    def check(self, key: str):
        """Record an attempt for `key` if allowed.

        Returns None when the attempt is allowed, else the number of seconds
        until the oldest counted attempt leaves the window (> 0).
        """
        now = self._clock()
        cutoff = now - self.window_sec
        dq = self._hits.get(key)
        if dq is None:
            dq = self._hits[key] = deque()
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.max_attempts:
            return dq[0] + self.window_sec - now
        dq.append(now)
        self._maybe_sweep(now)
        return None

    def _maybe_sweep(self, now):
        # Drop idle keys so the map can't grow without bound under an
        # address-rotating client.
        if now - self._last_sweep < self.window_sec:
            return
        self._last_sweep = now
        cutoff = now - self.window_sec
        for key in list(self._hits):
            dq = self._hits[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                del self._hits[key]


def client_ip(request: Request) -> str:
    """Best-effort client IP behind the Caddy → frontend-nginx chain.

    Caddy (≥ 2.5) replaces a client-supplied X-Forwarded-For with the real
    peer address unless the peer is a configured trusted proxy, and the nginx
    hop appends its own upstream — so the first entry is the real client.
    With the public :8000 firewall port closed (2026-07-11) no untrusted peer
    reaches the backend directly; in the -NoTls rollback stack the header is
    client-forgeable, which only lets an attacker shard their own limit.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    first = fwd.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def _rule_from_env(env_name: str, default: str):
    raw = os.getenv(env_name, default)
    try:
        attempts_s, window_s = raw.split("/", 1)
        attempts, window = int(attempts_s), float(window_s)
        if attempts < 1 or window <= 0:
            raise ValueError(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r (expected '<attempts>/<window seconds>'); using default %s",
            env_name, raw, default,
        )
        attempts_s, window_s = default.split("/", 1)
        attempts, window = int(attempts_s), float(window_s)
    return attempts, window


def rate_limit_dependency(limiter: SlidingWindowRateLimiter, action: str):
    """FastAPI dependency enforcing `limiter` per client IP; raises 429."""
    async def dependency(request: Request):
        retry_after = limiter.check(client_ip(request))
        if retry_after is not None:
            seconds = int(retry_after) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many {action} attempts. Try again in {seconds} s.",
                headers={"Retry-After": str(seconds)},
            )
    return dependency


login_rate_limiter = SlidingWindowRateLimiter(*_rule_from_env("LOGIN_RATE_LIMIT", "10/60"))
register_rate_limiter = SlidingWindowRateLimiter(*_rule_from_env("REGISTER_RATE_LIMIT", "10/3600"))
