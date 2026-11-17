"""
Public network status (GET /api/status, backend/routers/status.py).

DB-free: the build step is patched or fed mocked rows. Covers the route's own
public Cache-Control (main.py defaults API responses to no-store), the 60 s
payload cache, the 1 h reachability cache, the strict per-IP limiter, the
503-without-caching failure path, and that the payload never carries
recon-sensitive fields (firmware, IPs) and coarsens last_seen to the minute.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.database.db import get_db
from backend.routers import status as status_mod
from backend.services.rate_limit import SlidingWindowRateLimiter, rate_limit_dependency


@pytest.fixture(autouse=True)
def _reset_state():
    status_mod._payload_cache.update(at=None, body=None)
    status_mod._reach_cache.clear()
    status_mod._clock = lambda: 1000.0
    yield
    status_mod._clock = __import__("time").monotonic


def _client():
    app = FastAPI()
    app.include_router(status_mod.router)

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    return TestClient(app)


def test_route_sets_its_own_public_cache_header_and_caches_60s():
    build = AsyncMock(return_value={"status": "operational"})
    clock = {"t": 1000.0}
    status_mod._clock = lambda: clock["t"]
    with patch.object(status_mod, "_build", build):
        c = _client()
        r1 = c.get("/api/status")
        clock["t"] += 30
        r2 = c.get("/api/status")
        assert build.await_count == 1  # second hit served from the 60 s cache
        clock["t"] += 31
        c.get("/api/status")
        assert build.await_count == 2  # expired, rebuilt
    assert r1.status_code == 200 and r2.json() == {"status": "operational"}
    assert r1.headers["cache-control"] == "public, max-age=60"


def test_build_failure_is_503_no_store_and_not_cached():
    build = AsyncMock(side_effect=RuntimeError("db down"))
    with patch.object(status_mod, "_build", build):
        c = _client()
        r = c.get("/api/status")
        c.get("/api/status")
    assert r.status_code == 503
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {"status": "down", "db": "error"}
    assert build.await_count == 2  # a failure must not be cached for 60 s


def test_status_is_rate_limited_per_ip():
    limiter = SlidingWindowRateLimiter(2, 60)
    build = AsyncMock(return_value={"status": "operational"})
    with patch.object(status_mod, "_build", build), \
         patch.object(status_mod, "status_rate_limiter", limiter):
        # The dependency was bound at import time, so rebuild the route
        # against the small test limiter.
        app = FastAPI()
        app.add_api_route(
            "/api/status", status_mod.get_status, methods=["GET"],
            dependencies=[Depends(rate_limit_dependency(limiter, "status"))],
        )

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = fake_db
        c = TestClient(app)
        codes = [c.get("/api/status").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_availability_counts_offline_gateway_wins_over_db_status():
    plugs = [
        MagicMock(status="available", gateway_online=True),
        MagicMock(status="available", gateway_online=False),
        MagicMock(status="occupied", gateway_online=True),
        MagicMock(status="maintenance", gateway_online=True),
    ]
    assert status_mod._availability_counts(plugs) == {
        "total": 4, "available": 1, "occupied": 1, "offline": 1, "maintenance": 1,
    }


def test_minute_truncation_and_overall():
    dt = datetime(2026, 11, 17, 10, 5, 42, 999, tzinfo=timezone.utc)
    assert status_mod._minute(dt) == "2026-11-17T10:05:00+00:00"
    assert status_mod._minute(None) is None
    assert status_mod._overall(1, 1) == "operational"
    assert status_mod._overall(0, 1) == "degraded"
    assert status_mod._overall(0, 0) == "no_public_chargers"


@pytest.mark.asyncio
async def test_reachability_is_cached_for_an_hour():
    db = AsyncMock()
    db.get = AsyncMock(return_value=MagicMock())
    uptime = AsyncMock(return_value={"uptime_pct": 99.5})
    with patch.object(status_mod, "plug_uptime_7d", uptime):
        assert await status_mod._reachability(db, 7, 1000.0) == 99.5
        assert await status_mod._reachability(db, 7, 1000.0 + 3599) == 99.5
        assert uptime.await_count == 1
        await status_mod._reachability(db, 7, 1000.0 + 3601)
        assert uptime.await_count == 2


@pytest.mark.asyncio
async def test_build_payload_is_minimal_and_coarsened():
    last_seen = datetime.now(timezone.utc) - timedelta(seconds=5)
    gw = MagicMock(last_seen_at=last_seen, firmware_version="2.9.0-direct", vpn_ip="10.0.0.9")
    scalars = MagicMock()
    scalars.all.return_value = [gw]
    result = MagicMock()
    result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    plug = MagicMock(id=3, status="available", gateway_online=True)
    plug.name = "Pub A"
    with patch.object(status_mod, "get_public_plugs", AsyncMock(return_value=[plug])), \
         patch.object(status_mod, "gateway_is_live", return_value=True), \
         patch.object(status_mod, "_reachability", AsyncMock(return_value=100.0)), \
         patch.object(status_mod, "_tariff_today",
                      AsyncMock(return_value={"timezone": "Asia/Kolkata", "hourly": [6.0] * 24})):
        body = await status_mod._build(db, 1000.0)

    assert body["status"] == "operational" and body["db"] == "ok"
    assert body["gateways"]["online"] == 1 and body["gateways"]["total"] == 1
    assert body["gateways"]["last_seen_at"].endswith(":00+00:00")  # minute precision
    assert body["plugs"]["available"] == 1
    assert body["reachability"] == [
        {"plug_id": 3, "name": "Pub A", "status": "available", "online": True,
         "uptime_7d_pct": 100.0}
    ]
    assert len(body["tariff_today"]["hourly"]) == 24
    blob = str(body)
    assert "2.9.0" not in blob and "10.0.0.9" not in blob


@pytest.mark.asyncio
async def test_build_with_no_public_plugs_ignores_other_gateways():
    """A gateway hosting only unmappable plugs must not change status/total,
    and the gateway query is not even issued without public plugs."""
    db = AsyncMock()
    with patch.object(status_mod, "get_public_plugs", AsyncMock(return_value=[])):
        body = await status_mod._build(db, 1000.0)
    assert body["status"] == "no_public_chargers"
    assert body["gateways"]["total"] == 0 and body["gateways"]["online"] == 0
    assert body["plugs"]["total"] == 0 and body["tariff_today"] is None
    # Only the SELECT 1 probe ran; no gateway scan keyed off ungrouped plugs.
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_build_gateway_set_is_keyed_to_public_plug_ids():
    gw = MagicMock(last_seen_at=datetime.now(timezone.utc))
    scalars = MagicMock()
    scalars.all.return_value = [gw]
    result = MagicMock()
    result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    plug = MagicMock(id=3, status="available", gateway_online=True)
    plug.name = "Pub A"
    with patch.object(status_mod, "get_public_plugs", AsyncMock(return_value=[plug])),          patch.object(status_mod, "gateway_is_live", return_value=False),          patch.object(status_mod, "_reachability", AsyncMock(return_value=None)),          patch.object(status_mod, "_tariff_today",
                      AsyncMock(return_value={"timezone": "UTC", "hourly": []})):
        body = await status_mod._build(db, 1000.0)
    assert body["status"] == "degraded"
    assert body["gateways"] == {"online": 0, "total": 1, "last_seen_at": body["gateways"]["last_seen_at"]}
    sql = str(db.execute.await_args_list[1].args[0])
    assert "plugs.id IN" in sql
