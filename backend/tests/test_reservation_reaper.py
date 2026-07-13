"""
Reservation-start janitor tests (services/session_reaper.py
reap_reservation_starts_once) + the walk-up advance-warning helper
(services/reservations.py next_conflicting_reservation).

Need a real PostgreSQL (CI's postgres:15, exported as TEST_DATABASE_URL) —
the sweep does a real candidate query, a SELECT ... FOR UPDATE SKIP LOCKED
claim, and an overrun lookup. Skipped locally: this repo's dev boxes run no
database by policy (same pattern as test_reservations.py / test_wallet.py).

What's proven here:

1. When a booking's window opens, the sweep nudges the holder ("your
   reservation has started") exactly once — idempotent across ticks via
   started_notified_at, so a second sweep is a no-op.
2. A session still running on the plug under ANOTHER user (a walk-up that
   started legally before the window) is force-stopped through the injected
   finalize with a reason routing it to the "plug reserved" stop notification;
   the holder's nudge then says the plug was freed.
3. The holder's OWN in-progress session is never force-stopped.
4. RESERVATION_FORCE_STOP_WALKUP=false leaves the walk-up running but still
   nudges the holder.
5. Windows that haven't opened yet, or have already ended, are not candidates.
6. next_conflicting_reservation returns the soonest OTHER-user BOOKED window
   starting within the horizon (excludes the caller's own, past, and
   beyond-horizon windows).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

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

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


# --- Seed helpers --------------------------------------------------------------

async def _seed_tenant(factory) -> int:
    from backend.database.models import Tenant

    async with factory() as db:
        tenant = Tenant(name=f"Tenant-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.commit()
        return tenant.id


async def _seed_gateway(factory, tenant_id: int) -> str:
    from backend.database.models import Gateway, GatewayStatus

    gateway_id = f"gw-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        gw = Gateway(
            id=gateway_id, tenant_id=tenant_id, name=gateway_id, vpn_ip=gateway_id,
            status=GatewayStatus.ONLINE, last_seen_at=_now(),
        )
        db.add(gw)
        await db.commit()
        return gw.id


async def _seed_plug(factory, gateway_id: str) -> int:
    from backend.database.models import Plug, PlugStatus

    async with factory() as db:
        plug = Plug(
            gateway_id=gateway_id, name=f"Plug-{uuid.uuid4().hex[:6]}",
            local_ip="10.0.0.5", status=PlugStatus.OCCUPIED,
        )
        db.add(plug)
        await db.commit()
        return plug.id


async def _seed_user(factory) -> int:
    from backend.database.models import User, UserRole

    async with factory() as db:
        user = User(
            email=f"resv-{uuid.uuid4().hex[:12]}@example.com",
            hashed_password="x", full_name="Reservation Tester",
            coin_balance=Decimal("500.00"), role=UserRole.DRIVER,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _seed_reservation(factory, *, plug_id, user_id, tenant_id,
                            start_at, end_at, status=None) -> int:
    from backend.database.models import Reservation, ReservationStatus

    async with factory() as db:
        r = Reservation(
            plug_id=plug_id, user_id=user_id, tenant_id=tenant_id,
            start_at=start_at, end_at=end_at,
            status=status or ReservationStatus.BOOKED,
        )
        db.add(r)
        await db.commit()
        return r.id


async def _seed_active_session(factory, *, plug_id, user_id, tenant_id) -> int:
    """A minimal ACTIVE ChargingSession pinned to the plug — stands in for a
    walk-up already charging (status defaults to ACTIVE)."""
    from backend.database.models import ChargingSession

    async with factory() as db:
        s = ChargingSession(tenant_id=tenant_id, user_id=user_id, plug_id=plug_id)
        db.add(s)
        await db.commit()
        return s.id


async def _get_reservation(factory, reservation_id: int):
    from sqlalchemy import select

    from backend.database.models import Reservation

    async with factory() as db:
        return (await db.execute(
            select(Reservation).where(Reservation.id == reservation_id)
        )).scalar_one()


async def _run_reservation_sweep(factory, monkeypatch, *, finalize=None):
    """Run reap_reservation_starts_once with notify stubbed (its own
    async_session_factory points at the app DB, not this test DB) and a
    stand-in finalize (the real one drives MQTT/telemetry — the sweep only
    needs its non-None dict to know the plug was freed)."""
    import backend.services.notifications as notif_module
    from backend.services.session_reaper import SessionReaperService

    notify_mock = AsyncMock()
    monkeypatch.setattr(notif_module, "notify", notify_mock)

    fake_finalize = finalize or AsyncMock(
        return_value={"energy_kwh": 0.12, "coins_spent": 0.6}
    )
    svc = SessionReaperService(factory, fake_finalize)
    activated = await svc.reap_reservation_starts_once()
    return activated, notify_mock, fake_finalize


async def _fixture_plug(factory):
    tenant_id = await _seed_tenant(factory)
    gateway_id = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gateway_id)
    return tenant_id, plug_id


# --- Sweep behavior ------------------------------------------------------------

@pytest.mark.asyncio
async def test_window_open_nudges_holder_once(factory, monkeypatch):
    tenant_id, plug_id = await _fixture_plug(factory)
    holder = await _seed_user(factory)
    now = _now()
    res_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=5), end_at=now + timedelta(minutes=55),
    )

    activated, notify_mock, finalize = await _run_reservation_sweep(factory, monkeypatch)

    assert activated == 1
    finalize.assert_not_awaited()  # no overrun to stop
    assert notify_mock.await_count == 1
    args = notify_mock.await_args.args
    assert args[0] == holder
    assert args[1] == "reservation_started"
    assert "freed for you" not in args[3]  # nothing was cleared

    reservation = await _get_reservation(factory, res_id)
    assert reservation.started_notified_at is not None

    # Second sweep: already stamped -> not a candidate -> no-op, no re-nudge.
    activated2, notify_mock2, _ = await _run_reservation_sweep(factory, monkeypatch)
    assert activated2 == 0
    notify_mock2.assert_not_awaited()


@pytest.mark.asyncio
async def test_walkup_overrun_is_force_stopped(factory, monkeypatch):
    tenant_id, plug_id = await _fixture_plug(factory)
    holder = await _seed_user(factory)
    walkup = await _seed_user(factory)
    now = _now()
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=2), end_at=now + timedelta(hours=1),
    )
    walkup_session = await _seed_active_session(
        factory, plug_id=plug_id, user_id=walkup, tenant_id=tenant_id
    )

    activated, notify_mock, finalize = await _run_reservation_sweep(factory, monkeypatch)

    assert activated == 1
    # The overrunning non-holder session was finalized with a "reserved" reason.
    finalize.assert_awaited_once()
    assert finalize.await_args.args[1] == walkup_session
    assert "reserved" in finalize.await_args.kwargs["reason"]
    # Holder nudged, and told the plug was cleared for them.
    assert notify_mock.await_count == 1
    holder_args = notify_mock.await_args.args
    assert holder_args[0] == holder
    assert holder_args[1] == "reservation_started"
    assert "freed for you" in holder_args[3]


@pytest.mark.asyncio
async def test_holders_own_session_is_not_stopped(factory, monkeypatch):
    """A holder who walked up early and is charging on their own plug must not
    be force-stopped when their window opens (overrun query excludes them)."""
    tenant_id, plug_id = await _fixture_plug(factory)
    holder = await _seed_user(factory)
    now = _now()
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=1), end_at=now + timedelta(hours=1),
    )
    await _seed_active_session(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id
    )

    activated, notify_mock, finalize = await _run_reservation_sweep(factory, monkeypatch)

    assert activated == 1
    finalize.assert_not_awaited()
    assert notify_mock.await_count == 1
    assert "freed for you" not in notify_mock.await_args.args[3]


@pytest.mark.asyncio
async def test_force_stop_can_be_disabled(factory, monkeypatch):
    monkeypatch.setattr(
        "backend.services.session_reaper.FORCE_STOP_WALKUP_ON_RESERVATION", False
    )
    tenant_id, plug_id = await _fixture_plug(factory)
    holder = await _seed_user(factory)
    walkup = await _seed_user(factory)
    now = _now()
    res_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(minutes=2), end_at=now + timedelta(hours=1),
    )
    await _seed_active_session(
        factory, plug_id=plug_id, user_id=walkup, tenant_id=tenant_id
    )

    activated, notify_mock, finalize = await _run_reservation_sweep(factory, monkeypatch)

    assert activated == 1
    finalize.assert_not_awaited()  # force-stop disabled
    # Holder still nudged, but the plug wasn't cleared.
    assert notify_mock.await_count == 1
    assert "freed for you" not in notify_mock.await_args.args[3]
    # Still stamped so it doesn't re-fire next tick.
    assert (await _get_reservation(factory, res_id)).started_notified_at is not None


@pytest.mark.asyncio
async def test_unopened_and_ended_windows_are_skipped(factory, monkeypatch):
    tenant_id, plug_id = await _fixture_plug(factory)
    holder = await _seed_user(factory)
    now = _now()
    # Not yet started.
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now + timedelta(minutes=30), end_at=now + timedelta(minutes=90),
    )
    # Already ended.
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=holder, tenant_id=tenant_id,
        start_at=now - timedelta(hours=2), end_at=now - timedelta(minutes=5),
    )

    activated, notify_mock, finalize = await _run_reservation_sweep(factory, monkeypatch)

    assert activated == 0
    notify_mock.assert_not_awaited()
    finalize.assert_not_awaited()


# --- Advance-warning helper ----------------------------------------------------

@pytest.mark.asyncio
async def test_next_conflicting_reservation_picks_soonest_other_within_horizon(factory):
    from backend.services.reservations import next_conflicting_reservation

    tenant_id, plug_id = await _fixture_plug(factory)
    me = await _seed_user(factory)
    other = await _seed_user(factory)
    now = _now()

    # My own future booking — excluded when I'm the caller, but a real
    # conflict for anyone else.
    my_booking = await _seed_reservation(
        factory, plug_id=plug_id, user_id=me, tenant_id=tenant_id,
        start_at=now + timedelta(minutes=20), end_at=now + timedelta(minutes=80),
    )
    # Another member's PAST booking (before now — excluded).
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=other, tenant_id=tenant_id,
        start_at=now - timedelta(hours=2), end_at=now - timedelta(hours=1),
    )
    # Another member's booking beyond the horizon (excluded).
    await _seed_reservation(
        factory, plug_id=plug_id, user_id=other, tenant_id=tenant_id,
        start_at=now + timedelta(hours=6), end_at=now + timedelta(hours=7),
    )
    # Another member's booking within the horizon.
    other_soon = await _seed_reservation(
        factory, plug_id=plug_id, user_id=other, tenant_id=tenant_id,
        start_at=now + timedelta(minutes=90), end_at=now + timedelta(minutes=150),
    )

    horizon = now + timedelta(hours=3)
    # A walk-up by `me` runs into `other`'s now+90 window (my own is excluded).
    async with factory() as db:
        found = await next_conflicting_reservation(
            db, plug_id, me, horizon=horizon, now=now
        )
    assert found is not None and found.id == other_soon and found.user_id == other

    # A walk-up by `other` instead sees MY now+20 window (the soonest not theirs).
    async with factory() as db:
        found2 = await next_conflicting_reservation(
            db, plug_id, other, horizon=horizon, now=now
        )
    assert found2 is not None and found2.id == my_booking and found2.user_id == me

    # None when the horizon is too near to reach the soonest other-user window
    # (other's next is now+90; a 30-min horizon can't reach it, mine excluded).
    async with factory() as db:
        none_found = await next_conflicting_reservation(
            db, plug_id, me, horizon=now + timedelta(minutes=30), now=now
        )
    assert none_found is None
