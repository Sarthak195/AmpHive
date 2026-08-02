"""
Tests for the auth rate limiting (SECURITY.md §8.6): the sliding-window
limiter, client-IP extraction behind the Caddy → nginx chain, the 429
dependency, and the wiring on /api/auth/login + /register.

Also covers the per-account limiters layered on top of the per-IP ones (one
account rotating source IPs is invisible to a limiter keyed on IP alone):
account_rate_limit_dependency (keyed by user id) on sessions start/stop,
payments create-order, and CPO offline top-up create; and
login_account_rate_limit_dependency (keyed by normalized email) on login.
"""
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.schemas import LoginRequest
from backend.services.rate_limit import (
    SlidingWindowRateLimiter,
    account_rate_limit_dependency,
    client_ip,
    login_account_rate_limit_dependency,
    rate_limit_dependency,
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


# ----------------------------------------------------- account dependency ---

def _mock_user(user_id):
    u = MagicMock()
    u.id = user_id
    return u


@pytest.mark.asyncio
async def test_account_dependency_keys_by_user_id_not_ip():
    """Two accounts sharing a bucket would defeat the whole point (the
    per-IP limiter already covers "one IP, many attempts" — this dependency
    exists specifically for "one account, many IPs"). Exhausting user A's
    quota must not touch user B's, and vice versa — the dependency never
    reads request/IP at all, only `user.id`."""
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = account_rate_limit_dependency(limiter, "session start")
    user_a, user_b = _mock_user(1), _mock_user(2)

    await dep(user=user_a)
    with pytest.raises(HTTPException):
        await dep(user=user_a)
    await dep(user=user_b)  # unaffected by A's exhausted bucket
    with pytest.raises(HTTPException):
        await dep(user=user_b)


@pytest.mark.asyncio
async def test_account_dependency_returns_the_user_on_success():
    limiter = SlidingWindowRateLimiter(5, 60, clock=FakeClock())
    dep = account_rate_limit_dependency(limiter, "session start")
    user = _mock_user(1)
    assert await dep(user=user) is user


@pytest.mark.asyncio
async def test_account_dependency_429_names_the_action_and_the_account():
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = account_rate_limit_dependency(limiter, "session start")
    user = _mock_user(1)

    await dep(user=user)
    with pytest.raises(HTTPException) as exc_info:
        await dep(user=user)

    exc = exc_info.value
    assert exc.status_code == 429
    assert re.match(
        r"^Too many session start attempts on this account\. Try again in \d+ s\.$",
        exc.detail,
    ), exc.detail
    assert int(exc.headers["Retry-After"]) >= 1


# ----------------------------------------------------- login account dep ---

@pytest.mark.asyncio
async def test_login_dependency_buckets_by_normalized_email_case_insensitive():
    """`Driver@AmpHive.Test` and `  driver@amphive.test  ` are the same
    account (services.auth.normalize_email) and must share one bucket."""
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = login_account_rate_limit_dependency(limiter)

    await dep(LoginRequest(email="Driver@AmpHive.Test", password="x"))
    with pytest.raises(HTTPException):
        await dep(LoginRequest(email="  driver@amphive.test  ", password="y"))


@pytest.mark.asyncio
async def test_login_dependency_different_email_is_unaffected():
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = login_account_rate_limit_dependency(limiter)

    await dep(LoginRequest(email="a@amphive.test", password="x"))
    with pytest.raises(HTTPException):
        await dep(LoginRequest(email="a@amphive.test", password="y"))
    await dep(LoginRequest(email="b@amphive.test", password="z"))  # different bucket


@pytest.mark.asyncio
async def test_login_dependency_429_copy_is_generic_same_shape_as_per_ip():
    """No account-enumeration oracle: the message must not hint at whether
    the email belongs to a real account, and must be the exact same shape
    (ideally the exact same text) as the per-IP login limiter's message for
    the same action, so a caller can't distinguish "rate-limited by IP" from
    "rate-limited by account"."""
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = login_account_rate_limit_dependency(limiter)
    req = LoginRequest(email="nobody@amphive.test", password="x")

    await dep(req)
    with pytest.raises(HTTPException) as exc_info:
        await dep(req)
    exc = exc_info.value

    ip_limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    ip_dep = rate_limit_dependency(ip_limiter, "login")
    ip_req = _request(host="203.0.113.9")
    await ip_dep(ip_req)
    with pytest.raises(HTTPException) as ip_exc_info:
        await ip_dep(ip_req)
    ip_exc = ip_exc_info.value

    assert exc.status_code == 429
    pattern = r"^Too many login attempts\. Try again in \d+ s\.$"
    assert re.match(pattern, exc.detail), exc.detail
    assert re.match(pattern, ip_exc.detail), ip_exc.detail
    assert exc.detail == ip_exc.detail  # byte-identical, not just same shape
    assert "nobody" not in exc.detail and "amphive.test" not in exc.detail
    assert int(exc.headers["Retry-After"]) >= 1


# ------------------------------------------- login route body-merge proof ---

def _login_user(user_id=7, email="driver@amphive.test", password="correct-horse"):
    from backend.services.auth import hash_password
    u = MagicMock()
    u.id = user_id
    u.email = email
    u.hashed_password = hash_password(password)
    u.full_name = "Test Driver"
    u.role = MagicMock()
    u.role.value = "driver"
    u.coin_balance = 0.0
    u.token_version = 0
    u.is_disabled = False  # bare MagicMock attr is truthy -> spurious 403
    return u


def test_login_route_body_is_not_double_consumed_by_the_account_dependency():
    """Wiring proof (spec requirement): `login_account_rate_limit_dependency`
    declares its own `req: LoginRequest` body parameter, layered alongside
    the route's own `req: LoginRequest`. If FastAPI treated those as two
    distinct body fields, it would require a nested `{"req": {...}, "req":
    {...}}`-shaped payload and reject the flat `{"email", "password"}` body
    every real client sends (422). Drive the real route through FastAPI's
    own dependency resolution (TestClient) with a flat body and prove login
    still succeeds — i.e. the body-param-name match (both named `req`)
    really does collapse the two into one parsed body, not two.

    DB-free: get_db is overridden with a stub session (same mocked-db
    pattern as test_login.py) so this doesn't touch a real database or the
    app's lifespan (MQTT/session-reaper startup) — only backend.routers.auth
    is mounted, not the full app.
    """
    from fastapi import FastAPI

    from backend.database.db import get_db
    from backend.routers import auth as auth_router

    user = _login_user()

    async def override_get_db():
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        yield db

    app = FastAPI()
    app.include_router(auth_router.router)
    app.dependency_overrides[get_db] = override_get_db

    with patch.object(auth_router, "check_and_speed_up_active_session", new=AsyncMock()):
        client = TestClient(app)
        resp = client.post(
            "/api/auth/login",
            json={"email": user.email, "password": "correct-horse"},
        )

    assert resp.status_code == 200, resp.text  # not 422 -> body was not double-consumed
    body = resp.json()
    assert body["user"]["email"] == user.email
    assert body["token"]


# ----------------------------------------------------------------- wiring ---

def _route(router, path, method="POST"):
    return next(
        r for r in router.routes
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set())
    )


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/register"])
def test_auth_routes_carry_the_rate_limit_dependency(path):
    from backend.routers.auth import router
    route = _route(router, path)
    dep_names = [d.call.__qualname__ for d in route.dependant.dependencies]
    assert any("rate_limit_dependency" in name for name in dep_names), dep_names


def test_login_route_also_carries_the_account_rate_limit_dependency():
    """Layered, not replaced: /api/auth/login must carry BOTH the original
    per-IP login limiter and the new per-account one."""
    from backend.routers.auth import router
    route = _route(router, "/api/auth/login")
    dep_names = [d.call.__qualname__ for d in route.dependant.dependencies]
    assert any("login_account_rate_limit_dependency" in name for name in dep_names), dep_names
    assert any(
        name == "rate_limit_dependency.<locals>.dependency" for name in dep_names
    ), dep_names


@pytest.mark.parametrize(
    "module_path,route_path,method",
    [
        ("backend.routers.sessions", "/api/sessions/start", "POST"),
        ("backend.routers.sessions", "/api/sessions/stop", "POST"),
        ("backend.routers.payments", "/api/payments/create-order", "POST"),
        ("backend.routers.cpo._topups", "/api/cpo/topups", "POST"),
    ],
)
def test_account_scoped_routes_carry_the_account_rate_limit_dependency(module_path, route_path, method):
    import importlib

    router = importlib.import_module(module_path).router
    route = _route(router, route_path, method)
    dep_names = [d.call.__qualname__ for d in route.dependant.dependencies]
    assert any("account_rate_limit_dependency" in name for name in dep_names), dep_names
