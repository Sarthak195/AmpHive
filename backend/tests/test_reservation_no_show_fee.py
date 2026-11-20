"""
[C8] Reservation no-show fee tests.

Tier 1 (DB-free, runs locally): the pure fee arithmetic (zero fee, server cap,
never more than the available balance, never negative), the plug accessor,
the "charger may have been unusable" skip rules, the plug-update schema bound,
and that the fee notice is on the non-suppressible push allowlist.

Tier 2 (real Postgres, CI only; skipped without TEST_DATABASE_URL), driving
services/reservations.py reap_reservation_no_shows_once:
- a no-show on a live, powered charger is charged once, even across two
  sweeps (stamp + UNIQUE ledger reservation_id);
- skipped when the gateway is offline or the plug lost power in the window;
- charged at most the available balance, never below zero;
- a zero fee does nothing; a request that lapsed while PENDING is never
  charged; a window older than the sweep lag is never charged;
- booking snapshots the plug's fee onto the reservation;
- a lapsed booking can no longer be cancelled to dodge the fee;
- skipped when the holder scheduled a charge or has a routine in the window,
  when maintenance was entered or cleared in it, after a safety cutoff, or
  when the charger was closed at the start (with negative controls).
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from backend.database.models import GatewayStatus, PlugStatus
from backend.schemas import CpoPlugUpdateRequest
from backend.services import reservations as policy
from backend.services.reservations import (
    no_show_fee_amount,
    no_show_fee_of,
    no_show_skip_reason,
)

# --- Tier 1: DB-free ------------------------------------------------------------


def test_fee_amount_zero_cap_and_balance():
    cap = policy.RESERVATION_NO_SHOW_FEE_MAX_COINS
    assert no_show_fee_amount(Decimal("0"), Decimal("500")) == Decimal("0.00")
    assert no_show_fee_amount(Decimal("20"), Decimal("500")) == Decimal("20.00")
    assert no_show_fee_amount(Decimal("20"), Decimal("7.5")) == Decimal("7.50")
    # Available can be negative (holds above the balance): never charge below 0.
    assert no_show_fee_amount(Decimal("20"), Decimal("-3")) == Decimal("0.00")
    assert no_show_fee_amount(cap + 50, Decimal("100000")) == cap


def test_fee_accessor_caps_and_defaults():
    cap = policy.RESERVATION_NO_SHOW_FEE_MAX_COINS
    assert no_show_fee_of(SimpleNamespace(no_show_fee_coins=Decimal("12.5"))) == Decimal("12.50")
    assert no_show_fee_of(SimpleNamespace(no_show_fee_coins=cap + 1)) == cap
    assert no_show_fee_of(SimpleNamespace(no_show_fee_coins=Decimal("0"))) == Decimal("0.00")
    assert no_show_fee_of(SimpleNamespace()) == Decimal("0.00")
    assert no_show_fee_of(MagicMock()) == Decimal("0.00")


def _usable(now, start_at):
    plug = SimpleNamespace(
        status=PlugStatus.AVAILABLE, last_telemetry_at=now,
        powered_since=start_at - timedelta(hours=1),
    )
    gateway = SimpleNamespace(status=GatewayStatus.ONLINE, last_seen_at=now)
    return plug, gateway


def test_skip_reason_rules():
    now = datetime.now(timezone.utc)
    start_at = now - timedelta(minutes=16)

    plug, gateway = _usable(now, start_at)
    assert no_show_skip_reason(plug, gateway, start_at=start_at, now=now) is None

    for status in (PlugStatus.OFFLINE, PlugStatus.MAINTENANCE):
        plug, gateway = _usable(now, start_at)
        plug.status = status
        assert no_show_skip_reason(plug, gateway, start_at=start_at, now=now)

    plug, gateway = _usable(now, start_at)
    gateway.status = GatewayStatus.OFFLINE
    assert no_show_skip_reason(plug, gateway, start_at=start_at, now=now)

    plug, gateway = _usable(now, start_at)
    gateway.last_seen_at = now - timedelta(hours=1)
    assert no_show_skip_reason(plug, gateway, start_at=start_at, now=now)

    plug, gateway = _usable(now, start_at)
    plug.last_telemetry_at = now - timedelta(hours=1)
    assert no_show_skip_reason(plug, gateway, start_at=start_at, now=now)

    plug, gateway = _usable(now, start_at)
    plug.powered_since = start_at + timedelta(minutes=5)  # power came back mid-window
    assert no_show_skip_reason(plug, gateway, start_at=start_at, now=now)

    plug, gateway = _usable(now, start_at)
    plug.powered_since = None
    assert no_show_skip_reason(plug, gateway, start_at=start_at, now=now)

    assert no_show_skip_reason(None, gateway, start_at=start_at, now=now)
    plug, _ = _usable(now, start_at)
    assert no_show_skip_reason(plug, None, start_at=start_at, now=now)


def test_plug_update_rejects_a_negative_fee():
    assert CpoPlugUpdateRequest(no_show_fee_coins=10).no_show_fee_coins == 10
    with pytest.raises(ValidationError):
        CpoPlugUpdateRequest(no_show_fee_coins=-1)


def test_fee_notice_always_pushes():
    from backend.services.notification_prefs import always_pushed

    assert always_pushed("reservation_no_show_fee", "info")


# --- Tier 2: DB-gated -----------------------------------------------------------

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

from backend.tests.test_reservations import (  # noqa: E402
    ENUM_TYPES,
    _book,
    _cancel,
    _fake_user,
    _get_reservation,
    _join_group,
    _now,
    _seed_gateway,
    _seed_plug,
    _seed_private_group,
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


async def _update(factory, model, row_id, **values):
    from sqlalchemy import update

    async with factory() as db:
        await db.execute(update(model).where(model.id == row_id).values(**values))
        await db.commit()


async def _no_show_site(factory, *, fee="20.00", balance="500.00", powered=True):
    """A live gateway and a powered plug with a fee, a driver, and that
    driver's BOOKED window whose 15-minute grace lapsed one minute ago."""
    from backend.database.models import Plug

    now = _now()
    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    await _update(
        factory, Plug, plug_id, no_show_fee_coins=Decimal(fee), last_telemetry_at=now,
        powered_since=(now - timedelta(hours=2)) if powered else (now - timedelta(minutes=5)),
    )
    driver = await _seed_user(factory, tenant_id=tenant_id, balance=balance)
    start = now - timedelta(minutes=policy.RESERVATION_NO_SHOW_GRACE_MIN + 1)
    resv_id = await _seed_reservation(
        factory, plug_id=plug_id, user_id=driver, tenant_id=tenant_id,
        start_at=start, end_at=start + timedelta(hours=1),
    )
    await _update_resv(factory, resv_id, no_show_fee_coins=Decimal(fee))
    return SimpleNamespace(
        tenant_id=tenant_id, gw=gw, plug_id=plug_id, driver=driver, resv_id=resv_id, now=now
    )


async def _update_resv(factory, resv_id, **values):
    from backend.database.models import Reservation

    await _update(factory, Reservation, resv_id, **values)


async def _ledger(factory, user_id):
    from sqlalchemy import select

    from backend.database.models import LedgerTransaction, TransactionType

    async with factory() as db:
        return list((await db.execute(
            select(LedgerTransaction).where(
                LedgerTransaction.user_id == user_id,
                LedgerTransaction.transaction_type == TransactionType.RESERVATION_NO_SHOW_FEE,
            )
        )).scalars().all())


async def _balance(factory, user_id):
    from sqlalchemy import select

    from backend.database.models import User

    async with factory() as db:
        return (await db.execute(select(User.coin_balance).where(User.id == user_id))).scalar_one()


@pytest.fixture
def notify_mock(monkeypatch):
    import backend.services.notifications as notifications

    mock = AsyncMock()
    monkeypatch.setattr(notifications, "notify", mock)
    return mock


@requires_db
@pytest.mark.asyncio
async def test_fee_charged_once_across_two_sweeps(factory, notify_mock):
    from backend.database.models import ReservationStatus

    site = await _no_show_site(factory)
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 1
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0

    rows = await _ledger(factory, site.driver)
    assert len(rows) == 1
    assert rows[0].amount == Decimal("-20.00")
    assert rows[0].reservation_id == site.resv_id
    assert await _balance(factory, site.driver) == Decimal("480.00")
    row = await _get_reservation(factory, site.resv_id)
    assert row.status == ReservationStatus.EXPIRED
    assert row.no_show_checked_at is not None
    assert notify_mock.await_count == 1
    assert notify_mock.await_args.args[:2] == (site.driver, "reservation_no_show_fee")


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_when_gateway_offline(factory, notify_mock):
    from backend.database.models import Gateway

    site = await _no_show_site(factory)
    await _update(factory, Gateway, site.gw, status=GatewayStatus.OFFLINE)
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []
    assert await _balance(factory, site.driver) == Decimal("500.00")
    # Settled as skipped: a reconnect two ticks later must not turn it into a charge.
    await _update(factory, Gateway, site.gw, status=GatewayStatus.ONLINE)
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert notify_mock.await_count == 0


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_when_plug_lost_power_in_window(factory, notify_mock):
    site = await _no_show_site(factory, powered=False)
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []


@requires_db
@pytest.mark.asyncio
async def test_fee_never_takes_the_balance_negative(factory, notify_mock):
    site = await _no_show_site(factory, fee="20.00", balance="5.00")
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 1
    assert (await _ledger(factory, site.driver))[0].amount == Decimal("-5.00")
    assert await _balance(factory, site.driver) == Decimal("0.00")

    empty = await _no_show_site(factory, fee="20.00", balance="0.00")
    assert await policy.reap_reservation_no_shows_once(factory, now=empty.now) == 0
    assert await _ledger(factory, empty.driver) == []
    assert await _balance(factory, empty.driver) == Decimal("0.00")


@requires_db
@pytest.mark.asyncio
async def test_zero_fee_does_nothing(factory, notify_mock):
    site = await _no_show_site(factory, fee="0")
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []
    assert (await _get_reservation(factory, site.resv_id)).no_show_checked_at is None
    assert notify_mock.await_count == 0


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_when_holder_lost_private_group_access(factory, notify_mock):
    """A holder the operator removed from (or who left) the plug's private
    group is refused 403 by every start path, so the empty window is not a
    no-show. Settled as skipped, never charged."""
    from sqlalchemy import delete

    from backend.database.models import GroupMembership, Plug

    site = await _no_show_site(factory)
    group_id = await _seed_private_group(factory, site.tenant_id)
    await _update(factory, Plug, site.plug_id, group_id=group_id)
    await _join_group(factory, site.driver, group_id)
    async with factory() as db:
        await db.execute(delete(GroupMembership).where(GroupMembership.user_id == site.driver))
        await db.commit()

    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []
    assert await _balance(factory, site.driver) == Decimal("500.00")
    assert (await _get_reservation(factory, site.resv_id)).no_show_checked_at is not None
    assert notify_mock.await_count == 0


@requires_db
@pytest.mark.asyncio
async def test_fee_still_charged_to_a_current_private_group_member(factory, notify_mock):
    from backend.database.models import Plug

    site = await _no_show_site(factory)
    group_id = await _seed_private_group(factory, site.tenant_id)
    await _update(factory, Plug, site.plug_id, group_id=group_id)
    await _join_group(factory, site.driver, group_id)
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 1


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_when_holder_account_disabled(factory, notify_mock):
    from backend.database.models import User

    site = await _no_show_site(factory)
    await _update(factory, User, site.driver, is_disabled=True)
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []


@requires_db
@pytest.mark.asyncio
async def test_lapsed_pending_request_is_never_charged(factory, notify_mock):
    from backend.database.models import ReservationStatus
    from backend.services.reservations import expire_lapsed_reservations

    site = await _no_show_site(factory)
    await _update_resv(factory, site.resv_id, status=ReservationStatus.PENDING)
    async with factory() as db:
        await expire_lapsed_reservations(db, plug_id=site.plug_id, now=site.now)
        await db.commit()
    row = await _get_reservation(factory, site.resv_id)
    assert row.status == ReservationStatus.EXPIRED
    assert row.no_show_checked_at is not None
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []


@requires_db
@pytest.mark.asyncio
async def test_window_older_than_the_sweep_lag_is_never_charged(factory, notify_mock):
    site = await _no_show_site(factory)
    later = site.now + timedelta(minutes=policy.RESERVATION_NO_SHOW_SWEEP_MAX_LAG_MIN + 1)
    assert await policy.reap_reservation_no_shows_once(factory, now=later) == 0
    assert await _ledger(factory, site.driver) == []


@requires_db
@pytest.mark.asyncio
async def test_booking_snapshots_the_plug_fee(factory, monkeypatch):
    from backend.database.models import Plug

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    await _update(factory, Plug, plug_id, no_show_fee_coins=Decimal("15.00"))
    driver = await _seed_user(factory, tenant_id=tenant_id)
    now = _now()
    resp = await _book(factory, monkeypatch, plug_id=plug_id, user_id=driver,
                       start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2))
    assert resp.no_show_fee_coins == 15.0
    assert (await _get_reservation(factory, resp.id)).no_show_fee_coins == Decimal("15.00")


@requires_db
@pytest.mark.asyncio
async def test_lapsed_booking_cannot_be_cancelled_to_dodge_the_fee(factory, monkeypatch):
    from fastapi import HTTPException

    site = await _no_show_site(factory)
    with pytest.raises(HTTPException) as exc:
        await _cancel(factory, monkeypatch, reservation_id=site.resv_id,
                      user=_fake_user(site.driver))
    assert exc.value.status_code == 409


# --- Review round: the holder showed up, or the charger was out -----------------


def test_routine_occurrence_in_window():
    from datetime import time

    tz = timezone.utc
    start = datetime(2026, 11, 16, 22, 0, tzinfo=tz)  # a Monday
    end = start + timedelta(hours=4)
    monday, tuesday = 1, 2
    assert policy.routine_occurs_in(monday, time(23, 30), tz, start, end)
    # Past midnight the occurrence belongs to Tuesday.
    assert policy.routine_occurs_in(tuesday, time(1, 0), tz, start, end)
    assert not policy.routine_occurs_in(monday, time(1, 0), tz, start, end)
    assert not policy.routine_occurs_in(tuesday, time(23, 30), tz, start, end)
    # end is exclusive, start inclusive.
    assert not policy.routine_occurs_in(tuesday, time(2, 0), tz, start, end)
    assert policy.routine_occurs_in(monday, time(22, 0), tz, start, end)


async def _seed_queued(factory, site, *, created_at, not_before, status=None):
    from backend.database.models import QueuedCharge, QueuedChargeStatus

    async with factory() as db:
        db.add(QueuedCharge(
            tenant_id=site.tenant_id, user_id=site.driver, plug_id=site.plug_id,
            status=status or QueuedChargeStatus.WAITING, created_at=created_at,
            not_before=not_before, expires_at=not_before + timedelta(minutes=30),
            max_duration_seconds=7200,
        ))
        await db.commit()


async def _audit(factory, site, action, *, target_id=None):
    from backend.database.models import AuditLog

    async with factory() as db:
        db.add(AuditLog(
            tenant_id=site.tenant_id, action=action, target_type="plug",
            target_id=str(target_id or site.plug_id), detail="test",
        ))
        await db.commit()


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_when_holder_scheduled_a_charge_in_the_window(factory, notify_mock):
    site = await _no_show_site(factory)
    start = site.now - timedelta(minutes=policy.RESERVATION_NO_SHOW_GRACE_MIN + 1)
    # Plugged in 5 minutes into the booking, start scheduled for later in it.
    await _seed_queued(factory, site, created_at=start + timedelta(minutes=5),
                       not_before=start + timedelta(minutes=40))
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []
    assert notify_mock.await_count == 0


@requires_db
@pytest.mark.asyncio
async def test_old_queued_charge_does_not_excuse_a_no_show(factory, notify_mock):
    from backend.database.models import QueuedChargeStatus

    site = await _no_show_site(factory)
    start = site.now - timedelta(minutes=policy.RESERVATION_NO_SHOW_GRACE_MIN + 1)
    # Expired the day before, and one the holder cancelled: neither is a show.
    await _seed_queued(factory, site, created_at=start - timedelta(days=1, hours=1),
                       not_before=start - timedelta(days=1),
                       status=QueuedChargeStatus.EXPIRED)
    await _seed_queued(factory, site, created_at=start + timedelta(minutes=1),
                       not_before=start + timedelta(minutes=30),
                       status=QueuedChargeStatus.CANCELLED)
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 1


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_when_holder_routine_falls_in_the_window(factory, notify_mock):
    from backend.database.models import ChargeRoutine
    from backend.services.pricing import _zone

    site = await _no_show_site(factory)
    start = site.now - timedelta(minutes=policy.RESERVATION_NO_SHOW_GRACE_MIN + 1)
    occ = (start + timedelta(minutes=40)).astimezone(_zone("Asia/Kolkata"))
    async with factory() as db:
        db.add(ChargeRoutine(
            user_id=site.driver, plug_id=site.plug_id, tenant_id=site.tenant_id,
            weekdays=1 << occ.weekday(), start_time=occ.time().replace(second=0, microsecond=0),
            max_duration_seconds=7200,
        ))
        await db.commit()
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_when_maintenance_entered_and_cleared_in_window(factory, notify_mock):
    site = await _no_show_site(factory)
    # The audit rows land "now", inside the window; the plug is AVAILABLE again.
    await _audit(factory, site, "plug.maintenance_enter")
    await _audit(factory, site, "plug.maintenance_clear")
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []


@requires_db
@pytest.mark.asyncio
async def test_maintenance_on_another_plug_does_not_excuse(factory, notify_mock):
    site = await _no_show_site(factory)
    await _audit(factory, site, "plug.maintenance_clear", target_id=site.plug_id + 1000)
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 1


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_after_a_safety_cutoff_in_window(factory, notify_mock):
    from backend.database.models import GatewayEvent

    site = await _no_show_site(factory)
    async with factory() as db:
        db.add(GatewayEvent(tenant_id=site.tenant_id, gateway_id=site.gw, plug_id=site.plug_id,
                            event_type="THERMAL_CUTOFF", severity="critical"))
        await db.commit()
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0


@requires_db
@pytest.mark.asyncio
async def test_fee_skipped_when_charger_closed_at_the_start(factory, notify_mock):
    from backend.database.models import Plug
    from backend.services.pricing import _zone

    site = await _no_show_site(factory)
    start = site.now - timedelta(minutes=policy.RESERVATION_NO_SHOW_GRACE_MIN + 1)
    # Open only on a weekday three days away: closed for the whole window.
    other_day = (start.astimezone(_zone("Asia/Kolkata")).weekday() + 3) % 7
    await _update(factory, Plug, site.plug_id,
                  opening_hours=[{"days": 1 << other_day, "start": 0, "end": 1440}])
    assert await policy.reap_reservation_no_shows_once(factory, now=site.now) == 0
    assert await _ledger(factory, site.driver) == []
