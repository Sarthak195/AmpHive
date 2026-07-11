"""
Tests for the auth rate limiting (SECURITY.md §8.6): the sliding-window
limiter, client-IP extraction behind the Caddy → nginx chain, the 429
dependency, and the wiring on /api/auth/login + /register.
"""
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.services.rate_limit import (
    SlidingWindowRateLimiter, client_ip, rate_limit_dependency,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _request(headers=None, host="10.0.0.9"):
    req = MagicMock()
    req.headers = headers or {}
    req.client.host = host
    return req


# ---------------------------------------------------------------- limiter ---

def test_allows_up_to_max_attempts_then_blocks():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(3, 60, clock=clock)

    assert limiter.check("ip1") is None
    assert limiter.check("ip1") is None
    assert limiter.check("ip1") is None
    retry_after = limiter.check("ip1")
    assert retry_after is not None and 0 < retry_after <= 60


def test_window_slides_attempts_expire():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(2, 60, clock=clock)

    assert limiter.check("k") is None
    clock.advance(30)
    assert limiter.check("k") is None
    assert limiter.check("k") is not None  # both attempts still in window
    clock.advance(31)                       # first attempt (t=0) now expired
    assert limiter.check("k") is None
    assert limiter.check("k") is not None


def test_blocked_attempts_do_not_extend_the_block():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(1, 60, clock=clock)

    assert limiter.check("k") is None
    for _ in range(5):
        assert limiter.check("k") is not None
    clock.advance(60.1)                     # only the allowed attempt counted
    assert limiter.check("k") is None


def test_keys_are_independent():
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("a") is not None


def test_retry_after_reflects_remaining_window():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(1, 60, clock=clock)
    limiter.check("k")
    clock.advance(45)
    retry_after = limiter.check("k")
    assert retry_after == pytest.approx(15)


def test_idle_keys_are_swept():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(3, 60, clock=clock)
    limiter.check("old")
    clock.advance(120)
    limiter.check("new")                    # triggers the periodic sweep
    assert "old" not in limiter._hits


# -------------------------------------------------------------- client_ip ---

def test_client_ip_uses_first_forwarded_entry():
    req = _request(headers={"x-forwarded-for": "203.0.113.7, 172.18.0.4"})
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_strips_whitespace():
    req = _request(headers={"x-forwarded-for": "  203.0.113.7  "})
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_falls_back_to_peer_address():
    assert client_ip(_request(host="192.0.2.1")) == "192.0.2.1"


def test_client_ip_handles_missing_client():
    req = _request()
    req.client = None
    assert client_ip(req) == "unknown"


# ------------------------------------------------------------- dependency ---

@pytest.mark.asyncio
async def test_dependency_raises_429_with_retry_after():
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = rate_limit_dependency(limiter, "login")
    req = _request(host="198.51.100.2")

    await dep(req)  # first attempt allowed
    with pytest.raises(HTTPException) as exc_info:
        await dep(req)

    exc = exc_info.value
    assert exc.status_code == 429
    assert "login" in exc.detail
    assert int(exc.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_dependency_keys_by_forwarded_client_not_proxy_hop():
    """Two clients behind the same proxy chain must not share a bucket."""
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = rate_limit_dependency(limiter, "login")

    await dep(_request(headers={"x-forwarded-for": "203.0.113.1, 172.18.0.4"}))
    # Same nginx hop (peer), different real client: allowed.
    await dep(_request(headers={"x-forwarded-for": "203.0.113.2, 172.18.0.4"}))
    with pytest.raises(HTTPException):
        await dep(_request(headers={"x-forwarded-for": "203.0.113.1, 172.18.0.4"}))


# ----------------------------------------------------------------- wiring ---

def _route(path):
    from backend.routers.auth import router
    return next(r for r in router.routes if getattr(r, "path", None) == path)


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/register"])
def test_auth_routes_carry_the_rate_limit_dependency(path):
    route = _route(path)
    dep_names = [d.call.__qualname__ for d in route.dependant.dependencies]
    assert any("rate_limit_dependency" in name for name in dep_names), dep_names
