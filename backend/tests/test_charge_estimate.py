"""DB-free tests for the C3 pre-start estimate (services/estimate.py and
GET /api/plugs/{id}/estimate). The estimate is display only."""
import itertools
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from backend.database.models import UserRole
from backend.routers.plugs import get_plug_charge_estimate
from backend.services.billing import session_cost
from backend.services.estimate import (
    estimate_charge,
    limited_energy_and_duration,
    resolve_power_basis,
    walk_cost,
)
from backend.services.money import energy_cost

IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: no tzdata on CI Windows boxes
FLAT = Decimal("10.00")


def _slot(start, end, price, mask=127):
    return SimpleNamespace(
        start_min=start, end_min=end, price_per_kwh=Decimal(str(price)), days_mask=mask
    )


def _at(h, mi=0):  # 2026-11-17 is a Tuesday
    return datetime(2026, 11, 17, h, mi, tzinfo=IST)


PEAK = [_slot(1080, 1320, 15)]  # 18:00-22:00 at 15, flat 10 otherwise


def test_window_crossing_prices_each_window():
    # 2 h at 2 kW from 21:00: 1 h in the 15 peak, then 1 h at the flat 10.
    cost, changes = walk_cost(PEAK, FLAT, IST, _at(21), 120, 2000.0)
    assert cost == energy_cost(2.0, 15) + energy_cost(2.0, 10) == Decimal("50.00")
    assert changes == [(_at(22), FLAT)]


def test_no_crossing_is_one_rate_and_no_changes():
    cost, changes = walk_cost(PEAK, FLAT, IST, _at(12), 60, 2000.0)
    assert cost == Decimal("20.00")
    assert changes == []


def test_flat_cost_matches_billing_with_no_extra_fee_or_gst():
    # Tariffs are GST-inclusive and billing adds no fee: cost is energy * rate.
    cost, _ = walk_cost([], FLAT, IST, _at(12), 90, 2000.0)
    billed = SimpleNamespace(
        rate_coins_per_kwh=FLAT, rate_segment_start_kwh=None, settled_cost_coins=None
    )
    assert cost == session_cost(billed, 3.0) == Decimal("30.00")


def test_kwh_limit_binds_first():
    # 2 kW, 3 kWh needs 90 min; the 4 h cap is looser.
    energy, minutes = limited_energy_and_duration(2000.0, 3.0, 240)
    assert (round(energy, 6), round(minutes, 6)) == (3.0, 90.0)


def test_duration_limit_binds_first():
    # 2 kW for 30 min is 1 kWh; the 5 kWh cap is looser.
    energy, minutes = limited_energy_and_duration(2000.0, 5.0, 30)
    assert (round(energy, 6), minutes) == (1.0, 30.0)


def test_single_limits():
    assert limited_energy_and_duration(2000.0, 1.0, None) == (1.0, 30.0)
    assert limited_energy_and_duration(2000.0, None, 60) == (2.0, 60.0)


def _first(value):
    r = MagicMock()
    r.first.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


def _plug(max_current_a=None, group_id=None):
    return SimpleNamespace(id=3, group_id=group_id, max_current_a=max_current_a)


NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_no_history_falls_back_to_rated_power():
    power, basis = await resolve_power_basis(_db(_first(None)), _plug(), 7, NOW)
    assert (power, basis) == (16 * 230.0, "rated_power")
    power, _ = await resolve_power_basis(_db(_first(None)), _plug(10), 7, NOW)
    assert power == 2300.0


@pytest.mark.asyncio
async def test_recent_session_average_is_the_basis():
    row = SimpleNamespace(
        energy_kwh=2.0,
        started_at=NOW - timedelta(hours=2),
        ended_at=NOW - timedelta(hours=1),
    )
    # 2 kWh over the 1 h the session ran = 2 kW.
    power, basis = await resolve_power_basis(_db(_first(row)), _plug(), 7, NOW)
    assert (round(power), basis) == (2000, "recent_session")


@pytest.mark.asyncio
async def test_too_short_history_is_ignored_and_average_is_capped_at_rated():
    short = SimpleNamespace(
        energy_kwh=1.0, started_at=NOW - timedelta(minutes=3), ended_at=NOW
    )
    assert (await resolve_power_basis(_db(_first(short)), _plug(), 7, NOW))[1] == "rated_power"
    spike = SimpleNamespace(
        energy_kwh=50.0, started_at=NOW - timedelta(hours=1), ended_at=NOW
    )
    power, basis = await resolve_power_basis(_db(_first(spike)), _plug(), 7, NOW)
    assert (power, basis) == (16 * 230.0, "recent_session")


@pytest.mark.asyncio
async def test_estimate_charge_shape_with_flat_env_fallback():
    plug = _plug()
    with patch(
        "backend.services.estimate._resolve_tariff_and_tz",
        new=AsyncMock(return_value=(None, "Asia/Kolkata")),
    ):
        out = await estimate_charge(
            _db(_first(None)), plug, 7, max_kwh=None, max_duration_min=60,
            start_at=_at(12),
        )
    assert out["is_estimate"] is True
    assert out["basis"] == "rated_power"
    assert out["duration_min"] == 60
    assert out["energy_kwh"] == round(3.68, 2)
    assert isinstance(out["cost_coins"], str)
    assert out["rate_changes"] == []
    assert out["finish_at"] == _at(13).isoformat()


# --- route: same visibility as GET /api/plugs/{id} ----------------------------


def _user(role=UserRole.DRIVER):
    u = MagicMock()
    u.id = 7
    u.role = role
    return u


def _one(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _private_group_db():
    group = MagicMock()
    group.id = 2
    group.name = "Society"
    group.is_public = False
    return _db(_one(_plug(group_id=2)), _one(group), _one(None))


@pytest.mark.asyncio
async def test_private_plug_non_member_gets_403_like_plug_detail():
    with pytest.raises(HTTPException) as exc:
        await get_plug_charge_estimate(3, 1.0, None, None, _user(), _private_group_db())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_private_plug_viewer_gets_404_like_plug_detail():
    with pytest.raises(HTTPException) as exc:
        await get_plug_charge_estimate(
            3, 1.0, None, None, _user(UserRole.CPO_VIEWER), _private_group_db()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_unknown_plug_404_and_missing_limit_422():
    with pytest.raises(HTTPException) as exc:
        await get_plug_charge_estimate(9, 1.0, None, None, _user(), _db(_one(None)))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await get_plug_charge_estimate(3, None, None, None, _user(), _db())
    assert exc.value.status_code == 422


def test_low_power_kwh_limit_is_capped_at_24h():
    # 120 W with a 100 kWh limit would be ~833 h; the estimate stops at 24 h
    # and reports the energy delivered within the cap.
    energy, minutes = limited_energy_and_duration(120.0, 100.0, None)
    assert minutes == 1440.0
    assert round(energy, 6) == round(0.12 * 24, 6)


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [timedelta(days=-2), timedelta(days=31), timedelta(days=3000)])
async def test_start_at_out_of_range_is_422(delta):
    start = datetime.now(timezone.utc) + delta
    with pytest.raises(HTTPException) as exc:
        await get_plug_charge_estimate(3, None, 60, start, _user(), _db(_one(_plug())))
    assert exc.value.status_code == 422


# --- [Integration C3 + C4] the estimate respects the plug-hours clamp ---------


def _hours_plug(opening_hours=None, max_session_minutes=None):
    p = _plug()
    p.name = "Bay 1"
    p.opening_hours = opening_hours
    p.max_session_minutes = max_session_minutes
    return p


async def _estimate(plug, **kw):
    # Fixed-offset IST for the hours math too (no tzdata on CI Windows boxes).
    with patch(
        "backend.services.estimate._resolve_tariff_and_tz",
        new=AsyncMock(return_value=(None, "Asia/Kolkata")),
    ), patch("backend.services.plug_hours._zone", new=lambda _name: IST):
        return await estimate_charge(_db(_first(None)), plug, 7, **kw)


@pytest.mark.asyncio
async def test_estimate_ends_at_closing_time_like_the_start_clamp():
    # Open 08:00-13:00 every day; a 120 min ask at 12:00 local ends at 13:00.
    plug = _hours_plug(opening_hours=[{"days": 127, "start": 480, "end": 780}])
    out = await _estimate(plug, max_kwh=None, max_duration_min=120, start_at=_at(12))
    assert out["duration_min"] == 60
    assert out["finish_at"] == _at(13).isoformat()
    assert out["energy_kwh"] == round(3.68, 2)


@pytest.mark.asyncio
async def test_estimate_respects_max_session_minutes_for_a_kwh_limit():
    plug = _hours_plug(max_session_minutes=45)
    out = await _estimate(plug, max_kwh=50.0, max_duration_min=None, start_at=_at(12))
    assert out["duration_min"] == 45
    assert out["finish_at"] == _at(12, 45).isoformat()


@pytest.mark.asyncio
async def test_estimate_for_a_closed_plug_is_409_like_the_start():
    plug = _hours_plug(opening_hours=[{"days": 127, "start": 480, "end": 780}])
    with pytest.raises(HTTPException) as exc:
        await _estimate(plug, max_kwh=None, max_duration_min=60, start_at=_at(20))
    assert exc.value.status_code == 409
    assert "closed" in exc.value.detail


# --- route validation over HTTP (FastAPI's own Query rules) -------------------


@pytest.fixture
def estimate_client():
    from fastapi.testclient import TestClient

    import backend.main as main
    from backend.database.db import get_db
    from backend.services.auth import get_current_user

    api = main.app.other_asgi_app
    db = AsyncMock()

    async def _db():
        yield db

    api.dependency_overrides[get_db] = _db
    api.dependency_overrides[get_current_user] = lambda: _user()
    yield TestClient(main.app, raise_server_exceptions=False), db
    for dep in (get_db, get_current_user):
        api.dependency_overrides.pop(dep, None)


@pytest.mark.parametrize(
    "query",
    [
        "max_kwh=0",
        "max_kwh=-1",
        "max_kwh=100.01",
        "max_kwh=abc",
        "max_duration_min=0",
        "max_duration_min=-5",
        "max_duration_min=1441",
        "max_duration_min=1.5",
        "max_duration_min=abc",
        "max_kwh=5&start_at=not-a-date",
        "start_at=2026-11-18T10:00:00Z",  # no limit at all
        "",
    ],
)
def test_estimate_rejects_bad_input_with_422_before_touching_the_database(estimate_client, query):
    client, db = estimate_client
    resp = client.get(f"/api/plugs/3/estimate?{query}")
    assert resp.status_code == 422
    db.execute.assert_not_called()


# --- DB-gated: the real history query (CI only) -------------------------------

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)
ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status", "queued_charge_status",
]
_n = itertools.count(1)


@pytest_asyncio.fixture()
async def factory():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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


async def _seed_history_world(factory):
    """One tenant, two plugs on one gateway, two drivers. Returns ids."""
    from backend.database.models import (
        Gateway,
        GatewayStatus,
        Plug,
        PlugStatus,
        Tenant,
        User,
    )

    n = next(_n)
    async with factory() as db:
        tenant = Tenant(name=f"T{n}", timezone="Asia/Kolkata")
        db.add(tenant)
        await db.flush()
        me = User(email=f"me{n}@example.com", hashed_password="x",
                  full_name="Me", role=UserRole.DRIVER)
        other = User(email=f"other{n}@example.com", hashed_password="x",
                     full_name="Other", role=UserRole.DRIVER)
        gw = Gateway(id=f"gw-e{n}", tenant_id=tenant.id, name="GW",
                     vpn_ip=f"10.8.0.{n}", status=GatewayStatus.ONLINE)
        db.add_all([me, other, gw])
        await db.flush()
        plug = Plug(gateway_id=gw.id, name="Bay", local_ip="10.0.2.1", status=PlugStatus.AVAILABLE)
        plug2 = Plug(gateway_id=gw.id, name="Bay 2", local_ip="10.0.2.2", status=PlugStatus.AVAILABLE)
        db.add_all([plug, plug2])
        await db.commit()
        return {"tenant": tenant.id, "me": me.id, "other": other.id,
                "plug": plug.id, "plug2": plug2.id}


async def _add_session(factory, w, *, user="me", plug="plug", ended_days_ago,
                       seconds, kwh, status=None):
    from backend.database.models import ChargingSession, SessionStatus

    ended = NOW - timedelta(days=ended_days_ago)
    async with factory() as db:
        db.add(ChargingSession(
            tenant_id=w["tenant"], user_id=w[user], plug_id=w[plug],
            started_at=ended - timedelta(seconds=seconds),
            ended_at=None if status == SessionStatus.ACTIVE else ended,
            energy_kwh=kwh, status=status or SessionStatus.COMPLETED,
        ))
        await db.commit()


async def _basis(factory, w):
    from backend.database.models import Plug

    async with factory() as db:
        plug = (await db.execute(select(Plug).where(Plug.id == w["plug"]))).scalar_one()
        return await resolve_power_basis(db, plug, w["me"], NOW)


@requires_db
@pytest.mark.asyncio
async def test_db_history_picks_the_callers_latest_completed_session_here(factory):
    from backend.database.models import SessionStatus

    w = await _seed_history_world(factory)
    # Everything below must be ignored by the query ...
    await _add_session(factory, w, ended_days_ago=40, seconds=3600, kwh=3.0)        # too old
    await _add_session(factory, w, user="other", ended_days_ago=1, seconds=3600, kwh=3.0)
    await _add_session(factory, w, plug="plug2", ended_days_ago=1, seconds=3600, kwh=3.0)
    await _add_session(factory, w, ended_days_ago=0, seconds=3600, kwh=3.0,
                       status=SessionStatus.ACTIVE)                                  # not completed
    # ... and an older completed session loses to the newer one.
    await _add_session(factory, w, ended_days_ago=9, seconds=3600, kwh=2.0)         # 2000 W
    await _add_session(factory, w, ended_days_ago=5, seconds=3600, kwh=1.0)         # 1000 W
    assert await _basis(factory, w) == (1000.0, "recent_session")


@requires_db
@pytest.mark.asyncio
async def test_db_history_minimum_length_and_energy_and_rated_cap(factory):
    w = await _seed_history_world(factory)
    assert await _basis(factory, w) == (16 * 230.0, "rated_power")                   # no history
    await _add_session(factory, w, ended_days_ago=3, seconds=3600, kwh=1.0)
    await _add_session(factory, w, ended_days_ago=2, seconds=299, kwh=1.0)          # 4m59s: too short
    assert await _basis(factory, w) == (16 * 230.0, "rated_power")
    await _add_session(factory, w, ended_days_ago=1, seconds=300, kwh=0.1)          # exactly 5 min
    power, basis = await _basis(factory, w)
    assert basis == "recent_session" and round(power) == 1200
    await _add_session(factory, w, ended_days_ago=0.5, seconds=3600, kwh=10.0)      # 10 kW: capped
    assert await _basis(factory, w) == (16 * 230.0, "recent_session")
