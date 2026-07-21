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
)
from backend.routers.sessions import (
    router as sessions_router,
)


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
    )

    resp = await get_my_stats(_user(user_id=7), db)

    assert resp == {
        "month": {"energy_kwh": 3.5, "spend_coins": 28.0, "sessions": 2},
        "lifetime": {"energy_kwh": 50.25, "spend_coins": 400.0, "sessions": 10},
    }


@pytest.mark.asyncio
async def test_my_stats_zero_history():
    db = _db(_first((0, 0, 0)), _first((0, 0, 0)))
    resp = await get_my_stats(_user(), db)
    assert resp["month"] == {"energy_kwh": 0.0, "spend_coins": 0.0, "sessions": 0}
    assert resp["lifetime"] == {"energy_kwh": 0.0, "spend_coins": 0.0, "sessions": 0}


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
