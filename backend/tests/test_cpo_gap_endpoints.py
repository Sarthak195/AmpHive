"""
Tests for the CPO-side gap endpoints (redesign/ui-v3 contract §4 "CPO gaps",
backend/routers/cpo.py):

- GET    /api/cpo/groups/{group_id}/members            (member roster)
- DELETE /api/cpo/groups/{group_id}/members/{user_id}  (revoke, audited)
- GET    /api/cpo/analytics/sessions   ({total, totals, items, sessions} —
                                        server-side aggregates + offset)
- GET    /api/cpo/events               ({total, items} + offset)
- GET    /api/cpo/reservations         ({total, items})
- GET    /api/cpo/invoices             ({total, items})
- GET    /api/cpo/invoices.csv         (CSV export, optional days filter)
- GET    /api/cpo/plugs                (tariff_id serialized for CpoPricing)
- GET    /api/cpo/groups               (tariff_id serialized for CpoPricing)

DB-free: the mocked-AsyncSession pattern from test_admin_router.py /
test_driver_gap_endpoints.py — route functions are called directly with an
AsyncMock db whose execute() side-effects supply each query's result in
call order.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from backend.database.models import (
    PlugStatus, ReservationStatus, SessionStatus, UserRole,
)
from backend.routers.cpo import (
    cpo_analytics_sessions, cpo_export_invoices_csv, cpo_list_events,
    cpo_list_group_members, cpo_list_groups, cpo_list_invoices,
    cpo_list_plugs, cpo_list_reservations, cpo_remove_group_member,
    router as cpo_router,
)


def _cpo(user_id=5, tenant_id=1):
    u = MagicMock()
    u.id = user_id
    u.email = "cpo@amphive.test"
    u.role = UserRole.CPO
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


# --- GET /api/cpo/plugs & /api/cpo/groups: tariff_id serialization -----------


def _plug(plug_id=3, tariff_id=None):
    p = MagicMock()
    p.id = plug_id
    p.name = "Plug A"
    p.gateway_id = "gw-01"
    p.local_ip = "192.168.1.50"
    p.plug_model = "P110"
    p.status = PlugStatus.AVAILABLE
    p.current_power_w = 0.0
    p.group_id = None
    p.latitude = None
    p.longitude = None
    p.max_current_a = None
    p.queued_charging_enabled = None
    p.auto_start_delay_min = None
    p.last_seen_at = None
    p.created_at = None
    p.tariff_id = tariff_id
    return p


@pytest.mark.asyncio
async def test_list_plugs_serializes_tariff_id():
    db = _db(_all([(_plug(plug_id=3, tariff_id=9), None)]))

    resp = await cpo_list_plugs(_cpo(), db)

    assert resp[0]["id"] == 3
    assert resp[0]["tariff_id"] == 9


@pytest.mark.asyncio
async def test_list_plugs_tariff_id_none_when_unassigned():
    db = _db(_all([(_plug(plug_id=3, tariff_id=None), None)]))

    resp = await cpo_list_plugs(_cpo(), db)

    assert resp[0]["tariff_id"] is None


def _group(group_id=12, tariff_id=None):
    g = MagicMock()
    g.id = group_id
    g.name = "Society Block A"
    g.is_public = True
    g.access_code = None
    g.max_current_a = None
    g.created_at = None
    g.tariff_id = tariff_id
    return g


@pytest.mark.asyncio
async def test_list_groups_serializes_tariff_id():
    db = _db(
        _scalars_all([_group(group_id=12, tariff_id=4)]),
        _scalar(2),  # plug_count
        _scalar(0),  # member_count
        _scalar(0),  # pending capacity requests
    )
    with patch(
        "backend.services.caps.measured_circuit_load_a",
        new=AsyncMock(return_value=12.5),
    ):
        resp = await cpo_list_groups(_cpo(), db)

    assert resp[0]["id"] == 12
    assert resp[0]["tariff_id"] == 4


@pytest.mark.asyncio
async def test_list_groups_tariff_id_none_when_unassigned():
    db = _db(
        _scalars_all([_group(group_id=12, tariff_id=None)]),
        _scalar(2),
        _scalar(0),
        _scalar(0),
    )
    with patch(
        "backend.services.caps.measured_circuit_load_a",
        new=AsyncMock(return_value=0.0),
    ):
        resp = await cpo_list_groups(_cpo(), db)

    assert resp[0]["tariff_id"] is None


# --- Route registration -------------------------------------------------------


def test_new_cpo_gap_routes_registered():
    routes = {
        (r.path, m)
        for r in cpo_router.routes
        if isinstance(r, APIRoute)
        for m in r.methods
    }
    for expected in (
        ("/api/cpo/groups/{group_id}/members", "GET"),
        ("/api/cpo/groups/{group_id}/members/{user_id}", "DELETE"),
        ("/api/cpo/invoices.csv", "GET"),
    ):
        assert expected in routes, f"{expected} route is missing"


# --- GET /api/cpo/groups/{group_id}/members -----------------------------------


def _membership(user_id=7):
    m = MagicMock()
    m.id = 3
    m.user_id = user_id
    m.group_id = 12
    m.joined_at = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    return m


@pytest.mark.asyncio
async def test_list_group_members_shape():
    group = MagicMock()
    group.id = 12
    db = _db(
        _scalar_one_or_none(group),
        _all([(_membership(user_id=7), "driver@amphive.test", "Dee River")]),
    )

    resp = await cpo_list_group_members(12, _cpo(), db)

    assert resp == [
        {
            "user_id": 7,
            "email": "driver@amphive.test",
            "full_name": "Dee River",
            "joined_at": "2026-07-01T09:00:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_list_group_members_cross_tenant_404():
    db = _db(_scalar_one_or_none(None))  # tenant-scoped group lookup misses
    with pytest.raises(HTTPException) as exc:
        await cpo_list_group_members(12, _cpo(tenant_id=2), db)
    assert exc.value.status_code == 404


# --- DELETE /api/cpo/groups/{group_id}/members/{user_id} ----------------------


@pytest.mark.asyncio
async def test_remove_group_member_deletes_and_audits():
    group = MagicMock()
    group.id = 12
    group.name = "Society Block A"
    membership = _membership(user_id=7)
    db = _db(_scalar_one_or_none(group), _scalar_one_or_none(membership))

    with patch("backend.routers.cpo.try_record_audit", new=AsyncMock()) as audit:
        resp = await cpo_remove_group_member(12, 7, _cpo(), db)

    db.delete.assert_awaited_once_with(membership)
    db.commit.assert_awaited()
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "group.member_remove"
    assert kwargs["target_type"] == "group"
    assert kwargs["target_id"] == 12
    assert resp == {"status": "removed", "group_id": 12, "user_id": 7}


@pytest.mark.asyncio
async def test_remove_group_member_cross_tenant_group_404():
    db = _db(_scalar_one_or_none(None))
    with pytest.raises(HTTPException) as exc:
        await cpo_remove_group_member(12, 7, _cpo(tenant_id=2), db)
    assert exc.value.status_code == 404
    db.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_group_member_not_a_member_404():
    group = MagicMock()
    db = _db(_scalar_one_or_none(group), _scalar_one_or_none(None))
    with pytest.raises(HTTPException) as exc:
        await cpo_remove_group_member(12, 99, _cpo(), db)
    assert exc.value.status_code == 404
    db.delete.assert_not_awaited()


# --- GET /api/cpo/analytics/sessions -------------------------------------------


def _session(session_id=42, plug_id=3, user_id=7):
    s = MagicMock()
    s.id = session_id
    s.plug_id = plug_id
    s.user_id = user_id
    s.started_at = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    s.ended_at = datetime(2026, 7, 20, 11, 30, tzinfo=timezone.utc)
    s.energy_kwh = 1.5
    s.coins_spent = Decimal("12.00")
    s.status = SessionStatus.COMPLETED
    return s


@pytest.mark.asyncio
async def test_analytics_sessions_server_side_totals_not_page_sums():
    # Aggregate row says 250 sessions / 500.1234 kWh / 4000.505 coins across
    # the WHOLE filtered set; the page itself only carries one row — the
    # totals must come from the aggregate, not the page (the old client-side
    # sum truncated at the page size).
    db = _db(
        _first((250, 500.1234, Decimal("4000.505"))),
        _all([(_session(), "Plug A", "driver@amphive.test")]),
    )

    resp = await cpo_analytics_sessions(_cpo(), db, limit=1)

    assert resp["total"] == 250
    assert resp["totals"] == {
        "count": 250,
        "energy_kwh": 500.123,
        "revenue_coins": 4000.51,
    }
    assert len(resp["items"]) == 1
    assert resp["items"][0]["id"] == 42
    assert resp["items"][0]["plug_name"] == "Plug A"
    assert resp["items"][0]["user_email"] == "driver@amphive.test"
    assert resp["items"][0]["energy_kwh"] == 1.5
    assert resp["items"][0]["coins_spent"] == Decimal("12.00")
    assert resp["items"][0]["duration_minutes"] == 90.0
    # Legacy alias for pre-contract callers that read a bare list.
    assert resp["sessions"] is resp["items"]


@pytest.mark.asyncio
async def test_analytics_sessions_orphan_fallbacks_preserved():
    db = _db(
        _first((1, 1.5, Decimal("12.00"))),
        _all([(_session(), None, None)]),  # orphaned plug/user references
    )
    resp = await cpo_analytics_sessions(_cpo(), db)
    assert resp["items"][0]["plug_name"] == "Plug #3"
    assert resp["items"][0]["user_email"] == "unknown"


@pytest.mark.asyncio
async def test_analytics_sessions_invalid_status_400():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await cpo_analytics_sessions(_cpo(), db, status_filter="bogus")
    assert exc.value.status_code == 400
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_analytics_sessions_empty_set_zero_totals():
    db = _db(_first((0, 0, 0)), _all([]))
    resp = await cpo_analytics_sessions(_cpo(), db, offset=50)
    assert resp == {
        "total": 0,
        "totals": {"count": 0, "energy_kwh": 0.0, "revenue_coins": 0.0},
        "items": [],
        "sessions": [],
    }


# --- GET /api/cpo/events --------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_paginated_shape():
    ev = MagicMock()
    ev.id = 9
    ev.gateway_id = "gw-01"
    ev.plug_id = 3
    ev.event_type = "OVERCURRENT_CUTOFF"
    ev.severity = "critical"
    ev.detail = "16.2 A on a 16 A cap"
    ev.acknowledged = False
    ev.created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    db = _db(_scalar(37), _scalars_all([ev]))

    resp = await cpo_list_events(_cpo(), db, limit=1, offset=10)

    assert resp["total"] == 37
    assert len(resp["items"]) == 1
    item = resp["items"][0]
    assert item.id == 9
    assert item.event_type == "OVERCURRENT_CUTOFF"
    assert item.created_at == "2026-07-20T12:00:00+00:00"


# --- GET /api/cpo/reservations ---------------------------------------------------


@pytest.mark.asyncio
async def test_list_reservations_paginated_shape():
    r = MagicMock()
    r.id = 4
    r.plug_id = 3
    r.user_id = 7
    r.start_at = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)
    r.end_at = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    r.status = ReservationStatus.BOOKED
    r.session_id = None
    r.created_at = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    db = _db(_scalar(12), _all([(r, "Plug A", "driver@amphive.test", "Dee River")]))

    with patch(
        "backend.routers.cpo.expire_lapsed_reservations",
        new=AsyncMock(return_value=0),
    ):
        resp = await cpo_list_reservations(_cpo(), db)

    assert resp["total"] == 12
    assert resp["items"] == [
        {
            "id": 4,
            "plug_id": 3,
            "plug_name": "Plug A",
            "user_id": 7,
            "user_email": "driver@amphive.test",
            "user_name": "Dee River",
            "start_at": "2026-07-22T09:00:00+00:00",
            "end_at": "2026-07-22T10:00:00+00:00",
            "status": "booked",
            "session_id": None,
            "created_at": "2026-07-21T08:00:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_list_reservations_invalid_status_400():
    db = _db()
    with patch(
        "backend.routers.cpo.expire_lapsed_reservations",
        new=AsyncMock(return_value=0),
    ):
        with pytest.raises(HTTPException) as exc:
            await cpo_list_reservations(_cpo(), db, status="bogus")
    assert exc.value.status_code == 400


# --- GET /api/cpo/invoices --------------------------------------------------------


def _invoice():
    inv = MagicMock()
    inv.id = 6
    inv.invoice_number = "ACME-2026-27-00001"
    inv.tenant_id = 1
    inv.session_id = 42
    inv.driver_user_id = 7
    inv.issued_at = datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)
    inv.amount_coins = Decimal("12.00")
    inv.taxable_value_inr = Decimal("10.17")
    inv.gst_rate_pct = Decimal("18.00")
    inv.gst_amount_inr = Decimal("1.83")
    inv.total_inr = Decimal("12.00")
    inv.seller_legal_name = "Acme Charging Pvt Ltd"
    inv.seller_gstin = None
    inv.energy_kwh = 1.5
    inv.rate_coins_per_kwh = Decimal("8.00")
    return inv


@pytest.mark.asyncio
async def test_list_invoices_paginated_shape():
    db = _db(_scalar(23), _scalars_all([_invoice()]))

    resp = await cpo_list_invoices(_cpo(), db, limit=1, offset=5)

    assert resp["total"] == 23
    assert len(resp["items"]) == 1
    assert resp["items"][0]["invoice_number"] == "ACME-2026-27-00001"
    assert resp["items"][0]["total_inr"] == 12.0


@pytest.mark.asyncio
async def test_list_invoices_tenantless_admin_400():
    admin = _cpo(tenant_id=None)
    admin.role = UserRole.ADMIN
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await cpo_list_invoices(admin, db)
    assert exc.value.status_code == 400


# --- GET /api/cpo/invoices.csv ------------------------------------------------------


@pytest.mark.asyncio
async def test_export_invoices_csv():
    db = _db(_scalars_all([_invoice()]))

    resp = await cpo_export_invoices_csv(_cpo(), db)

    assert resp.media_type == "text/csv"
    assert 'attachment; filename="amphive-invoices-' in resp.headers["content-disposition"]
    lines = resp.body.decode().strip().splitlines()
    assert lines[0] == (
        "invoice_number,issued_at,session_id,driver_user_id,"
        "energy_kwh,rate_coins_per_kwh,amount_coins,"
        "taxable_value_inr,gst_rate_pct,gst_amount_inr,total_inr,"
        "seller_legal_name,seller_gstin"
    )
    assert lines[1] == (
        "ACME-2026-27-00001,2026-07-20T13:00:00+00:00,42,7,"
        "1.5,8.0,12.0,10.17,18.0,1.83,12.0,"
        "Acme Charging Pvt Ltd,"
    )


@pytest.mark.asyncio
async def test_export_invoices_csv_accepts_days_filter():
    db = _db(_scalars_all([]))
    resp = await cpo_export_invoices_csv(_cpo(), db, days=30)
    assert resp.media_type == "text/csv"
    assert resp.body.decode().strip().splitlines()[0].startswith("invoice_number,")


@pytest.mark.asyncio
async def test_export_invoices_csv_tenantless_admin_400():
    admin = _cpo(tenant_id=None)
    admin.role = UserRole.ADMIN
    with pytest.raises(HTTPException) as exc:
        await cpo_export_invoices_csv(admin, _db())
    assert exc.value.status_code == 400
