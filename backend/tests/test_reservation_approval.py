"""
[C8] Reservation approval mode tests.

Tier 1 (DB-free, runs locally): the plug-update schema accepts only the two
modes, and the new approve/decline routes are POSTs the cpo_viewer global
write guard walks (so a read-only viewer gets 403 on them).

Tier 2 (real Postgres, CI only; skipped without TEST_DATABASE_URL):
- a booking on an approval-mode plug is PENDING, the driver and the tenant's
  CPO are told, and a CPO approve turns it into an ordinary BOOKED hold;
- decline frees the slot and tells the driver;
- a request still PENDING at start_at lazily flips to EXPIRED;
- PENDING blocks an overlapping booking and counts toward the per-user cap;
- a request on an approval-mode plug cannot start now;
- approve racing the driver's cancel settles exactly once (both lock the
  plug row, then the reservation row);
- another tenant's CPO gets 404.
"""
import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from backend.schemas import CpoPlugUpdateRequest

# --- Tier 1: DB-free ------------------------------------------------------------


def test_plug_update_accepts_only_known_reservation_modes():
    assert CpoPlugUpdateRequest(reservation_mode="approval").reservation_mode == "approval"
    assert CpoPlugUpdateRequest(reservation_mode="instant").reservation_mode == "instant"
    assert CpoPlugUpdateRequest().reservation_mode is None
    with pytest.raises(ValidationError):
        CpoPlugUpdateRequest(reservation_mode="auto")


def test_reservation_mode_of_defaults_to_instant():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from backend.services.reservations import reservation_mode_of

    assert reservation_mode_of(SimpleNamespace(reservation_mode="approval")) == "approval"
    assert reservation_mode_of(SimpleNamespace(reservation_mode="instant")) == "instant"
    assert reservation_mode_of(SimpleNamespace()) == "instant"
    assert reservation_mode_of(MagicMock()) == "instant"


def test_approve_and_decline_are_guarded_write_routes():
    from backend.tests.test_cpo_viewer_role import _write_routes

    walked = {(m, p) for m, p, _ in _write_routes()}
    assert {
        ("POST", "/api/cpo/reservations/{reservation_id}/approve"),
        ("POST", "/api/cpo/reservations/{reservation_id}/decline"),
    } <= walked


def _approve_db(plug, reservation):
    """A mocked AsyncSession for _answer_request: the tenant-scoped plug_id
    lookup, the plug row lock, the reservation row lock, the tenant zone."""
    from unittest.mock import MagicMock

    def _res(**kw):
        r = MagicMock()
        for k, v in kw.items():
            getattr(r, k).return_value = v
        return r

    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _res(scalar_one_or_none=plug.id),
        _res(scalar_one=plug),
        _res(scalar_one=reservation),
        _res(scalar_one_or_none="Asia/Kolkata"),
    ])
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("open_now", [False, True])
async def test_approval_respects_the_current_opening_hours(monkeypatch, open_now):
    """[C8 + C4] Approving books the slot, so hours narrowed after the request
    refuse it (409, still PENDING, nothing sent); an open slot is approved.
    Declining never checks hours."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from backend.database.models import PlugStatus, ReservationStatus
    from backend.routers.cpo import _reservations as mod

    monkeypatch.setattr(mod, "expire_lapsed_reservations", AsyncMock())
    monkeypatch.setattr(mod, "window_is_open", MagicMock(return_value=open_now))
    monkeypatch.setattr(mod, "user_can_access_plug", AsyncMock(return_value=True))
    sent = AsyncMock()
    monkeypatch.setattr(mod, "notify", sent)
    start = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
    plug = SimpleNamespace(id=5, name="Bay 1", status=PlugStatus.AVAILABLE,
                           opening_hours=[{"days": [0], "open": "08:00", "close": "18:00"}])
    reservation = SimpleNamespace(
        id=9, user_id=3, status=ReservationStatus.PENDING,
        start_at=start, end_at=start + timedelta(hours=1),
    )
    user = SimpleNamespace(tenant_id=1)
    db = _approve_db(plug, reservation)

    if not open_now:
        with pytest.raises(HTTPException) as exc:
            await mod._answer_request(db, user, 9, approve=True)
        assert exc.value.status_code == 409
        assert reservation.status == ReservationStatus.PENDING
        db.commit.assert_not_awaited()
        sent.assert_not_awaited()
    else:
        out = await mod._answer_request(db, user, 9, approve=True)
        assert out["status"] == "booked"
        assert sent.await_args.args[1] == "reservation_approved"

    # Declining a request outside the hours is always possible.
    reservation.status = ReservationStatus.PENDING
    db = _approve_db(plug, reservation)
    out = await mod._answer_request(db, user, 9, approve=False)
    assert out["status"] == "declined"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["maintenance", "lost_access"])
async def test_approval_refused_when_the_plug_or_requester_no_longer_qualifies(monkeypatch, case):
    """[C8] Approval books the slot, so it applies a direct booking's checks:
    a plug in MAINTENANCE, or a requester removed from (or who left) the
    private group after asking, refuses the approval (409, still PENDING,
    nothing sent). Declining still works."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from fastapi import HTTPException

    from backend.database.models import PlugStatus, ReservationStatus
    from backend.routers.cpo import _reservations as mod

    monkeypatch.setattr(mod, "expire_lapsed_reservations", AsyncMock())
    monkeypatch.setattr(mod, "user_can_access_plug", AsyncMock(return_value=case != "lost_access"))
    sent = AsyncMock()
    monkeypatch.setattr(mod, "notify", sent)
    status = PlugStatus.MAINTENANCE if case == "maintenance" else PlugStatus.AVAILABLE
    plug = SimpleNamespace(id=5, name="Bay 1", status=status, opening_hours=None, group_id=11)
    start = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
    reservation = SimpleNamespace(
        id=9, user_id=3, status=ReservationStatus.PENDING,
        start_at=start, end_at=start + timedelta(hours=1),
    )
    user = SimpleNamespace(tenant_id=1)

    db = _approve_db(plug, reservation)
    with pytest.raises(HTTPException) as exc:
        await mod._answer_request(db, user, 9, approve=True)
    assert exc.value.status_code == 409
    assert reservation.status == ReservationStatus.PENDING
    db.commit.assert_not_awaited()
    sent.assert_not_awaited()

    db = _approve_db(plug, reservation)
    out = await mod._answer_request(db, user, 9, approve=False)
    assert out["status"] == "declined"


# --- Tier 2: DB-gated -----------------------------------------------------------

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

from backend.tests.test_reservations import (  # noqa: E402
    ENUM_TYPES,
    _book,
    _fake_user,
    _get_reservation,
    _now,
    _seed_gateway,
    _seed_plug,
    _seed_reservation,
    _seed_tenant,
    _seed_user,
)


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test (test_reservations.py's fixture)."""
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


async def _set_mode(factory, plug_id, mode="approval"):
    from sqlalchemy import update

    from backend.database.models import Plug

    async with factory() as db:
        await db.execute(update(Plug).where(Plug.id == plug_id).values(reservation_mode=mode))
        await db.commit()


async def _site(factory, *, mode="approval"):
    """Tenant + live gateway + approval-mode plug + driver + tenant CPO."""
    from backend.database.models import UserRole

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    await _set_mode(factory, plug_id, mode)
    driver = await _seed_user(factory, tenant_id=tenant_id)
    cpo = await _seed_user(factory, tenant_id=tenant_id, role=UserRole.CPO)
    return tenant_id, plug_id, driver, cpo


async def _answer(factory, monkeypatch, *, reservation_id, cpo, tenant_id, approve):
    import backend.routers.cpo._reservations as cpo_resv
    from backend.database.models import UserRole

    notify_mock = AsyncMock()
    monkeypatch.setattr(cpo_resv, "notify", notify_mock)
    user = _fake_user(cpo, role=UserRole.CPO, tenant_id=tenant_id)
    async with factory() as db:
        result = await cpo_resv._answer_request(db, user, reservation_id, approve=approve)
    return result, notify_mock


@requires_db
@pytest.mark.asyncio
async def test_approval_mode_booking_is_pending_then_approved(factory, monkeypatch):
    import backend.routers.reservations as resv_module
    from backend.database.models import ReservationStatus

    tenant_id, plug_id, driver, cpo = await _site(factory)
    now = _now()
    notify_mock = AsyncMock()
    monkeypatch.setattr(resv_module, "notify", notify_mock)
    from backend.routers.reservations import create_reservation
    from backend.schemas import ReservationCreateRequest

    req = ReservationCreateRequest(
        plug_id=plug_id, start_at=now + timedelta(hours=2), end_at=now + timedelta(hours=3)
    )
    async with factory() as db:
        resp = await create_reservation(req, _fake_user(driver), db)
    assert resp.status == "pending"
    types = [(c.args[0], c.args[1]) for c in notify_mock.await_args_list]
    assert (driver, "reservation_requested") in types
    assert (cpo, "reservation_needs_approval") in types

    result, approve_notify = await _answer(
        factory, monkeypatch, reservation_id=resp.id, cpo=cpo, tenant_id=tenant_id, approve=True
    )
    assert result["status"] == "booked"
    assert (await _get_reservation(factory, resp.id)).status == ReservationStatus.BOOKED
    assert approve_notify.await_args.args[:2] == (driver, "reservation_approved")

    # Answering twice is a conflict, not a second state change.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _answer(
            factory, monkeypatch, reservation_id=resp.id, cpo=cpo, tenant_id=tenant_id, approve=False
        )
    assert exc.value.status_code == 409


@requires_db
@pytest.mark.asyncio
async def test_decline_frees_the_slot_and_tells_the_driver(factory, monkeypatch):
    from backend.database.models import ReservationStatus

    tenant_id, plug_id, driver, cpo = await _site(factory)
    other = await _seed_user(factory, tenant_id=tenant_id)
    now = _now()
    start, end = now + timedelta(hours=2), now + timedelta(hours=3)
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_id,
        start_at=start, end_at=end, status=ReservationStatus.PENDING,
    )

    result, notify_mock = await _answer(
        factory, monkeypatch, reservation_id=resv_id, cpo=cpo, tenant_id=tenant_id, approve=False
    )
    assert result["status"] == "declined"
    assert (await _get_reservation(factory, resv_id)).status == ReservationStatus.DECLINED
    assert notify_mock.await_args.args[:2] == (driver, "reservation_declined")

    # The declined window no longer blocks anyone.
    resp = await _book(factory, monkeypatch, plug_id=plug_id, user_id=other,
                       start_at=start, end_at=end)
    assert resp.status == "pending"


@requires_db
@pytest.mark.asyncio
async def test_pending_request_expires_at_start(factory):
    from backend.database.models import ReservationStatus
    from backend.services.reservations import expire_lapsed_reservations

    tenant_id, plug_id, driver, _cpo = await _site(factory)
    now = _now()
    lapsed = await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=1), end_at=now + timedelta(hours=1),
        status=ReservationStatus.PENDING,
    )
    future = await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_id,
        start_at=now + timedelta(hours=2), end_at=now + timedelta(hours=3),
        status=ReservationStatus.PENDING,
    )
    async with factory() as db:
        flipped = await expire_lapsed_reservations(db, plug_id=plug_id, now=now)
        await db.commit()
    assert flipped == 1
    assert (await _get_reservation(factory, lapsed)).status == ReservationStatus.EXPIRED
    assert (await _get_reservation(factory, future)).status == ReservationStatus.PENDING


@requires_db
@pytest.mark.asyncio
async def test_pending_blocks_overlap_and_counts_toward_cap(factory, monkeypatch):
    from fastapi import HTTPException

    from backend.database.models import ReservationStatus
    from backend.services import reservations as policy

    tenant_id, plug_id, driver, _cpo = await _site(factory)
    other = await _seed_user(factory, tenant_id=tenant_id)
    now = _now()
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_id,
        start_at=now + timedelta(hours=2), end_at=now + timedelta(hours=3),
        status=ReservationStatus.PENDING,
    )
    with pytest.raises(HTTPException) as exc:
        await _book(factory, monkeypatch, plug_id=plug_id, user_id=other,
                    start_at=now + timedelta(hours=2, minutes=30),
                    end_at=now + timedelta(hours=4))
    assert exc.value.status_code == 409

    monkeypatch.setattr(policy, "MAX_UPCOMING_RESERVATIONS_PER_USER", 1)
    with pytest.raises(HTTPException) as exc:
        await _book(factory, monkeypatch, plug_id=plug_id, user_id=driver,
                    start_at=now + timedelta(hours=5), end_at=now + timedelta(hours=6))
    assert exc.value.status_code == 409


@requires_db
@pytest.mark.asyncio
async def test_approval_mode_refuses_a_start_now_request(factory, monkeypatch):
    from fastapi import HTTPException

    _tenant_id, plug_id, driver, _cpo = await _site(factory)
    now = _now()
    with pytest.raises(HTTPException) as exc:
        await _book(factory, monkeypatch, plug_id=plug_id, user_id=driver,
                    start_at=now - timedelta(minutes=1), end_at=now + timedelta(hours=1))
    assert exc.value.status_code == 400


@requires_db
@pytest.mark.asyncio
async def test_approve_racing_driver_cancel_settles_once(factory, monkeypatch):
    """Both paths lock the plug row, then the reservation row. Whichever
    commits first, the driver's cancel wins in the end: cancel-first makes the
    approve a 409; approve-first leaves a BOOKED row the cancel then releases.
    Never an approved row the driver thinks is cancelled."""
    from fastapi import HTTPException

    import backend.routers.cpo._reservations as cpo_resv
    import backend.routers.reservations as resv_module
    from backend.database.models import ReservationStatus, UserRole
    from backend.routers.reservations import cancel_reservation

    tenant_id, plug_id, driver, cpo = await _site(factory)
    now = _now()
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_id,
        start_at=now + timedelta(hours=2), end_at=now + timedelta(hours=3),
        status=ReservationStatus.PENDING,
    )
    monkeypatch.setattr(cpo_resv, "notify", AsyncMock())
    monkeypatch.setattr(resv_module, "notify", AsyncMock())

    async def approve():
        async with factory() as db:
            return await cpo_resv._answer_request(
                db, _fake_user(cpo, role=UserRole.CPO, tenant_id=tenant_id), resv_id, approve=True
            )

    async def cancel():
        async with factory() as db:
            return await cancel_reservation(resv_id, _fake_user(driver), db)

    approved, cancelled = await asyncio.gather(approve(), cancel(), return_exceptions=True)
    if isinstance(approved, HTTPException):
        assert approved.status_code == 409
    else:
        assert not isinstance(approved, BaseException), approved
        assert approved["status"] == "booked"
    assert not isinstance(cancelled, BaseException), cancelled
    assert cancelled.status == "cancelled"
    assert (await _get_reservation(factory, resv_id)).status == ReservationStatus.CANCELLED


@requires_db
@pytest.mark.asyncio
async def test_other_tenant_cpo_cannot_answer(factory, monkeypatch):
    from fastapi import HTTPException

    from backend.database.models import ReservationStatus, UserRole

    tenant_id, plug_id, driver, _cpo = await _site(factory)
    other_tenant = await _seed_tenant(factory, "Other")
    stranger = await _seed_user(factory, tenant_id=other_tenant, role=UserRole.CPO)
    now = _now()
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_id,
        start_at=now + timedelta(hours=2), end_at=now + timedelta(hours=3),
        status=ReservationStatus.PENDING,
    )
    with pytest.raises(HTTPException) as exc:
        await _answer(factory, monkeypatch, reservation_id=resv_id, cpo=stranger,
                      tenant_id=other_tenant, approve=True)
    assert exc.value.status_code == 404
    assert (await _get_reservation(factory, resv_id)).status == ReservationStatus.PENDING
