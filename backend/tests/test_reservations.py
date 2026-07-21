"""
Plug reservation tests (feat/reservations) — need a real PostgreSQL (CI's
postgres:15 service, exported as TEST_DATABASE_URL). Skipped locally: this
repo's dev boxes run no database by policy (see test_wallet.py, the same
pattern).

What's proven here:

1. Booking (POST /api/reservations, routers/reservations.py): success writes
   a BOOKED row with tenant_id denormalized from plug -> gateway -> tenant
   and fires the confirmation notification; window-shape violations (end
   before start, < 15 min, > RESERVATION_MAX_HOURS, start in the past beyond
   the 2-min grace, start beyond RESERVATION_MAX_ADVANCE_DAYS) 400; the
   per-user cap of BOOKED-with-future-end (MAX_UPCOMING_RESERVATIONS_PER_
   USER) 409s; an intersecting BOOKED window on the plug 409s while
   back-to-back windows sharing an edge are legal ([start, end) half-open);
   a non-member booking a private-group plug 403s while a member books fine;
   a MAINTENANCE plug 409s.
2. The session-start gate (routers/sessions.py, inside the plug-locked
   section): a non-holder starting inside someone's BOOKED window 409s
   ("reserved until"); the holder's start FULFILs the reservation and links
   the created session; lazy no-show expiry (services/reservations.py
   expire_lapsed_reservations) frees the gate once start_at +
   RESERVATION_NO_SHOW_GRACE_MIN lapses — the walk-up start succeeds and the
   reservation flips EXPIRED, with no background sweep involved.
3. Cancel (POST /api/reservations/{id}/cancel): the owner can cancel; a
   cpo/admin of the owning tenant can cancel (driver gets notified); a CPO
   of ANOTHER tenant gets 404 (existence not leaked); terminal states 409.
4. Read models: GET /api/reservations/my splits upcoming vs history;
   GET /api/plugs/{id}/reservations lists the schedule with holder names and
   is_mine; GET /api/plugs/available carries reserved_now /
   reserved_now_by_me / reserved_until / next_reservation from ONE grouped
   query (routers/plugs.py _reservation_fields_for_plugs).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "reservation_status",
]


def _now():
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory."""
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

    # Mirror the app factory's expire_on_commit=False (backend/database/db.py).
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


# --- Seed helpers --------------------------------------------------------------

async def _seed_tenant(factory, name: str = "Tenant") -> int:
    from backend.database.models import Tenant

    async with factory() as db:
        tenant = Tenant(name=f"{name}-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.commit()
        return tenant.id


async def _seed_gateway(factory, tenant_id: int, gateway_id: str = "gw") -> str:
    """ONLINE + freshly seen, so gateway_is_live() clears the session-start gate."""
    from backend.database.models import Gateway, GatewayStatus

    gateway_id = f"{gateway_id}-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        gw = Gateway(
            id=gateway_id, tenant_id=tenant_id, name=gateway_id, vpn_ip=gateway_id,
            status=GatewayStatus.ONLINE, last_seen_at=_now(),
        )
        db.add(gw)
        await db.commit()
        return gw.id


async def _seed_plug(factory, gateway_id: str, name: str = "Plug",
                     group_id=None, status=None) -> int:
    from backend.database.models import Plug, PlugStatus

    async with factory() as db:
        plug = Plug(
            gateway_id=gateway_id, name=name, local_ip="10.0.0.5",
            status=status or PlugStatus.AVAILABLE, group_id=group_id,
        )
        db.add(plug)
        await db.commit()
        return plug.id


async def _seed_user(factory, *, balance: str = "500.00", tenant_id=None,
                     role=None) -> int:
    from backend.database.models import User, UserRole

    async with factory() as db:
        user = User(
            email=f"resv-{uuid.uuid4().hex[:12]}@example.com",
            hashed_password="x", full_name="Reservation Tester",
            tenant_id=tenant_id, coin_balance=Decimal(balance),
            role=role or UserRole.DRIVER,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _seed_private_group(factory, tenant_id: int) -> int:
    from backend.database.models import ChargerGroup

    async with factory() as db:
        group = ChargerGroup(
            tenant_id=tenant_id, name=f"Society-{uuid.uuid4().hex[:8]}",
            is_public=False, access_code=uuid.uuid4().hex[:8].upper(),
        )
        db.add(group)
        await db.commit()
        return group.id


async def _join_group(factory, user_id: int, group_id: int) -> None:
    from backend.database.models import GroupMembership

    async with factory() as db:
        db.add(GroupMembership(user_id=user_id, group_id=group_id))
        await db.commit()


async def _seed_reservation(factory, *, plug_id, user_id, tenant_id,
                            start_at, end_at, status=None, session_id=None) -> int:
    from backend.database.models import Reservation, ReservationStatus

    async with factory() as db:
        r = Reservation(
            plug_id=plug_id, user_id=user_id, tenant_id=tenant_id,
            start_at=start_at, end_at=end_at,
            status=status or ReservationStatus.BOOKED, session_id=session_id,
        )
        db.add(r)
        await db.commit()
        return r.id


async def _get_reservation(factory, reservation_id: int):
    from sqlalchemy import select

    from backend.database.models import Reservation

    async with factory() as db:
        return (await db.execute(
            select(Reservation).where(Reservation.id == reservation_id)
        )).scalar_one()


def _fake_user(user_id: int, *, role=None, tenant_id=None):
    """A pre-authorized User stand-in for calling router functions directly
    (matches the test_auth_holds.py convention). Booking re-selects the real
    row under a lock, so `.id` must be real; cancel additionally reads
    `.role` and `.tenant_id`."""
    from backend.database.models import UserRole

    return SimpleNamespace(
        id=user_id, role=role or UserRole.DRIVER, tenant_id=tenant_id,
        email="fake@example.com",
    )


async def _book(factory, monkeypatch, *, plug_id, user_id, start_at, end_at):
    """Call POST /api/reservations' handler directly against a real DB
    session, with the notification fan-out stubbed (its own
    async_session_factory points at the app DB, not this test DB)."""
    from unittest.mock import AsyncMock

    import backend.routers.reservations as resv_module
    from backend.routers.reservations import create_reservation
    from backend.schemas import ReservationCreateRequest

    monkeypatch.setattr(resv_module, "notify", AsyncMock())

    req = ReservationCreateRequest(plug_id=plug_id, start_at=start_at, end_at=end_at)
    async with factory() as db:
        return await create_reservation(req, _fake_user(user_id), db)


async def _cancel(factory, monkeypatch, *, reservation_id, user):
    from unittest.mock import AsyncMock

    import backend.routers.reservations as resv_module
    from backend.routers.reservations import cancel_reservation

    notify_mock = AsyncMock()
    monkeypatch.setattr(resv_module, "notify", notify_mock)

    async with factory() as db:
        result = await cancel_reservation(reservation_id, user, db)
    return result, notify_mock


async def _start_session(factory, monkeypatch, *, plug_id: int, user_id: int):
    """Call POST /api/sessions/start's handler directly, gateway/telemetry
    side-effects stubbed (no MQTT broker in this process) — same rig as
    test_auth_holds.py."""
    from unittest.mock import AsyncMock, MagicMock

    import backend.routers.sessions as sessions_module
    from backend import state as state_module
    from backend.routers.sessions import start_charging_session
    from backend.schemas import SessionStartRequest

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(sessions_module, "set_plug_telemetry_interval", AsyncMock())

    req = SessionStartRequest(plug_id=plug_id)
    async with factory() as db:
        return await start_charging_session(req, _fake_user(user_id), db)


# --- 1. Booking -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_booking_success_denormalizes_tenant_and_notifies(factory, monkeypatch):
    from unittest.mock import AsyncMock

    import backend.routers.reservations as resv_module
    from backend.database.models import ReservationStatus
    from backend.routers.reservations import create_reservation
    from backend.schemas import ReservationCreateRequest

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw, "Society Plug")
    uid = await _seed_user(factory, tenant_id=tenant_id)

    notify_mock = AsyncMock()
    monkeypatch.setattr(resv_module, "notify", notify_mock)

    start = _now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    req = ReservationCreateRequest(plug_id=plug_id, start_at=start, end_at=end)
    async with factory() as db:
        resp = await create_reservation(req, _fake_user(uid), db)

    assert resp.status == "booked"
    assert resp.plug_id == plug_id
    assert resp.tenant_id == tenant_id  # denormalized plug -> gateway -> tenant
    assert resp.plug_name == "Society Plug"
    assert resp.is_mine is True

    row = await _get_reservation(factory, resp.id)
    assert row.status == ReservationStatus.BOOKED
    assert row.tenant_id == tenant_id
    assert row.session_id is None

    assert notify_mock.await_count == 1
    args, kwargs = notify_mock.await_args
    assert args[0] == uid
    assert args[1] == "reservation_booked"


@pytest.mark.asyncio
@pytest.mark.parametrize("start_offset,duration,expected_fragment", [
    (timedelta(hours=1), timedelta(minutes=-30), "after start_at"),       # end before start
    (timedelta(hours=1), timedelta(minutes=10), "at least 15 minutes"),   # too short
    (timedelta(hours=1), timedelta(hours=5), "at most 4 hours"),          # > RESERVATION_MAX_HOURS
    (timedelta(minutes=-10), timedelta(hours=1), "in the past"),          # past the 2-min grace
    (timedelta(days=8), timedelta(hours=1), "days ahead"),                # > RESERVATION_MAX_ADVANCE_DAYS
])
async def test_booking_window_validation_400(factory, monkeypatch, start_offset,
                                             duration, expected_fragment):
    from fastapi import HTTPException

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, tenant_id=tenant_id)

    start = _now() + start_offset
    with pytest.raises(HTTPException) as exc_info:
        await _book(factory, monkeypatch, plug_id=plug_id, user_id=uid,
                    start_at=start, end_at=start + duration)

    assert exc_info.value.status_code == 400
    assert expected_fragment in exc_info.value.detail


@pytest.mark.asyncio
async def test_booking_start_within_past_grace_is_accepted(factory, monkeypatch):
    """'Book starting now' must survive submit latency: a start 1 minute ago
    is inside the 2-minute grace and books fine."""
    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, tenant_id=tenant_id)

    start = _now() - timedelta(minutes=1)
    resp = await _book(factory, monkeypatch, plug_id=plug_id, user_id=uid,
                       start_at=start, end_at=start + timedelta(hours=1))
    assert resp.status == "booked"


@pytest.mark.asyncio
async def test_booking_per_user_cap_409(factory, monkeypatch):
    from fastapi import HTTPException

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_a = await _seed_plug(factory, gw, "A")
    plug_b = await _seed_plug(factory, gw, "B")
    plug_c = await _seed_plug(factory, gw, "C")
    uid = await _seed_user(factory, tenant_id=tenant_id)

    base = _now() + timedelta(hours=1)
    await _book(factory, monkeypatch, plug_id=plug_a, user_id=uid,
                start_at=base, end_at=base + timedelta(hours=1))
    await _book(factory, monkeypatch, plug_id=plug_b, user_id=uid,
                start_at=base, end_at=base + timedelta(hours=1))

    with pytest.raises(HTTPException) as exc_info:
        await _book(factory, monkeypatch, plug_id=plug_c, user_id=uid,
                    start_at=base, end_at=base + timedelta(hours=1))

    assert exc_info.value.status_code == 409
    assert "limit 2" in exc_info.value.detail


@pytest.mark.asyncio
async def test_booking_cap_ignores_cancelled_and_past(factory, monkeypatch):
    """Only BOOKED-with-future-end rows count toward the cap: a cancelled
    reservation and one already fully in the past don't block a new booking."""
    from backend.database.models import ReservationStatus

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    other_plug = await _seed_plug(factory, gw, "Other")
    uid = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    await _seed_reservation(
        factory, plug_id=other_plug, user_id=uid, tenant_id=tenant_id,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
        status=ReservationStatus.CANCELLED,
    )
    await _seed_reservation(
        factory, plug_id=other_plug, user_id=uid, tenant_id=tenant_id,
        start_at=now - timedelta(hours=3), end_at=now - timedelta(hours=2),
        status=ReservationStatus.FULFILLED,
    )
    # One live upcoming booking — still under the cap of 2.
    await _seed_reservation(
        factory, plug_id=other_plug, user_id=uid, tenant_id=tenant_id,
        start_at=now + timedelta(hours=3), end_at=now + timedelta(hours=4),
    )

    start = now + timedelta(hours=5)
    resp = await _book(factory, monkeypatch, plug_id=plug_id, user_id=uid,
                       start_at=start, end_at=start + timedelta(hours=1))
    assert resp.status == "booked"


@pytest.mark.asyncio
async def test_booking_overlap_409_and_adjacent_ok(factory, monkeypatch):
    from fastapi import HTTPException

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    holder = await _seed_user(factory, tenant_id=tenant_id)
    other = await _seed_user(factory, tenant_id=tenant_id)

    base = _now() + timedelta(hours=2)
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=base, end_at=base + timedelta(hours=2),
    )

    # Every intersecting shape 409s: straddling the start, contained, and
    # straddling the end.
    for start, end in [
        (base - timedelta(minutes=30), base + timedelta(minutes=30)),
        (base + timedelta(minutes=30), base + timedelta(minutes=90)),
        (base + timedelta(minutes=90), base + timedelta(hours=3)),
    ]:
        with pytest.raises(HTTPException) as exc_info:
            await _book(factory, monkeypatch, plug_id=plug_id, user_id=other,
                        start_at=start, end_at=end)
        assert exc_info.value.status_code == 409
        assert "overlaps" in exc_info.value.detail

    # [start, end) half-open: back-to-back windows sharing the edge are legal.
    resp = await _book(factory, monkeypatch, plug_id=plug_id, user_id=other,
                       start_at=base + timedelta(hours=2),
                       end_at=base + timedelta(hours=3))
    assert resp.status == "booked"


@pytest.mark.asyncio
async def test_booking_private_group_denied_for_non_member(factory, monkeypatch):
    from fastapi import HTTPException

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    group_id = await _seed_private_group(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw, group_id=group_id)
    member = await _seed_user(factory, tenant_id=tenant_id)
    outsider = await _seed_user(factory)
    await _join_group(factory, member, group_id)

    start = _now() + timedelta(hours=1)

    with pytest.raises(HTTPException) as exc_info:
        await _book(factory, monkeypatch, plug_id=plug_id, user_id=outsider,
                    start_at=start, end_at=start + timedelta(hours=1))
    assert exc_info.value.status_code == 403

    resp = await _book(factory, monkeypatch, plug_id=plug_id, user_id=member,
                       start_at=start, end_at=start + timedelta(hours=1))
    assert resp.status == "booked"


@pytest.mark.asyncio
async def test_booking_maintenance_plug_409(factory, monkeypatch):
    from fastapi import HTTPException

    from backend.database.models import PlugStatus

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw, status=PlugStatus.MAINTENANCE)
    uid = await _seed_user(factory, tenant_id=tenant_id)

    start = _now() + timedelta(hours=1)
    with pytest.raises(HTTPException) as exc_info:
        await _book(factory, monkeypatch, plug_id=plug_id, user_id=uid,
                    start_at=start, end_at=start + timedelta(hours=1))
    assert exc_info.value.status_code == 409
    assert "maintenance" in exc_info.value.detail.lower()


# --- 2. Session-start gate -------------------------------------------------------

@pytest.mark.asyncio
async def test_start_gate_409_for_non_holder_inside_window(factory, monkeypatch):
    from fastapi import HTTPException

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    holder = await _seed_user(factory, tenant_id=tenant_id)
    intruder = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=5), end_at=now + timedelta(hours=1),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=intruder)

    assert exc_info.value.status_code == 409
    assert "reserved until" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_holder_start_fulfils_reservation_and_links_session(factory, monkeypatch):
    from backend.database.models import ReservationStatus

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    holder = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=5), end_at=now + timedelta(hours=1),
    )

    result = await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=holder)
    assert result["status"] == "started"

    row = await _get_reservation(factory, resv_id)
    assert row.status == ReservationStatus.FULFILLED
    assert row.session_id == result["session_id"]


@pytest.mark.asyncio
async def test_lazy_expiry_frees_the_gate_after_no_show_grace(factory, monkeypatch):
    """A holder who never shows up stops blocking the plug once start_at +
    RESERVATION_NO_SHOW_GRACE_MIN (default 15) lapses — enforced lazily by
    the gate itself, no background sweep — and the row flips EXPIRED."""
    from backend.database.models import ReservationStatus

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    no_show = await _seed_user(factory, tenant_id=tenant_id)
    walk_up = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=no_show, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=20),  # grace (15 min) lapsed
        end_at=now + timedelta(minutes=40),    # window itself still open
    )

    result = await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=walk_up)
    assert result["status"] == "started"

    row = await _get_reservation(factory, resv_id)
    assert row.status == ReservationStatus.EXPIRED
    assert row.session_id is None


@pytest.mark.asyncio
async def test_gate_still_blocks_within_no_show_grace(factory, monkeypatch):
    """Inside the grace the hold is still live: 10 minutes after start_at
    (grace 15) a non-holder is still 409'd."""
    from fastapi import HTTPException

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    holder = await _seed_user(factory, tenant_id=tenant_id)
    intruder = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=10), end_at=now + timedelta(minutes=50),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=intruder)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_expired_hold_no_longer_blocks_a_new_booking(factory, monkeypatch):
    """The lazy-expiry helper also runs before the overlap check: a lapsed
    no-show window doesn't 409 a new booking over the same slot."""
    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    no_show = await _seed_user(factory, tenant_id=tenant_id)
    booker = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=no_show, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=20), end_at=now + timedelta(hours=1),
    )

    resp = await _book(factory, monkeypatch, plug_id=plug_id, user_id=booker,
                       start_at=now, end_at=now + timedelta(hours=1))
    assert resp.status == "booked"


# --- 3. Cancel -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_by_owner(factory, monkeypatch):
    from backend.database.models import ReservationStatus

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=uid, tenant_id=tenant_id,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
    )

    result, notify_mock = await _cancel(
        factory, monkeypatch, reservation_id=resv_id, user=_fake_user(uid)
    )
    assert result.status == "cancelled"
    row = await _get_reservation(factory, resv_id)
    assert row.status == ReservationStatus.CANCELLED
    # Self-cancel: no "cancelled by the operator" notification.
    assert notify_mock.await_count == 0


@pytest.mark.asyncio
async def test_cancel_by_tenant_cpo_notifies_the_driver(factory, monkeypatch):
    from backend.database.models import ReservationStatus, UserRole

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    driver = await _seed_user(factory, tenant_id=tenant_id)
    cpo = await _seed_user(factory, tenant_id=tenant_id, role=UserRole.CPO)

    now = _now()
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_id,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
    )

    result, notify_mock = await _cancel(
        factory, monkeypatch, reservation_id=resv_id,
        user=_fake_user(cpo, role=UserRole.CPO, tenant_id=tenant_id),
    )
    assert result.status == "cancelled"
    row = await _get_reservation(factory, resv_id)
    assert row.status == ReservationStatus.CANCELLED

    assert notify_mock.await_count == 1
    args, _kwargs = notify_mock.await_args
    assert args[0] == driver  # the DRIVER is told, not the operator
    assert args[1] == "reservation_cancelled"


@pytest.mark.asyncio
async def test_cancel_cross_tenant_cpo_denied_404(factory, monkeypatch):
    from fastapi import HTTPException

    from backend.database.models import ReservationStatus, UserRole

    tenant_a = await _seed_tenant(factory, "TenantA")
    tenant_b = await _seed_tenant(factory, "TenantB")
    gw = await _seed_gateway(factory, tenant_a)
    plug_id = await _seed_plug(factory, gw)
    driver = await _seed_user(factory, tenant_id=tenant_a)
    foreign_cpo = await _seed_user(factory, tenant_id=tenant_b, role=UserRole.CPO)

    now = _now()
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_a,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _cancel(
            factory, monkeypatch, reservation_id=resv_id,
            user=_fake_user(foreign_cpo, role=UserRole.CPO, tenant_id=tenant_b),
        )
    assert exc_info.value.status_code == 404  # existence not leaked

    row = await _get_reservation(factory, resv_id)
    assert row.status == ReservationStatus.BOOKED  # untouched


@pytest.mark.asyncio
async def test_cancel_terminal_states_409(factory, monkeypatch):
    from fastapi import HTTPException

    from backend.database.models import ReservationStatus

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=uid, tenant_id=tenant_id,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
        status=ReservationStatus.CANCELLED,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _cancel(factory, monkeypatch, reservation_id=resv_id,
                      user=_fake_user(uid))
    assert exc_info.value.status_code == 409


# --- 4. Read models ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_my_reservations_splits_upcoming_and_history(factory):
    from backend.database.models import ReservationStatus
    from backend.routers.reservations import my_reservations

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw, "Society Plug")
    uid = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    upcoming_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=uid, tenant_id=tenant_id,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
    )
    cancelled_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=uid, tenant_id=tenant_id,
        start_at=now + timedelta(hours=3), end_at=now + timedelta(hours=4),
        status=ReservationStatus.CANCELLED,
    )
    # A stale BOOKED row past its grace: the endpoint lazily expires it into
    # history instead of showing it as upcoming.
    stale_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=uid, tenant_id=tenant_id,
        start_at=now - timedelta(hours=1), end_at=now - timedelta(minutes=10),
    )

    async with factory() as db:
        result = await my_reservations(_fake_user(uid), db)

    assert [r.id for r in result.upcoming] == [upcoming_id]
    assert result.upcoming[0].plug_name == "Society Plug"
    history_ids = {r.id for r in result.history}
    assert history_ids == {cancelled_id, stale_id}

    stale = await _get_reservation(factory, stale_id)
    assert stale.status == ReservationStatus.EXPIRED


@pytest.mark.asyncio
async def test_plug_schedule_lists_booked_windows_with_holder(factory):
    from backend.routers.reservations import plug_reservations

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    holder = await _seed_user(factory, tenant_id=tenant_id)
    viewer = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
    )

    async with factory() as db:
        schedule = await plug_reservations(plug_id, _fake_user(viewer), db)

    assert len(schedule) == 1
    assert schedule[0].user_name == "Reservation Tester"
    assert schedule[0].is_mine is False
    assert schedule[0].status == "booked"


@pytest.mark.asyncio
async def test_plug_schedule_access_denied_for_non_member(factory):
    from fastapi import HTTPException

    from backend.routers.reservations import plug_reservations

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    group_id = await _seed_private_group(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw, group_id=group_id)
    outsider = await _seed_user(factory)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc_info:
            await plug_reservations(plug_id, _fake_user(outsider), db)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_available_plugs_carry_reservation_fields(factory):
    from backend.routers.plugs import get_available_plugs

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    reserved_plug = await _seed_plug(factory, gw, "Reserved")
    free_plug = await _seed_plug(factory, gw, "Free")
    holder = await _seed_user(factory, tenant_id=tenant_id)
    viewer = await _seed_user(factory, tenant_id=tenant_id)

    now = _now()
    covering_end = now + timedelta(minutes=45)
    await _seed_reservation(
        factory, plug_id=reserved_plug, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=5), end_at=covering_end,
    )
    next_start = now + timedelta(hours=2)
    await _seed_reservation(
        factory, plug_id=reserved_plug, user_id=holder, tenant_id=tenant_id,
        start_at=next_start, end_at=next_start + timedelta(hours=1),
    )

    async with factory() as db:
        plugs = await get_available_plugs(_fake_user(viewer), db)

    by_name = {p.name: p for p in plugs}
    reserved = by_name["Reserved"]
    assert reserved.reserved_now is True
    assert reserved.reserved_now_by_me is False
    assert reserved.reserved_until == covering_end.isoformat()
    assert reserved.next_reservation is not None
    assert reserved.next_reservation.start_at == next_start.isoformat()

    free = by_name["Free"]
    assert free.reserved_now is False
    assert free.reserved_until is None
    assert free.next_reservation is None

    # The holder themselves sees reserved_now_by_me.
    async with factory() as db:
        plugs_as_holder = await get_available_plugs(_fake_user(holder), db)
    assert {p.name: p for p in plugs_as_holder}["Reserved"].reserved_now_by_me is True


@pytest.mark.asyncio
async def test_cpo_reservations_tenant_scoped(factory):
    from backend.database.models import UserRole
    from backend.routers.cpo import cpo_list_reservations

    tenant_a = await _seed_tenant(factory, "TenantA")
    tenant_b = await _seed_tenant(factory, "TenantB")
    gw_a = await _seed_gateway(factory, tenant_a)
    gw_b = await _seed_gateway(factory, tenant_b)
    plug_a = await _seed_plug(factory, gw_a, "A-Plug")
    plug_b = await _seed_plug(factory, gw_b, "B-Plug")
    driver_a = await _seed_user(factory, tenant_id=tenant_a)
    driver_b = await _seed_user(factory, tenant_id=tenant_b)
    cpo_a = await _seed_user(factory, tenant_id=tenant_a, role=UserRole.CPO)

    now = _now()
    a_id = await _seed_reservation(
        factory, plug_id=plug_a, user_id=driver_a, tenant_id=tenant_a,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
    )
    await _seed_reservation(
        factory, plug_id=plug_b, user_id=driver_b, tenant_id=tenant_b,
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
    )

    async with factory() as db:
        resp = await cpo_list_reservations(
            user=_fake_user(cpo_a, role=UserRole.CPO, tenant_id=tenant_a), db=db
        )

    # [redesign/ui-v3 contract §4] paginated {total, items} shape
    rows = resp["items"]
    assert resp["total"] == 1
    assert [r["id"] for r in rows] == [a_id]  # only tenant A's rows
    assert rows[0]["plug_name"] == "A-Plug"
    assert rows[0]["user_name"] == "Reservation Tester"
