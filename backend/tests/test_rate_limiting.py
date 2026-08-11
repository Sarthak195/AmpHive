"""
Tests for the auth rate limiting (SECURITY.md §8.6): the sliding-window
limiter, client-IP extraction behind the Caddy → nginx chain, the 429
dependency, and the wiring on /api/auth/login + /register.

Also covers the per-account limiters layered on top of the per-IP ones (one
account rotating source IPs is invisible to a limiter keyed on IP alone):
account_rate_limit_dependency (keyed by user id) on sessions start/stop,
payments create-order, and CPO offline top-up create. The per-account LOGIN
cap is not a dependency — it lives inside routers/auth.login as a FAILURE
bucket (a correct password is never throttled); see test_login.py.

And the blanket per-IP /api middleware (api_rate_limit_middleware) — the
defense-in-depth floor under all of the above.
"""
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.schemas import RegisterRequest
from backend.services import rate_limit
from backend.services.rate_limit import (
    SlidingWindowRateLimiter,
    account_rate_limit_dependency,
    api_rate_limit_middleware,
    client_ip,
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

def test_client_ip_selects_client_by_trusted_hops_from_right():
    # Chain the backend sees: <spoofed>, <real client>, <nginx→caddy peer>.
    # With 2 trusted hops (Caddy + nginx append one entry each), the real client
    # is the 2nd token from the RIGHT; the attacker-supplied leftmost entry is
    # ignored — trusting it (the old leftmost behaviour) is exactly the M3 bug.
    req = _request(headers={"x-forwarded-for": "9.9.9.9, 203.0.113.7, 172.18.0.4"})
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_ignores_spoofed_leftmost_prefix():
    """Adding fabricated leftmost tokens must not change the selected client."""
    plain = _request(headers={"x-forwarded-for": "203.0.113.7, 172.18.0.4"})
    spoofed = _request(headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8, 203.0.113.7, 172.18.0.4"})
    assert client_ip(plain) == client_ip(spoofed) == "203.0.113.7"


def test_client_ip_strips_whitespace():
    req = _request(headers={"x-forwarded-for": "  203.0.113.7 ,  172.18.0.4  "})
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_short_chain_falls_back_to_peer():
    # Fewer entries than the trusted-hop count (here a single forwarded token):
    # no forwarded value is trustworthy, so fall back to the real peer address.
    req = _request(headers={"x-forwarded-for": "203.0.113.7"}, host="192.0.2.1")
    assert client_ip(req) == "192.0.2.1"


def test_client_ip_honours_trusted_proxy_hops_env(monkeypatch):
    # nginx-only stack (1 trusted hop): the real client is the rightmost entry.
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    req = _request(headers={"x-forwarded-for": "9.9.9.9, 203.0.113.7"})
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_bad_trusted_proxy_hops_env_uses_default(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "not-a-number")
    req = _request(headers={"x-forwarded-for": "9.9.9.9, 203.0.113.7, 172.18.0.4"})
    assert client_ip(req) == "203.0.113.7"  # falls back to the default 2 hops


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
async def test_dependency_keys_by_trusted_hop_client():
    """Two distinct real clients (2nd token from the right, behind Caddy+nginx)
    behind the same proxy chain must not share a bucket."""
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = rate_limit_dependency(limiter, "login")

    await dep(_request(headers={"x-forwarded-for": "203.0.113.1, 172.18.0.4"}))
    # Same nginx hop (peer), different real client: allowed.
    await dep(_request(headers={"x-forwarded-for": "203.0.113.2, 172.18.0.4"}))
    with pytest.raises(HTTPException):
        await dep(_request(headers={"x-forwarded-for": "203.0.113.1, 172.18.0.4"}))


@pytest.mark.asyncio
async def test_dependency_spoofed_leftmost_cannot_mint_fresh_budget():
    """Rotating the spoofable leftmost X-Forwarded-For token must NOT let one
    real client escape its bucket — the core M3 abuse."""
    limiter = SlidingWindowRateLimiter(1, 60, clock=FakeClock())
    dep = rate_limit_dependency(limiter, "login")

    await dep(_request(headers={"x-forwarded-for": "203.0.113.1, 172.18.0.4"}))
    # Same real client, a fabricated fresh leftmost token: still the same bucket.
    with pytest.raises(HTTPException):
        await dep(_request(headers={"x-forwarded-for": "8.8.8.8, 203.0.113.1, 172.18.0.4"}))


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


# ------------------------------------------------- login route smoke (TC) ---

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


def test_login_route_accepts_flat_body_through_fastapi_resolution():
    """The login route takes a single `req: LoginRequest` body and its only
    remaining dependency is the per-IP rate limiter (which reads the Request,
    not the body). Drive the real route through FastAPI's own dependency
    resolution (TestClient) with a flat `{"email", "password"}` body and prove
    login succeeds (200, not 422) — i.e. the body is parsed once, cleanly.

    DB-free: get_db is overridden with a stub session (same mocked-db pattern
    as test_login.py) so this doesn't touch a real database or the app's
    lifespan — only backend.routers.auth is mounted, not the full app.
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

    assert resp.status_code == 200, resp.text  # flat body parsed cleanly
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


def test_login_route_carries_only_the_per_ip_dependency_not_a_pre_handler_account_block():
    """The per-account login cap is NOT a pre-handler dependency any more: a
    hard pre-check keyed on the victim's email let an IP-rotating attacker 429
    the victim BEFORE the password was checked (targeted-lockout DoS). The
    route must carry the per-IP limiter and NOT re-introduce a body-keyed
    account dependency; the per-account FAILURE cap now lives inside the
    handler (see test_login.py)."""
    from backend.routers.auth import router
    route = _route(router, "/api/auth/login")
    dep_names = [d.call.__qualname__ for d in route.dependant.dependencies]
    assert any(
        name == "rate_limit_dependency.<locals>.dependency" for name in dep_names
    ), dep_names
    assert not any("login_account_rate_limit_dependency" in name for name in dep_names), dep_names


@pytest.mark.parametrize(
    "module_path,route_path,method",
    [
        ("backend.routers.sessions", "/api/sessions/start", "POST"),
        ("backend.routers.sessions", "/api/sessions/stop", "POST"),
        ("backend.routers.payments", "/api/payments/create-order", "POST"),
        ("backend.routers.cpo._topups", "/api/cpo/topups", "POST"),
        ("backend.routers.cpo._gateways", "/api/cpo/gateways/claim", "POST"),
        ("backend.routers.groups", "/api/groups/join", "POST"),
        ("backend.routers.cpo._profile", "/api/cpo/setup", "POST"),
    ],
)
def test_account_scoped_routes_carry_the_account_rate_limit_dependency(module_path, route_path, method):
    import importlib

    router = importlib.import_module(module_path).router
    route = _route(router, route_path, method)
    dep_names = [d.call.__qualname__ for d in route.dependant.dependencies]
    assert any("account_rate_limit_dependency" in name for name in dep_names), dep_names


# ------------------------------------------------- blanket /api middleware ---

def _blanket_app():
    """Minimal app with only the blanket middleware mounted — the real
    backend.main registration is asserted separately below."""
    from fastapi import FastAPI

    app = FastAPI()
    app.middleware("http")(api_rate_limit_middleware)

    @app.get("/api/things")
    def things():
        return {"ok": True}

    @app.get("/api/health")
    def health():
        return {"status": "healthy"}

    @app.get("/socket.io/")
    def socketio_ish():
        return {"ok": True}

    return app


def _patched_blanket(limiter):
    """The middleware looks `api_rate_limiter` up per-call precisely so tests
    can swap in a FakeClock-driven (or None) limiter."""
    return patch.object(rate_limit, "api_rate_limiter", limiter)


def test_blanket_middleware_429s_over_the_limit_with_retry_after():
    client = TestClient(_blanket_app())
    with _patched_blanket(SlidingWindowRateLimiter(2, 60, clock=FakeClock())):
        assert client.get("/api/things").status_code == 200
        assert client.get("/api/things").status_code == 200
        resp = client.get("/api/things")

    assert resp.status_code == 429
    assert re.match(
        r"^Too many requests\. Try again in \d+ s\.$", resp.json()["detail"]
    ), resp.text
    assert int(resp.headers["Retry-After"]) >= 1


def test_blanket_middleware_keys_by_trusted_hop_not_spoofable_prefix():
    client = TestClient(_blanket_app())
    with _patched_blanket(SlidingWindowRateLimiter(1, 60, clock=FakeClock())):
        # Real client = 2nd token from the right (Caddy + nginx append 2 hops).
        a = {"X-Forwarded-For": "203.0.113.1, 172.18.0.4"}
        b = {"X-Forwarded-For": "203.0.113.2, 172.18.0.4"}
        assert client.get("/api/things", headers=a).status_code == 200
        assert client.get("/api/things", headers=b).status_code == 200  # own bucket
        # Rotating the spoofable leftmost entry does NOT mint a fresh budget:
        # it resolves to the same real client (203.0.113.1) and is throttled.
        spoofed = {"X-Forwarded-For": "1.2.3.4, 203.0.113.1, 172.18.0.4"}
        assert client.get("/api/things", headers=spoofed).status_code == 429


def test_blanket_middleware_exempts_health_and_non_api_paths():
    client = TestClient(_blanket_app())
    with _patched_blanket(SlidingWindowRateLimiter(1, 60, clock=FakeClock())):
        assert client.get("/api/things").status_code == 200  # bucket now full
        for _ in range(3):
            assert client.get("/api/health").status_code == 200
            assert client.get("/socket.io/").status_code == 200
        assert client.get("/api/things").status_code == 429


def test_blanket_middleware_spends_budget_on_unmatched_api_probes():
    """Runs before routing: a 404 probe flood over /api/* consumes the same
    bucket as real routes (a dependency-based limiter would never see it)."""
    client = TestClient(_blanket_app())
    with _patched_blanket(SlidingWindowRateLimiter(2, 60, clock=FakeClock())):
        assert client.get("/api/no-such-route").status_code == 404
        assert client.get("/api/no-such-route").status_code == 404
        assert client.get("/api/no-such-route").status_code == 429
        assert client.get("/api/things").status_code == 429  # same bucket


def test_blanket_middleware_none_limiter_disables_the_layer():
    client = TestClient(_blanket_app())
    with _patched_blanket(None):
        for _ in range(5):
            assert client.get("/api/things").status_code == 200


def test_optional_limiter_off_and_rule_parsing(monkeypatch):
    monkeypatch.setenv("API_RATE_LIMIT", "off")
    assert rate_limit._optional_limiter("API_RATE_LIMIT", "300/60") is None
    monkeypatch.setenv("API_RATE_LIMIT", "0")
    assert rate_limit._optional_limiter("API_RATE_LIMIT", "300/60") is None
    monkeypatch.setenv("API_RATE_LIMIT", "20/60")
    limiter = rate_limit._optional_limiter("API_RATE_LIMIT", "300/60")
    assert limiter.max_attempts == 20 and limiter.window_sec == 60


def _mw_kwargs(m):
    # Starlette's Middleware carried .options before 0.35 and .kwargs after.
    return getattr(m, "kwargs", None) or getattr(m, "options", {})


# ---------------------------------------------------- schema-level bounds ---

def test_register_full_name_over_150_chars_is_rejected():
    """users.full_name is String(150) (database/models.py) — an oversized
    value must fail Pydantic validation (422) rather than reach the DB and
    raise an uncaught DataError (500)."""
    with pytest.raises(ValidationError):
        RegisterRequest(email="a@example.com", password="correct-horse", full_name="x" * 151)


def test_register_full_name_at_150_chars_is_accepted():
    req = RegisterRequest(email="a@example.com", password="correct-horse", full_name="x" * 150)
    assert len(req.full_name) == 150


def test_app_registers_the_blanket_middleware_inside_cors():
    """backend.main must mount the blanket limiter with CORSMiddleware
    OUTSIDE it (user_middleware[0] is the outermost — add_middleware inserts
    at 0 and Starlette builds the stack reversed): preflight OPTIONS then
    never spend budget, and a 429 passes back through CORS and gets its
    headers, so a cross-origin page can read the error."""
    from fastapi.middleware.cors import CORSMiddleware

    from backend.main import app

    # backend.main's exported `app` is the socketio.ASGIApp wrapper (which is
    # also why /socket.io traffic never reaches this limiter); the FastAPI
    # instance with the middleware stack is its other_asgi_app.
    fastapi_app = app.other_asgi_app

    idx_limit = next(
        i for i, m in enumerate(fastapi_app.user_middleware)
        if _mw_kwargs(m).get("dispatch") is api_rate_limit_middleware
    )
    idx_cors = next(
        i for i, m in enumerate(fastapi_app.user_middleware) if m.cls is CORSMiddleware
    )
    assert idx_cors < idx_limit
