"""
Tests for the driver-side gap endpoints (redesign/ui-v3 contract §4
"Driver gaps"):

- GET  /api/sessions/{session_id}    (receipt/detail, routers/sessions.py)
- GET  /api/sessions/history         (paginated {total, items} + plug_name)
- GET  /api/me/stats                 (month + lifetime aggregates)
- GET  /api/sessions/disputes/my     (the caller's disputes)
- GET  /api/plugs/{id}/tariff-preview (routers/plugs.py)
- DELETE /api/groups/{id}/leave      (routers/groups.py)

DB-free: the mocked-AsyncSession pattern from test_admin_router.py /
test_audit_log.py — route functions are called directly with an AsyncMock
db whose execute() side-effects supply each query's result in call order.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from backend.database.models import (
    DisputeStatus,
    SessionStatus,
    UserRole,
)
from backend.routers.groups import leave_group
from backend.routers.plugs import get_plug_tariff_preview
from backend.routers.sessions import (
    get_my_disputes,
    get_my_stats,
    get_session_detail,
    get_session_history,
    get_session_telemetry,
)
from backend.routers.sessions import (
    router as sessions_router,
)
from backend.services import insights


def _user(user_id=7, role=UserRole.DRIVER, tenant_id=None):
    u = MagicMock()
    u.id = user_id
    u.email = "driver@amphive.test"
    u.role = role
    u.tenant_id = tenant_id
    return u


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _scalar(value):
    r = MagicMock()
    r.scalar.return_value = value
    return r


def _first(value):
    r = MagicMock()
    r.first.return_value = value
    return r


def _all(rows):
    r = MagicMock()
    r.all.return_value = list(rows)
    return r


def _scalars_all(rows):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(rows)
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()  # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db.delete = AsyncMock()
    return db


# --- Route ordering ----------------------------------------------------------
# GET /api/sessions/{session_id} is a catch-all in the /api/sessions/* GET
# namespace (a non-int segment 422s, it never falls through), so every static
# GET sibling must be registered BEFORE it.


def test_session_detail_route_registered_after_static_siblings():
    get_paths = [
        r.path for r in sessions_router.routes
        if isinstance(r, APIRoute) and "GET" in r.methods
    ]
    catch_all = get_paths.index("/api/sessions/{session_id}")
    for static in (
        "/api/sessions/active",
        "/api/sessions/history",
        "/api/sessions/queued",
        "/api/sessions/disputes/my",
        "/api/me/stats",
    ):
        assert static in get_paths, f"{static} route is missing"
        assert get_paths.index(static) < catch_all, (
            f"{static} must be registered before /api/sessions/{{session_id}}"
        )


# --- GET /api/sessions/{session_id} ------------------------------------------


def _session(
    session_id=42, user_id=7, tenant_id=1, plug_id=3,
    status=SessionStatus.COMPLETED,
    energy_kwh=1.5, coins_spent=Decimal("12.00"),
    rate=Decimal("8.00"), ended=True,
):
    s = MagicMock()
    s.id = session_id
    s.user_id = user_id
    s.tenant_id = tenant_id
    s.plug_id = plug_id
    s.status = status
    s.energy_kwh = energy_kwh
    s.peak_power_w = 2300.44
    s.coins_spent = coins_spent
    s.rate_coins_per_kwh = rate
    s.settled_cost_coins = None
    s.rate_segment_start_kwh = None
    s.started_at = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    s.ended_at = (
        datetime(2026, 7, 20, 11, 30, tzinfo=timezone.utc) if ended else None
    )
    s.max_kwh = 30.0
    s.max_duration_seconds = 14400
    return s


def _ledger(amount=Decimal("-12.00"), balance_after=Decimal("88.00"),
            description="Charging session on Plug A: 1.500 kWh"):
    ledger = MagicMock()
    ledger.amount = amount
    ledger.balance_after = balance_after
    ledger.description = description
    return ledger


@pytest.mark.asyncio
async def test_session_detail_receipt_shape_for_owner():
    session = _session()
    ledger = _ledger(
        description=(
            "Charging session on Plug A: 1.500 kWh "
            "[auto-stopped: energy limit reached]"
        )
    )
    db = _db(_first((session, "Plug A")), _scalar_one_or_none(ledger))

    resp = await get_session_detail(42, _user(user_id=7), db)

    assert resp == {
        "status": "completed",
        "session_id": 42,
        "plug_id": 3,
        "plug_name": "Plug A",
        "energy_kwh": 1.5,
        "peak_power_w": 2300.4,
        "price_per_kwh": 8.0,
        "settled_cost_coins": None,
        "coins_spent": 12.0,
        "idle_fee_coins": 0.0,    # [Idle fee C1] none on this session
        "shortfall_coins": 0.0,   # 1.5 kWh * 8.00 == 12.00 collected
        "balance_before": 100.0,  # 88.00 - (-12.00)
        "balance_remaining": 88.0,
        "duration_sec": 5400,
        "started_at": "2026-07-20T10:00:00+00:00",
        "ended_at": "2026-07-20T11:30:00+00:00",
        "max_kwh": 30.0,
        "max_duration_seconds": 14400,
        "reason": "auto-stopped: energy limit reached",
    }


@pytest.mark.asyncio
async def test_session_detail_shortfall_derived_from_billed_cost():
    # Billed 12.00 (1.5 kWh * 8.00) but only 10.00 collected -> 2.00 shortfall.
    session = _session(coins_spent=Decimal("10.00"))
    db = _db(
        _first((session, "Plug A")),
        _scalar_one_or_none(_ledger(amount=Decimal("-10.00"))),
    )

    resp = await get_session_detail(42, _user(user_id=7), db)

    assert resp["coins_spent"] == 10.0
    assert resp["shortfall_coins"] == 2.0


@pytest.mark.asyncio
async def test_session_detail_active_session_has_no_ledger_fields():
    session = _session(status=SessionStatus.ACTIVE,
                       coins_spent=Decimal("0.00"), ended=False)
    db = _db(_first((session, "Plug A")), _scalar_one_or_none(None))

    resp = await get_session_detail(42, _user(user_id=7), db)

    assert resp["status"] == "active"
    assert resp["shortfall_coins"] == 0.0  # nothing billed yet
    assert resp["balance_before"] is None
    assert resp["balance_remaining"] is None
    assert resp["duration_sec"] is None
    assert resp["ended_at"] is None
    assert resp["reason"] is None


@pytest.mark.asyncio
async def test_session_detail_unknown_session_404():
    db = _db(_first(None))
    with pytest.raises(HTTPException) as exc:
        await get_session_detail(999, _user(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_session_detail_foreign_driver_gets_404_not_403():
    session = _session(user_id=8)  # someone else's
    db = _db(_first((session, "Plug A")))
    with pytest.raises(HTTPException) as exc:
        await get_session_detail(42, _user(user_id=7), db)
    assert exc.value.status_code == 404  # existence isn't leaked


@pytest.mark.asyncio
async def test_session_detail_cross_tenant_cpo_gets_404():
    session = _session(tenant_id=1)
    db = _db(_first((session, "Plug A")))
    with pytest.raises(HTTPException) as exc:
        await get_session_detail(
            42, _user(user_id=99, role=UserRole.CPO, tenant_id=2), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_session_detail_same_tenant_cpo_and_admin_allowed():
    for viewer in (
        _user(user_id=99, role=UserRole.CPO, tenant_id=1),
        _user(user_id=100, role=UserRole.ADMIN),
    ):
        session = _session(tenant_id=1)
        db = _db(_first((session, "Plug A")), _scalar_one_or_none(_ledger()))
        resp = await get_session_detail(42, viewer, db)
        assert resp["session_id"] == 42


# --- GET /api/sessions/history ------------------------------------------------


@pytest.mark.asyncio
async def test_history_paginated_shape_with_plug_name():
    s = _session()
    db = _db(_scalar(3), _all([(s, "Plug A")]))

    resp = await get_session_history(_user(user_id=7), db, limit=1, offset=2)

    assert resp["total"] == 3
    assert resp["items"] == [{
        "id": 42,
        "plug_id": 3,
        "plug_name": "Plug A",
        "started_at": "2026-07-20T10:00:00+00:00",
        "ended_at": "2026-07-20T11:30:00+00:00",
        "energy_kwh": 1.5,
        "coins_spent": 12.0,
        "status": "completed",
    }]


@pytest.mark.asyncio
async def test_history_clamps_limit_to_200():
    db = _db(_scalar(0), _all([]))
    resp = await get_session_history(_user(), db, limit=5000, offset=-3)
    assert resp == {"total": 0, "items": []}
    # The page query (second execute) must carry the clamped values.
    stmt = db.execute.call_args_list[1][0][0]
    assert stmt._limit_clause.value == 200
    assert stmt._offset_clause.value == 0


# --- GET /api/me/stats ---------------------------------------------------------


@pytest.mark.asyncio
async def test_my_stats_month_and_lifetime_buckets():
    db = _db(
        _first((2, 3.5, Decimal("28.00"))),     # month
        _first((10, 50.25, Decimal("400.00"))), # lifetime
        _all([]),                               # series gross rows
        _all([]),                               # series refund rows
    )

    resp = await get_my_stats(_user(user_id=7), db)

    assert resp["month"] == {"energy_kwh": 3.5, "spend_coins": 28.0, "sessions": 2}
    assert resp["lifetime"] == {"energy_kwh": 50.25, "spend_coins": 400.0, "sessions": 10}
    # Estimates ride along with the factors used.
    assert resp["impact"]["assumptions"]["estimate"] is True
    assert resp["impact"]["lifetime"]["co2_avoided_kg"] == insights.estimate_impact(50.25)["co2_avoided_kg"]


@pytest.mark.asyncio
async def test_my_stats_zero_history():
    db = _db(_first((0, 0, 0)), _first((0, 0, 0)), _all([]), _all([]))
    resp = await get_my_stats(_user(), db)
    assert resp["month"] == {"energy_kwh": 0.0, "spend_coins": 0.0, "sessions": 0}
    assert resp["lifetime"] == {"energy_kwh": 0.0, "spend_coins": 0.0, "sessions": 0}
    assert len(resp["series"]) == 12
    assert all(m["sessions"] == 0 and m["spend_coins"] == 0 for m in resp["series"])
    assert resp["impact"]["month"]["co2_avoided_kg"] == 0


@pytest.mark.asyncio
async def test_my_stats_series_nets_refunds_and_zero_fills():
    now = datetime.now(timezone.utc)
    keys = insights.month_keys(now)
    this_m = datetime(now.year, now.month, 1)  # date_trunc returns a naive timestamp
    db = _db(
        _first((1, 2.0, Decimal("10"))),
        _first((1, 2.0, Decimal("10"))),
        _all([(this_m, 3, 6.5, Decimal("32.50"))]),
        _all([(this_m, Decimal("5.00"))]),
    )

    resp = await get_my_stats(_user(), db)

    series = resp["series"]
    assert [m["month"] for m in series] == keys
    assert series[-1] == {
        "month": keys[-1], "energy_kwh": 6.5, "spend_coins": 27.5, "sessions": 3,
    }
    assert series[0]["sessions"] == 0
    # Refunds are a separate aggregate query: 4 executes, no fan-out join.
    assert db.execute.call_count == 4


def test_month_keys_cross_year_boundary():
    keys = insights.month_keys(datetime(2026, 2, 10, tzinfo=timezone.utc))
    assert keys[0] == "2025-03" and keys[-1] == "2026-02" and len(keys) == 12


def test_estimate_impact_nets_grid_against_petrol():
    out = insights.estimate_impact(10)
    expected_petrol = 10 * insights.EV_KM_PER_KWH / insights.ICE_KM_PER_L * insights.PETROL_CO2_KG_PER_L
    assert out["petrol_co2_kg"] == round(expected_petrol, 2)
    assert out["grid_co2_kg"] == round(10 * insights.GRID_EMISSION_FACTOR_KG_PER_KWH, 2)
    assert out["co2_avoided_kg"] == round(expected_petrol - 10 * insights.GRID_EMISSION_FACTOR_KG_PER_KWH, 2)
    assert insights.estimate_impact(-3)["co2_avoided_kg"] == 0


def test_insights_env_falls_back_on_bad_values(monkeypatch):
    monkeypatch.setenv("ICE_KM_PER_L", "abc")
    assert insights._float_env("ICE_KM_PER_L", 15.0) == 15.0
    monkeypatch.setenv("ICE_KM_PER_L", "-2")
    assert insights._float_env("ICE_KM_PER_L", 15.0) == 15.0
    monkeypatch.setenv("ICE_KM_PER_L", "20")
    assert insights._float_env("ICE_KM_PER_L", 15.0) == 20.0


# --- GET /api/sessions/disputes/my ---------------------------------------------


def _dispute(dispute_id=5, status=DisputeStatus.APPROVED,
             refund=Decimal("4.50")):
    d = MagicMock()
    d.id = dispute_id
    d.session_id = 42
    d.status = status
    d.reason = "Charger stopped early"
    d.resolution_note = "Partial refund" if refund is not None else None
    d.refund_coins = refund
    d.created_at = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)
    d.resolved_at = (
        datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
        if status != DisputeStatus.OPEN else None
    )
    return d


@pytest.mark.asyncio
async def test_my_disputes_shape():
    db = _db(_scalars_all([
        _dispute(),
        _dispute(dispute_id=6, status=DisputeStatus.OPEN, refund=None),
    ]))

    resp = await get_my_disputes(_user(user_id=7), db)

    assert resp[0] == {
        "id": 5,
        "session_id": 42,
        "status": "approved",
        "reason": "Charger stopped early",
        "resolution_note": "Partial refund",
        "refund_coins": 4.5,
        "created_at": "2026-07-19T09:00:00+00:00",
        "resolved_at": "2026-07-20T09:00:00+00:00",
    }
    assert resp[1]["status"] == "open"
    assert resp[1]["refund_coins"] is None
    assert resp[1]["resolved_at"] is None


# --- GET /api/plugs/{id}/tariff-preview ----------------------------------------


def _plug(plug_id=3, group_id=None):
    p = MagicMock()
    p.id = plug_id
    p.group_id = group_id
    return p


def _slot(start_min, end_min, price, days_mask=127):
    s = MagicMock()
    s.start_min = start_min
    s.end_min = end_min
    s.price_per_kwh = price
    s.days_mask = days_mask
    return s


@pytest.mark.asyncio
async def test_tariff_preview_env_fallback_when_chain_empty():
    db = _db(_scalar_one_or_none(_plug()))

    with patch("backend.services.pricing._resolve_tariff_and_tz",
               new=AsyncMock(return_value=(None, "Asia/Kolkata"))):
        resp = await get_plug_tariff_preview(3, _user(), db)

    assert resp["slots"] == []
    assert resp["cheapest_window"] is None
    assert resp["base_price_per_kwh"] == resp["price_now"]
    assert resp["base_price_per_kwh"] > 0


@pytest.mark.asyncio
async def test_tariff_preview_slots_and_price_now():
    tariff = MagicMock()
    tariff.id = 5
    tariff.price_per_kwh = Decimal("8.00")
    # One all-day, every-day slot: it always covers "now".
    all_day = _slot(0, 1440, Decimal("10.00"))
    # A weekday-only evening slot that can never cover an all-day winner
    # (overlap wouldn't be valid data) — only here to check days expansion.
    weekdays = _slot(1080, 1320, Decimal("12.00"), days_mask=0b0011111)

    db = _db(
        _scalar_one_or_none(_plug()),
        _scalars_all([weekdays, all_day]),
    )

    with patch("backend.services.pricing._resolve_tariff_and_tz",
               new=AsyncMock(return_value=(tariff, "Asia/Kolkata"))):
        resp = await get_plug_tariff_preview(3, _user(), db)

    assert resp["base_price_per_kwh"] == 8.0
    assert resp["price_now"] == 10.0  # the covering slot wins over the flat base
    # The 12.00 evening slot is dearer than now, so no cheaper window exists.
    assert resp["cheapest_window"] is None
    # Sorted by (start_minute, end_minute); days expands the mask, Mon=0.
    assert resp["slots"] == [
        {"days": [0, 1, 2, 3, 4, 5, 6], "start_minute": 0,
         "end_minute": 1440, "price_per_kwh": 10.0},
        {"days": [0, 1, 2, 3, 4], "start_minute": 1080,
         "end_minute": 1320, "price_per_kwh": 12.0},
    ]


@pytest.mark.asyncio
async def test_tariff_preview_unknown_plug_404():
    db = _db(_scalar_one_or_none(None))
    with pytest.raises(HTTPException) as exc:
        await get_plug_tariff_preview(999, _user(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_tariff_preview_private_group_non_member_403():
    group = MagicMock()
    group.id = 2
    group.name = "Society"
    group.is_public = False
    db = _db(
        _scalar_one_or_none(_plug(group_id=2)),  # plug
        _scalar_one_or_none(group),              # its private group
        _scalar_one_or_none(None),               # no membership
    )
    with pytest.raises(HTTPException) as exc:
        await get_plug_tariff_preview(3, _user(), db)
    assert exc.value.status_code == 403


# --- DELETE /api/groups/{id}/leave ---------------------------------------------


@pytest.mark.asyncio
async def test_leave_group_deletes_membership():
    membership = MagicMock()
    db = _db(_scalar_one_or_none(membership))

    resp = await leave_group(2, _user(user_id=7), db)

    assert resp == {"status": "left", "group_id": 2}
    db.delete.assert_awaited_once_with(membership)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_leave_group_not_a_member_404():
    db = _db(_scalar_one_or_none(None))

    with pytest.raises(HTTPException) as exc:
        await leave_group(2, _user(user_id=7), db)

    assert exc.value.status_code == 404
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


# --- GET /api/sessions/{session_id}/telemetry --------------------------------


@pytest.mark.asyncio
async def test_session_telemetry_buckets_and_shape():
    session = _session(status=SessionStatus.COMPLETED)  # 90 min window
    rows = [(0, 1500.04, 1600.0, 6.5, 0.012), (3, 2300.0, 2400.5, 10.0, 0.4)]
    db = _db(_scalar_one_or_none(session), _all(rows))

    resp = await get_session_telemetry(42, _user(user_id=7), db)

    assert resp["bucket_seconds"] == 45  # ceil(5400 / 120)
    assert resp["max_kwh"] == 30.0
    assert resp["status"] == "completed"
    assert resp["points"] == [
        {"offset_sec": 0, "power_w": 1500.0, "peak_power_w": 1600.0,
         "current_a": 6.5, "energy_kwh": 0.012},
        {"offset_sec": 135, "power_w": 2300.0, "peak_power_w": 2400.5,
         "current_a": 10.0, "energy_kwh": 0.4},
    ]
    # One query stays in SQL: the grouped aggregate, filtered by plug and window.
    sql = str(db.execute.await_args_list[1].args[0])
    assert "GROUP BY" in sql and "telemetry_readings.plug_id" in sql


@pytest.mark.asyncio
async def test_session_telemetry_empty_session_has_no_points():
    db = _db(_scalar_one_or_none(_session()), _all([]))
    resp = await get_session_telemetry(42, _user(user_id=7), db)
    assert resp["points"] == []


@pytest.mark.asyncio
async def test_session_telemetry_access_matches_detail():
    # Unknown, foreign driver and cross-tenant CPO all 404; no readings query runs.
    for sess, viewer in (
        (None, _user()),
        (_session(user_id=8), _user(user_id=7)),
        (_session(tenant_id=1), _user(user_id=99, role=UserRole.CPO, tenant_id=2)),
    ):
        db = _db(_scalar_one_or_none(sess))
        with pytest.raises(HTTPException) as exc:
            await get_session_telemetry(42, viewer, db)
        assert exc.value.status_code == 404
        assert db.execute.await_count == 1
    for viewer in (
        _user(user_id=99, role=UserRole.CPO, tenant_id=1),
        _user(user_id=100, role=UserRole.ADMIN),
    ):
        db = _db(_scalar_one_or_none(_session(tenant_id=1)), _all([]))
        assert (await get_session_telemetry(42, viewer, db))["points"] == []


def test_public_config_ships_co2_factor_for_the_receipt():
    from backend.main import public_config

    assert public_config()["co2_avoided_kg_per_kwh"] == round(insights.co2_avoided_kg_per_kwh(), 4)


@pytest.mark.asyncio
async def test_my_stats_series_sql_groups_by_one_literal_month_expr():
    """The SELECT and GROUP BY must carry the same inline month expression
    (no bind params, which Postgres would reject as not grouped)."""
    from sqlalchemy.dialects import postgresql

    db = _db(_first((0, 0, 0)), _first((0, 0, 0)), _all([]), _all([]))
    await get_my_stats(_user(), db)
    sql = str(db.execute.call_args_list[2].args[0].compile(dialect=postgresql.dialect())).lower()
    expr = "date_trunc('month', timezone('utc', charging_sessions.started_at))"
    assert sql.count(expr) == 2
