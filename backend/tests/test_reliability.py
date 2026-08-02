"""
Plug reliability (services/reliability.py) tests.

Two tiers:

1. DB-free pure-math unit tests for `_uptime_pct` and `_effective_window` —
   the two helpers plug_uptime_7d splits its logic into specifically so the
   percentage arithmetic and window-capping rule are testable without a
   database (always run).
2. DB-gated tests (need TEST_DATABASE_URL, a throwaway Postgres — same
   skip-locally policy as test_wallet.py / test_disputes.py) that seed
   TelemetryReading rows with controlled hourly gaps against a real plug and
   assert plug_uptime_7d's end-to-end percentage, the young-plug None case,
   last_seen_at, and the TELEMETRY_RETENTION_DAYS window cap.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.services.reliability import (
    DEFAULT_WINDOW_DAYS,
    _effective_window,
    _uptime_pct,
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

db_gated = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]


# ===========================================================================
# 1. DB-free pure math
# ===========================================================================

def test_uptime_pct_full_coverage_is_100():
    assert _uptime_pct(buckets_with_data=24, elapsed_hours=24.0) == 100.0


def test_uptime_pct_zero_coverage_is_0():
    assert _uptime_pct(buckets_with_data=0, elapsed_hours=24.0) == 0.0


def test_uptime_pct_partial_coverage_rounds_to_one_decimal():
    # 10 covered hourly buckets out of a ceil(50.5) = 51-hour window.
    assert _uptime_pct(buckets_with_data=10, elapsed_hours=50.5) == round(10 / 51 * 100, 1)


def test_uptime_pct_never_exceeds_100_even_if_buckets_overcount():
    # Shouldn't happen in practice (DISTINCT bucket count can't exceed the
    # possible bucket count), but the cap is a deliberate safety net.
    assert _uptime_pct(buckets_with_data=999, elapsed_hours=1.0) == 100.0


def test_uptime_pct_possible_buckets_is_a_ceiling_not_a_floor():
    # elapsed_hours=1.5 -> a trailing partial hour still counts as one whole
    # bucket to fill (ceil(1.5) = 2), so 1/2 = 50%, not 1/1 = 100%.
    assert _uptime_pct(buckets_with_data=1, elapsed_hours=1.5) == 50.0


def test_uptime_pct_guards_against_zero_elapsed_hours():
    # possible_buckets is floored at 1, so this never divides by zero.
    assert _uptime_pct(buckets_with_data=0, elapsed_hours=0.0) == 0.0


def test_effective_window_defaults_to_seven_days_for_an_old_plug():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    plug_created = now - timedelta(days=90)
    cutoff = _effective_window(now, plug_created, retention_days=0)
    assert cutoff == now - timedelta(days=DEFAULT_WINDOW_DAYS)


def test_effective_window_capped_by_a_young_plugs_own_age():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    plug_created = now - timedelta(days=2)
    cutoff = _effective_window(now, plug_created, retention_days=0)
    assert cutoff == plug_created


def test_effective_window_zero_retention_means_unlimited_not_zero():
    """TELEMETRY_RETENTION_DAYS=0 means retention is DISABLED (rows are
    never pruned, per telemetry_persistence.py's own convention) — it must
    NOT collapse the window to zero days."""
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    plug_created = now - timedelta(days=90)
    cutoff = _effective_window(now, plug_created, retention_days=0)
    assert cutoff == now - timedelta(days=7)


def test_effective_window_capped_by_a_narrower_retention_setting():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    plug_created = now - timedelta(days=90)
    cutoff = _effective_window(now, plug_created, retention_days=3)
    assert cutoff == now - timedelta(days=3)


def test_effective_window_retention_wider_than_default_does_not_widen_it():
    # retention_days=30 > the 7-day default -> still capped at 7, never wider.
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    plug_created = now - timedelta(days=90)
    cutoff = _effective_window(now, plug_created, retention_days=30)
    assert cutoff == now - timedelta(days=7)


def test_effective_window_no_plug_created_at_falls_back_to_window_start():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    cutoff = _effective_window(now, None, retention_days=0)
    assert cutoff == now - timedelta(days=7)


# ===========================================================================
# 2. DB-gated: plug_uptime_7d end to end
# ===========================================================================

@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory."""
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


async def _seed_plug(factory, *, created_at, last_telemetry_at=None) -> dict:
    from backend.database.models import Gateway, Plug, Tenant

    tag = uuid.uuid4().hex[:10]
    async with factory() as db:
        tenant = Tenant(name=f"tenant-{tag}")
        db.add(tenant)
        await db.flush()

        u = uuid.uuid4().int
        gateway = Gateway(
            id=f"gw-{tag}", tenant_id=tenant.id, name="Test GW",
            vpn_ip=f"10.{u % 250 + 1}.{(u >> 8) % 250 + 1}.{(u >> 16) % 250 + 1}",
        )
        db.add(gateway)
        await db.flush()

        plug = Plug(
            gateway_id=gateway.id, name="Test Plug", local_ip="10.0.1.1",
            created_at=created_at, last_telemetry_at=last_telemetry_at,
        )
        db.add(plug)
        await db.commit()

        return {"tenant_id": tenant.id, "plug_id": plug.id}


async def _seed_reading(factory, world: dict, recorded_at: datetime):
    from backend.database.models import TelemetryReading

    async with factory() as db:
        db.add(TelemetryReading(
            tenant_id=world["tenant_id"], plug_id=world["plug_id"],
            recorded_at=recorded_at, power_w=100.0, energy_kwh=0.1,
            voltage_v=230.0, current_a=0.5,
        ))
        await db.commit()


async def _load_plug(factory, plug_id: int):
    from sqlalchemy import select

    from backend.database.models import Plug

    async with factory() as db:
        result = await db.execute(select(Plug).where(Plug.id == plug_id))
        return result.scalar_one()


@db_gated
@pytest.mark.asyncio
async def test_plug_uptime_none_for_a_plug_younger_than_min_sample_hours(factory):
    from backend.services.reliability import plug_uptime_7d

    now = datetime.now(timezone.utc)
    world = await _seed_plug(factory, created_at=now - timedelta(hours=1))

    async with factory() as db:
        plug = await _load_plug(factory, world["plug_id"])
        data = await plug_uptime_7d(db, plug)

    assert data["uptime_pct"] is None
    assert data["sample_window_days"] == pytest.approx(1 / 24, abs=0.01)


@db_gated
@pytest.mark.asyncio
async def test_plug_uptime_computes_pct_from_hourly_bucket_coverage(factory):
    """Seed 10 telemetry rows spaced 5 hours apart (10 distinct hourly
    buckets, no two closer than 1 bucket-width) against a plug created
    50.5 hours ago (mid-hour offset so the possible-bucket ceiling is stable
    across the seed-then-verify time gap: ceil(50.5+epsilon) == 51 either
    way). Expected: 10 / 51 * 100, rounded to one decimal."""
    from backend.services.reliability import plug_uptime_7d

    now = datetime.now(timezone.utc)
    world = await _seed_plug(factory, created_at=now - timedelta(hours=50, minutes=30))

    offsets_hours = [1, 6, 11, 16, 21, 26, 31, 36, 41, 46]
    for h in offsets_hours:
        await _seed_reading(factory, world, now - timedelta(hours=h))

    async with factory() as db:
        plug = await _load_plug(factory, world["plug_id"])
        data = await plug_uptime_7d(db, plug)

    assert data["uptime_pct"] == pytest.approx(round(10 / 51 * 100, 1), abs=0.1)
    assert data["sample_window_days"] == pytest.approx(50.5 / 24, abs=0.02)


@db_gated
@pytest.mark.asyncio
async def test_plug_uptime_100_pct_when_every_bucket_in_a_short_window_has_data(factory):
    """A plug created just under 4 hours ago (above MIN_SAMPLE_HOURS) with a
    reading in every one of its elapsed hourly buckets reads 100%."""
    from backend.services.reliability import plug_uptime_7d

    now = datetime.now(timezone.utc)
    world = await _seed_plug(factory, created_at=now - timedelta(hours=3, minutes=30))

    for h in (0, 1, 2, 3):
        await _seed_reading(factory, world, now - timedelta(hours=h, minutes=5))

    async with factory() as db:
        plug = await _load_plug(factory, world["plug_id"])
        data = await plug_uptime_7d(db, plug)

    assert data["uptime_pct"] == 100.0


@db_gated
@pytest.mark.asyncio
async def test_plug_uptime_zero_pct_when_no_readings_in_window(factory):
    from backend.services.reliability import plug_uptime_7d

    now = datetime.now(timezone.utc)
    world = await _seed_plug(factory, created_at=now - timedelta(hours=10))

    async with factory() as db:
        plug = await _load_plug(factory, world["plug_id"])
        data = await plug_uptime_7d(db, plug)

    assert data["uptime_pct"] == 0.0


@db_gated
@pytest.mark.asyncio
async def test_plug_uptime_reports_last_seen_at_from_the_plug_row(factory):
    from backend.services.reliability import plug_uptime_7d

    now = datetime.now(timezone.utc)
    last_seen = now - timedelta(minutes=5)
    world = await _seed_plug(factory, created_at=now - timedelta(hours=10), last_telemetry_at=last_seen)

    async with factory() as db:
        plug = await _load_plug(factory, world["plug_id"])
        data = await plug_uptime_7d(db, plug)

    assert data["last_seen_at"] == last_seen


@db_gated
@pytest.mark.asyncio
async def test_plug_uptime_window_capped_by_telemetry_retention_days(factory, monkeypatch):
    """With TELEMETRY_RETENTION_DAYS=2, a plug created 10 days ago is only
    asked about its last 2 days — a reading from 3 days ago (outside that
    capped window) must NOT count toward the bucket total."""
    monkeypatch.setenv("TELEMETRY_RETENTION_DAYS", "2")
    from backend.services.reliability import plug_uptime_7d

    now = datetime.now(timezone.utc)
    world = await _seed_plug(factory, created_at=now - timedelta(days=10))

    # Outside the 2-day cap — must be excluded.
    await _seed_reading(factory, world, now - timedelta(days=3))
    # Inside the 2-day cap.
    await _seed_reading(factory, world, now - timedelta(hours=1))

    async with factory() as db:
        plug = await _load_plug(factory, world["plug_id"])
        data = await plug_uptime_7d(db, plug)

    assert data["sample_window_days"] == pytest.approx(2.0, abs=0.02)
    # Exactly one of the two seeded readings falls inside the window ->
    # 1 covered bucket out of ceil(48) = 48 possible.
    assert data["uptime_pct"] == pytest.approx(round(1 / 48 * 100, 1), abs=0.1)
