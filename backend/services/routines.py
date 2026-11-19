"""
Recurring weekly charge routines (feature C5).

A routine is NOT a start path. On each due occurrence the session reaper calls
materialize_routines(), which inserts an ordinary scheduled QueuedCharge (the
exact row POST /api/sessions/queue with not_before would create);
reap_queued_starts_once then does every admission check, the wallet hold and
billing unchanged, and is the only thing that can switch the plug on. After
that sweep, settle_routines() reads what happened to the queued row and keeps
the routine's failure count:

  - STARTED            -> consecutive_failures = 0, notify "routine_started".
  - EXPIRED / FAILED   -> consecutive_failures += 1; the third in a row
                          disables the routine and notifies "routine_disabled".
  - CANCELLED          -> no count (the driver cancelled it, or started the
                          plug themselves first).

Due-ness is evaluated per occurrence date in the plug's tenant timezone
(users have no zone): an occurrence on local date D fires when D's weekday bit
is set, its local start time has been reached within the materialize window,
and last_materialized_date is before D. Both local today and local yesterday
are checked, so a 23:58 routine ticked at 00:01 still fires (for yesterday).
The window is short and capped by the scheduled-start grace, so a row is never
born already expired; an occurrence missed while the backend was down is
skipped, which is the safe direction for an unattended start.

Idempotency: the routine row is locked FOR UPDATE SKIP LOCKED and
last_materialized_date is stamped in the same transaction as the insert, so
a double tick (or two workers) queues one row. The QueuedCharge partial unique
index is the backstop: losing it still stamps the date.
"""
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional, Tuple

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError

from backend.database.models import (
    ChargerGroup,
    ChargeRoutine,
    Gateway,
    GroupMembership,
    Plug,
    QueuedCharge,
    QueuedChargeStatus,
    Tenant,
    User,
)
from backend.services import pricing
from backend.services.session_start import queued_charging_enabled, schedule_ttl

logger = logging.getLogger("amphive.routines")

MAX_ROUTINES_PER_USER = 5
# Consecutive failed occurrences after which a routine turns itself off.
ROUTINE_FAILURE_LIMIT = 3
# How long after its local start time an occurrence may still be queued.
# Also capped per tenant by schedule_ttl so a row is never born expired.
MATERIALIZE_WINDOW = timedelta(minutes=5)
# Same name and default as routers/sessions.py (read independently: services
# do not import routers). A routine occurrence respects the same cap the queue
# endpoint enforces at creation.
MAX_QUEUED_CHARGES_PER_USER = int(os.getenv("MAX_QUEUED_CHARGES_PER_USER", "2"))

Notice = Tuple[str, str, str, str]  # (type, title, body, severity)


def weekday_bit(d: date) -> int:
    """Monday=1, Tuesday=2, ... Sunday=64."""
    return 1 << d.weekday()


def due_occurrence(
    weekdays: int,
    start_time: time,
    last_date: Optional[date],
    tz,
    now: datetime,
    window: timedelta = MATERIALIZE_WINDOW,
) -> Optional[Tuple[date, datetime]]:
    """The (local date, UTC start instant) of the occurrence due at `now`, or
    None. PURE: the unit-tested core of the materialize pass."""
    local_today = now.astimezone(tz).date()
    for d in (local_today - timedelta(days=1), local_today):
        if not weekdays & weekday_bit(d):
            continue
        if last_date is not None and last_date >= d:
            continue
        start = datetime.combine(d, start_time, tzinfo=tz).astimezone(timezone.utc)
        if start <= now < start + window:
            return d, start
    return None


def build_queued_charge(
    routine: ChargeRoutine, tenant: Tenant, plug: Plug, gateway: Gateway, not_before: datetime
) -> QueuedCharge:
    """The row queue_charge's scheduled path creates (routers/sessions.py),
    field for field: tenant from the gateway, the snapshot limits, the
    opt-in as True-or-NULL, and expires_at = not_before + schedule_ttl."""
    return QueuedCharge(
        tenant_id=gateway.tenant_id,
        user_id=routine.user_id,
        plug_id=plug.id,
        max_kwh=routine.max_kwh,
        max_duration_seconds=routine.max_duration_seconds,
        auto_stop_when_full=True if routine.auto_stop_when_full else None,
        status=QueuedChargeStatus.WAITING,
        not_before=not_before,
        expires_at=not_before + timedelta(minutes=schedule_ttl(tenant, plug)),
    )


def _record_failure(routine: ChargeRoutine, plug_name: str, why: str) -> Notice:
    """Count a failed occurrence; the third in a row disables the routine."""
    routine.consecutive_failures = (routine.consecutive_failures or 0) + 1
    if routine.consecutive_failures >= ROUTINE_FAILURE_LIMIT:
        routine.enabled = False
        return (
            "routine_disabled",
            "Charge routine turned off",
            f"Your routine on {plug_name} failed {routine.consecutive_failures} times "
            f"in a row ({why}), so it was turned off. Turn it back on from Account.",
            "warning",
        )
    return (
        "routine_skipped",
        "Charge routine skipped",
        f"Your routine on {plug_name} didn't run today: {why}.",
        "warning",
    )


async def _send(user_id: int, plug_id: int, notice: Optional[Notice], session_id: Optional[int] = None) -> None:
    if notice is None:
        return
    from backend.services.notifications import notify

    type_, title, body, severity = notice
    await notify(user_id, type_, title, body, severity=severity, plug_id=plug_id, session_id=session_id)


async def has_group_access(db, user_id: int, plug: Plug) -> bool:
    """The queue endpoint's private-group gate (routers/sessions.py)."""
    if not plug.group_id:
        return True
    group = (await db.execute(
        select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
    )).scalar_one_or_none()
    if group is None or group.is_public:
        return True
    member = (await db.execute(
        select(GroupMembership).where(and_(
            GroupMembership.user_id == user_id,
            GroupMembership.group_id == group.id,
        ))
    )).scalar_one_or_none()
    return member is not None


async def _materialize_one(factory: Callable, routine_id: int, now: datetime) -> bool:
    """One routine, one transaction. Returns True when a row was queued."""
    notice: Optional[Notice] = None
    async with factory() as db:
        routine = (await db.execute(
            select(ChargeRoutine)
            .where(and_(ChargeRoutine.id == routine_id, ChargeRoutine.enabled.is_(True)))
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if routine is None:
            return False  # disabled/deleted since the scan, or another tick holds it
        plug = (await db.execute(select(Plug).where(Plug.id == routine.plug_id))).scalar_one()
        gateway = (await db.execute(
            select(Gateway).where(Gateway.id == plug.gateway_id)
        )).scalar_one()
        tenant = (await db.execute(
            select(Tenant).where(Tenant.id == gateway.tenant_id)
        )).scalar_one()
        window = min(MATERIALIZE_WINDOW, timedelta(minutes=schedule_ttl(tenant, plug)))
        occ = due_occurrence(
            routine.weekdays, routine.start_time, routine.last_materialized_date,
            pricing._zone(tenant.timezone), now, window,
        )
        if occ is None:
            await db.commit()  # a racer already handled this occurrence
            return False
        occ_date, not_before = occ
        # From here every branch handles this occurrence: stamp it.
        routine.last_materialized_date = occ_date
        user_id, plug_id, plug_name = routine.user_id, plug.id, plug.name

        user = (await db.execute(
            select(User.is_disabled, User.deleted_at).where(User.id == user_id)
        )).one_or_none()
        if user is None or user.is_disabled or user.deleted_at is not None:
            await db.commit()  # never start for a disabled or closed account
            return False

        if not queued_charging_enabled(tenant, plug):
            # Tell the driver once: the routine turns off and the API refuses
            # to turn it back on while scheduled charging stays off.
            routine.enabled = False
            await db.commit()
            await _send(user_id, plug_id, (
                "routine_disabled",
                "Charge routine turned off",
                f"Scheduled charging is no longer enabled on {plug_name}, so your "
                "routine was turned off.",
                "warning",
            ))
            return False

        own_waiting = (await db.execute(
            select(QueuedCharge.id).where(and_(
                QueuedCharge.plug_id == plug_id,
                QueuedCharge.user_id == user_id,
                QueuedCharge.status == QueuedChargeStatus.WAITING,
            ))
        )).scalar_one_or_none()
        if own_waiting is not None:
            await db.commit()  # the driver already has a charge queued here
            return False

        if not await has_group_access(db, user_id, plug):
            notice = _record_failure(routine, plug_name, "you no longer have access to this charger")
        elif (await db.execute(
            select(QueuedCharge.id).where(and_(
                QueuedCharge.plug_id == plug_id,
                QueuedCharge.status == QueuedChargeStatus.WAITING,
                QueuedCharge.user_id != user_id,
            )).limit(1)
        )).scalar_one_or_none() is not None:
            # Mirrors the scheduled path's plug_scheduled refusal: the car on
            # the socket may be the other driver's.
            notice = _record_failure(routine, plug_name, "another driver's charge is queued on it")
        elif (await db.execute(
            select(func.count()).select_from(QueuedCharge).where(and_(
                QueuedCharge.user_id == user_id,
                QueuedCharge.status == QueuedChargeStatus.WAITING,
            ))
        )).scalar_one() >= MAX_QUEUED_CHARGES_PER_USER:
            notice = _record_failure(routine, plug_name, "you already have the most queued charges allowed")
        if notice is not None:
            await db.commit()
            await _send(user_id, plug_id, notice)
            return False

        qc = build_queued_charge(routine, tenant, plug, gateway, not_before)
        try:
            async with db.begin_nested():
                db.add(qc)
                await db.flush()
        except IntegrityError:
            # The driver queued this plug in the same instant: the partial
            # unique index kept one WAITING row. The stamp still commits.
            await db.commit()
            return False
        routine.last_queued_charge_id = qc.id
        await db.commit()
        logger.info(
            "Charge routine queued",
            extra={"routine_id": routine_id, "queued_charge_id": qc.id, "plug_id": plug_id},
        )
        return True


async def materialize_routines(factory: Callable, now: Optional[datetime] = None) -> int:
    """Queue today's occurrence of every enabled routine that is due. A cheap
    read-only scan picks candidates; each is then handled in its own locked
    transaction. Returns the number of rows queued."""
    now = now or datetime.now(timezone.utc)
    async with factory() as db:
        rows = (await db.execute(
            select(
                ChargeRoutine.id,
                ChargeRoutine.weekdays,
                ChargeRoutine.start_time,
                ChargeRoutine.last_materialized_date,
                Tenant.timezone,
            )
            .join(Plug, Plug.id == ChargeRoutine.plug_id)
            .join(Gateway, Gateway.id == Plug.gateway_id)
            .join(Tenant, Tenant.id == Gateway.tenant_id)
            .where(ChargeRoutine.enabled.is_(True))
        )).all()
    candidates = [
        r.id for r in rows
        if due_occurrence(r.weekdays, r.start_time, r.last_materialized_date,
                          pricing._zone(r.timezone), now) is not None
    ]
    queued = 0
    for routine_id in candidates:
        try:
            if await _materialize_one(factory, routine_id, now):
                queued += 1
        except Exception:
            logger.exception(f"Failed to materialize charge routine {routine_id}")
    return queued


async def _settle_one(factory: Callable, routine_id: int) -> None:
    notice: Optional[Notice] = None
    async with factory() as db:
        routine = (await db.execute(
            select(ChargeRoutine)
            .where(and_(
                ChargeRoutine.id == routine_id,
                ChargeRoutine.last_queued_charge_id.is_not(None),
            ))
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if routine is None:
            return
        qc = (await db.execute(
            select(QueuedCharge).where(QueuedCharge.id == routine.last_queued_charge_id)
        )).scalar_one_or_none()
        if qc is not None and qc.status == QueuedChargeStatus.WAITING:
            await db.commit()  # not decided yet
            return
        routine.last_queued_charge_id = None
        user_id, plug_id = routine.user_id, routine.plug_id
        session_id = None
        if qc is not None and qc.status == QueuedChargeStatus.STARTED:
            routine.consecutive_failures = 0
            session_id = qc.started_session_id
            plug_name = (await db.execute(select(Plug.name).where(Plug.id == plug_id))).scalar_one()
            notice = (
                "routine_started",
                "Charge routine started",
                f"Your routine started charging on {plug_name}. It stops at its limit.",
                "info",
            )
        elif qc is not None and qc.status in (QueuedChargeStatus.EXPIRED, QueuedChargeStatus.FAILED):
            was_enabled = routine.enabled
            plug_name = (await db.execute(select(Plug.name).where(Plug.id == plug_id))).scalar_one()
            notice = _record_failure(routine, plug_name, "the charge couldn't start")
            # The reaper already told the driver about this failure; only the
            # switch-off is news (and not for a routine they turned off).
            if notice[0] != "routine_disabled" or not was_enabled:
                notice = None
        await db.commit()
    await _send(user_id, plug_id, notice, session_id=session_id)


async def settle_routines(factory: Callable) -> int:
    """Settle the outcome of every routine's last queued row once the reaper
    has decided it. Returns the number of routines examined."""
    async with factory() as db:
        ids = list((await db.execute(
            select(ChargeRoutine.id).where(ChargeRoutine.last_queued_charge_id.is_not(None))
        )).scalars().all())
    for routine_id in ids:
        try:
            await _settle_one(factory, routine_id)
        except Exception:
            logger.exception(f"Failed to settle charge routine {routine_id}")
    return len(ids)


async def cancel_pending(db, routine: ChargeRoutine) -> None:
    """Withdraw a routine's queued row that has not started yet (routine
    turned off or deleted). The status predicate is re-evaluated after any
    row lock wait, so a row the reaper just claimed STARTED is left alone."""
    if routine.last_queued_charge_id is None:
        return
    await db.execute(
        update(QueuedCharge)
        .where(and_(
            QueuedCharge.id == routine.last_queued_charge_id,
            QueuedCharge.status == QueuedChargeStatus.WAITING,
        ))
        .values(status=QueuedChargeStatus.CANCELLED)
        .execution_options(synchronize_session=False)
    )
