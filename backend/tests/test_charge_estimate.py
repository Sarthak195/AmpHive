"""DB-free tests for the C3 pre-start estimate (services/estimate.py and
GET /api/plugs/{id}/estimate). The estimate is display only."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

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
