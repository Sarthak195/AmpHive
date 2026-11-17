"""
DB-gated tests for the SQL added in release wave 1 (2026-11-17), which was
otherwise only ever compiled or exercised against mocked sessions:

- GET /api/sessions/{id}/telemetry: the SQL-side bucketing (F2)
- GET /api/me/stats: the 12-month series with refunds netted (F8)
- GET /api/status: the public gateway/plug counts and reachability (F11)
- GET /api/admin/health: the pg_database_size / numbackends probe (F5)

They need a real PostgreSQL (CI's postgres service, exported as
TEST_DATABASE_URL) and are skipped locally, like test_payouts.py whose
requires_db / factory pattern they follow. Route functions are called
directly with a MagicMock user, so no auth, rate limiter or HTTP is involved.
"""
import itertools
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from backend.database.models import (
    ChargerGroup,
    ChargingSession,
    DisputeStatus,
    Gateway,
    GatewayStatus,
    Plug,
    PlugStatus,
    SessionDispute,
    SessionStatus,
    TelemetryReading,
    Tenant,
    User,
    UserRole,
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status",
]

_n = itertools.count(1)


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory (same shape as
    test_payouts.py's fixture)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

async def _tenant(factory, name="WaveOneCo"):
    async with factory() as db:
        tenant = Tenant(name=name)
        db.add(tenant)
        await db.commit()
        return tenant.id


async def _driver(factory):
    async with factory() as db:
        user = User(
            email=f"driver-{next(_n)}@example.com",
            hashed_password="x",
            full_name="Driver Test",
            role=UserRole.DRIVER,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _gateway(factory, tenant_id, status=GatewayStatus.ONLINE, last_seen=None,
                   lat=12.97, lon=77.59):
    n = next(_n)
    async with factory() as db:
        gw = Gateway(
            id=f"w1-gw-{n}",
            tenant_id=tenant_id,
            name="GW",
            vpn_ip=f"10.1.{n // 250}.{n % 250 + 1}",
            status=status,
            last_seen_at=last_seen or datetime.now(timezone.utc),
            latitude=lat,
            longitude=lon,
        )
        db.add(gw)
        await db.commit()
        return gw.id


async def _plug(factory, gateway_id, status=PlugStatus.AVAILABLE, group_id=None,
                created_at=None):
    async with factory() as db:
        plug = Plug(
            gateway_id=gateway_id,
            name=f"Plug {next(_n)}",
            local_ip="10.0.1.1",
            status=status,
            group_id=group_id,
        )
        if created_at is not None:
            plug.created_at = created_at
        db.add(plug)
        await db.commit()
        return plug.id


async def _session(factory, tenant_id, user_id, plug_id, started_at, ended_at,
                   status=SessionStatus.COMPLETED, energy=1.0, coins="10.00"):
    async with factory() as db:
        s = ChargingSession(
            tenant_id=tenant_id,
            user_id=user_id,
            plug_id=plug_id,
            started_at=started_at,
            ended_at=ended_at,
            energy_kwh=energy,
            coins_spent=Decimal(coins),
            status=status,
        )
        db.add(s)
        await db.commit()
        return s.id


def _reading(tenant_id, plug_id, at, power, energy=0.0, current=0.0):
    return TelemetryReading(
        tenant_id=tenant_id,
        plug_id=plug_id,
        recorded_at=at,
        power_w=power,
        energy_kwh=energy,
        voltage_v=230.0,
        current_a=current,
    )


def _user(user_id, role=UserRole.DRIVER, tenant_id=None):
    u = MagicMock()
    u.id = user_id
    u.role = role
    u.tenant_id = tenant_id
    return u


# ---------------------------------------------------------------------------
# (a) GET /api/sessions/{id}/telemetry
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.asyncio
async def test_session_telemetry_buckets_in_sql_and_excludes_other_plugs(factory):
    from backend.routers.sessions import get_session_telemetry

    tenant_id = await _tenant(factory)
    driver_id = await _driver(factory)
    gw = await _gateway(factory, tenant_id)
    plug = await _plug(factory, gw)
    other_plug = await _plug(factory, gw)

    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
    ended = started + timedelta(hours=1)  # 3600 s -> 30 s buckets
    session_id = await _session(factory, tenant_id, driver_id, plug, started, ended)

    rows = []
    # One reading every 10 s strictly inside the window: 2000 W first half,
    # 3000 W second half, energy climbing 0 -> 1 kWh, one 3500 W spike.
    for i in range(360):
        at = started + timedelta(seconds=i * 10)
        power = 2000.0 if i < 180 else 3000.0
        if i == 100:
            power = 3500.0
        rows.append(_reading(tenant_id, plug, at, power, energy=i / 360.0, current=8.0))
    # ANOTHER plug in the same window (must never leak in) ...
    for i in range(0, 360, 5):
        rows.append(_reading(tenant_id, other_plug, started + timedelta(seconds=i * 10), 99999.0))
    # ... and this plug just outside the window on both sides.
    rows.append(_reading(tenant_id, plug, started - timedelta(seconds=60), 7777.0))
    rows.append(_reading(tenant_id, plug, ended + timedelta(seconds=60), 7777.0))
    async with factory() as db:
        db.add_all(rows)
        await db.commit()

    async with factory() as db:
        out = await get_session_telemetry(session_id, _user(driver_id), db)

    points = out["points"]
    assert out["bucket_seconds"] == 30
    assert 0 < len(points) <= 120
    offsets = [p["offset_sec"] for p in points]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    assert offsets[0] == 0
    # The other plug (99999 W) and the out-of-window rows (7777 W) are excluded.
    assert max(p["peak_power_w"] for p in points) == 3500.0
    # Bucket 0 covers readings at 0, 10, 20 s: all 2000 W, 8 A.
    assert points[0]["power_w"] == 2000.0
    assert points[0]["current_a"] == 8.0
    assert all(2000.0 <= p["power_w"] <= 3500.0 for p in points)
    assert all(p["peak_power_w"] >= p["power_w"] for p in points)
    # Cumulative energy (MAX per bucket) never goes backwards.
    energies = [p["energy_kwh"] for p in points]
    assert energies == sorted(energies)
    assert energies[-1] == pytest.approx(359 / 360.0, abs=1e-3)


# ---------------------------------------------------------------------------
# (b) GET /api/me/stats: 12-month series, refund netted on the session's month
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.asyncio
async def test_my_stats_series_nets_an_approved_refund_on_its_sessions_month(factory):
    from backend.routers.sessions import get_my_stats

    tenant_id = await _tenant(factory)
    driver_id = await _driver(factory)
    gw = await _gateway(factory, tenant_id)
    plug = await _plug(factory, gw)

    now = datetime.now(timezone.utc)
    this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = this_month - timedelta(days=10)  # always inside the previous month
    this_key = f"{this_month.year:04d}-{this_month.month:02d}"
    last_key = f"{last_month.year:04d}-{last_month.month:02d}"

    await _session(
        factory, tenant_id, driver_id, plug,
        this_month + timedelta(minutes=5), this_month + timedelta(minutes=65),
        energy=2.0, coins="20.00",
    )
    refunded = await _session(
        factory, tenant_id, driver_id, plug,
        last_month, last_month + timedelta(hours=1), energy=5.0, coins="50.00",
    )
    rejected = await _session(
        factory, tenant_id, driver_id, plug,
        last_month + timedelta(days=1), last_month + timedelta(days=1, hours=1),
        energy=1.0, coins="10.00",
    )
    # A cancelled session counts toward lifetime but never toward the series.
    await _session(
        factory, tenant_id, driver_id, plug,
        last_month + timedelta(days=2), last_month + timedelta(days=2, hours=1),
        status=SessionStatus.CANCELLED, energy=9.0, coins="0.00",
    )
    async with factory() as db:
        # Approved today (this month) but taken off the SESSION's month.
        db.add(SessionDispute(
            session_id=refunded, tenant_id=tenant_id, driver_user_id=driver_id,
            reason="Stopped early", status=DisputeStatus.APPROVED,
            refund_coins=Decimal("10.00"), resolved_at=now,
        ))
        db.add(SessionDispute(
            session_id=rejected, tenant_id=tenant_id, driver_user_id=driver_id,
            reason="Changed my mind", status=DisputeStatus.REJECTED, resolved_at=now,
        ))
        await db.commit()

    async with factory() as db:
        out = await get_my_stats(_user(driver_id), db)

    series = out["series"]
    assert len(series) == 12
    months = [m["month"] for m in series]
    assert months == sorted(months) and months[-1] == this_key
    by_month = {m["month"]: m for m in series}
    assert by_month[this_key] == {
        "month": this_key, "energy_kwh": 2.0, "spend_coins": 20.0, "sessions": 1,
    }
    # (50 + 10) gross, minus the 10 approved refund; the rejected one nets 0.
    assert by_month[last_key] == {
        "month": last_key, "energy_kwh": 6.0, "spend_coins": 50.0, "sessions": 2,
    }
    zero = [m for k, m in by_month.items() if k not in (this_key, last_key)]
    assert len(zero) == 10
    assert all(m["spend_coins"] == 0 and m["sessions"] == 0 for m in zero)
    # Lifetime keeps the gross finished-session totals (incl. the cancelled one).
    assert out["lifetime"]["sessions"] == 4
    assert out["lifetime"]["energy_kwh"] == 17.0


# ---------------------------------------------------------------------------
# (c) GET /api/status counts
# ---------------------------------------------------------------------------

@pytest.fixture()
def _fresh_status_cache():
    from backend.routers import status as status_mod

    status_mod._payload_cache.update(at=None, body=None)
    status_mod._reach_cache.clear()
    yield status_mod
    status_mod._payload_cache.update(at=None, body=None)
    status_mod._reach_cache.clear()


@requires_db
@pytest.mark.asyncio
async def test_status_counts_gateways_and_plugs_and_hides_private_groups(factory, _fresh_status_cache):
    from fastapi import Response

    status_mod = _fresh_status_cache
    tenant_id = await _tenant(factory)
    now = datetime.now(timezone.utc)

    online_gw = await _gateway(factory, tenant_id, last_seen=now)
    offline_gw = await _gateway(factory, tenant_id, status=GatewayStatus.OFFLINE,
                                last_seen=now - timedelta(hours=3))
    async with factory() as db:
        private = ChargerGroup(tenant_id=tenant_id, name="Residents", is_public=False)
        db.add(private)
        await db.commit()
        private_id = private.id

    two_days_ago = now - timedelta(days=2)
    available = await _plug(factory, online_gw, PlugStatus.AVAILABLE, created_at=two_days_ago)
    await _plug(factory, online_gw, PlugStatus.OCCUPIED)
    await _plug(factory, online_gw, PlugStatus.MAINTENANCE)
    await _plug(factory, offline_gw, PlugStatus.AVAILABLE)  # unreachable: counts as offline
    await _plug(factory, online_gw, PlugStatus.AVAILABLE, group_id=private_id)  # never public

    # Two distinct hours of telemetry on the older plug feed the 7-day figure.
    async with factory() as db:
        db.add_all([
            _reading(tenant_id, available, now - timedelta(hours=1), 1000.0),
            _reading(tenant_id, available, now - timedelta(hours=5), 1000.0),
        ])
        await db.commit()

    async with factory() as db:
        out = await status_mod.get_status(Response(), db)

    assert out["status"] == "degraded"
    assert out["db"] == "ok"
    assert out["gateways"]["online"] == 1
    assert out["gateways"]["total"] == 2
    assert out["gateways"]["last_seen_at"].endswith("+00:00")
    assert out["plugs"] == {
        "total": 4, "available": 1, "occupied": 1, "offline": 1, "maintenance": 1,
    }
    assert len(out["reachability"]) == 4
    pct = {r["plug_id"]: r["uptime_7d_pct"] for r in out["reachability"]}[available]
    assert pct is not None and 0 < pct <= 100
    assert len(out["tariff_today"]["hourly"]) == 24


@requires_db
@pytest.mark.asyncio
async def test_status_with_no_public_chargers(factory, _fresh_status_cache):
    from fastapi import Response

    async with factory() as db:
        out = await _fresh_status_cache.get_status(Response(), db)

    assert out["status"] == "no_public_chargers"
    assert out["gateways"] == {"online": 0, "total": 0, "last_seen_at": None}
    assert out["plugs"]["total"] == 0
    assert out["tariff_today"] is None


# ---------------------------------------------------------------------------
# (d) GET /api/admin/health database probe
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.asyncio
async def test_admin_health_database_probe_runs_on_postgres(factory):
    from backend.routers.admin import admin_health

    with patch("backend.routers.admin.state") as st:
        st.session_reaper = None
        st.telemetry_persistence = None
        async with factory() as db:
            out = await admin_health(MagicMock(), db)

    assert isinstance(out["database"]["size_mb"], float) and out["database"]["size_mb"] > 0
    assert isinstance(out["database"]["connections"], int) and out["database"]["connections"] >= 1
