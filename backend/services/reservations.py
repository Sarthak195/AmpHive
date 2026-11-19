"""
Reservation policy + shared helpers (feat/reservations, 2026-07-12).

Group members book a future window on a charger (routers/reservations.py);
during that window only the holder can start a session (the gate in
routers/sessions.py start_charging_session). No coin is held for a booking.
The only money movement anywhere in the lifecycle is the optional, capped
[C8] no-show fee, charged solely by reap_reservation_no_shows_once below.

Policy knobs (env). All read with the `or "x"` guard (not a getenv default):
compose passes vars through with plain ${VAR} interpolation, so an unset
.env entry arrives as an EMPTY string — float("")/int("") would crash-loop
the container (the GST_RATE_PCT lesson; same guard as
MIN_START_BALANCE_COINS in routers/sessions.py).

Expiry is LAZY — there is no background sweep. expire_lapsed_reservations()
is called wherever reservations are read (the session-start gate, the
booking overlap check, every list endpoint), so a no-show hold never blocks
anyone even if the process never runs a janitor. Piggybacking a sweep (+ a
"your reservation just started" nudge) onto SessionReaperService is a noted
follow-up, not a correctness requirement.

[C8 approval mode] A plug whose reservation_mode is "approval" takes
bookings as PENDING requests. HOLD_STATUSES (BOOKED + PENDING) is what blocks
an overlapping booking and counts toward the per-user cap; only BOOKED gates a
session start. A request still PENDING at start_at lazily flips to EXPIRED
here, so an unanswered request never outlives its own window.

[C8 no-show fee] Optional, per plug, default 0, capped by
RESERVATION_NO_SHOW_FEE_MAX_COINS. The fee in force when a booking is made is
snapshotted onto it (what the driver was shown). Only the reaper sweep
reap_reservation_no_shows_once charges it, never the lazy
expire_lapsed_reservations UPDATE: once per reservation (the stamp
no_show_checked_at plus the UNIQUE ledger_transactions.reservation_id), only
for a window the sweep saw lapse moments ago, skipped whenever the plug or its
gateway may have been unusable in the window (offline, an outage or safety
cutoff event, a maintenance or status change audited since start_at, or
outside its opening hours at the start), skipped when the holder showed up
another way (a session on the plug, or their own scheduled or queued charge
or weekly routine on it inside the window), and at most the driver's
available balance, so a balance never goes negative.
"""
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable, Optional

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    AuditLog,
    ChargerGroup,
    ChargeRoutine,
    ChargingSession,
    Gateway,
    GatewayEvent,
    GroupMembership,
    LedgerTransaction,
    Plug,
    PlugStatus,
    QueuedCharge,
    QueuedChargeStatus,
    Reservation,
    ReservationStatus,
    Tenant,
    TransactionType,
    User,
)
from backend.services.money import ZERO_MONEY, to_money

logger = logging.getLogger("amphive.reservations")

# Longest bookable window, in hours.
RESERVATION_MAX_HOURS = float(os.getenv("RESERVATION_MAX_HOURS") or "4")
# How far ahead a window may START, in days.
RESERVATION_MAX_ADVANCE_DAYS = float(os.getenv("RESERVATION_MAX_ADVANCE_DAYS") or "7")
# Cap of BOOKED-with-end_at-in-the-future reservations per user (409 past it).
MAX_UPCOMING_RESERVATIONS_PER_USER = int(os.getenv("MAX_UPCOMING_RESERVATIONS_PER_USER") or "2")
# No-show grace: a BOOKED reservation still unfulfilled this many minutes
# after start_at lazily flips to EXPIRED and stops blocking anyone.
RESERVATION_NO_SHOW_GRACE_MIN = float(os.getenv("RESERVATION_NO_SHOW_GRACE_MIN") or "15")

# Advance-warning lookahead for a walk-up: when a driver starts a session on a
# plug another member has booked SOON, warn them it will be force-stopped when
# that window opens (the reservation-start janitor in
# services/session_reaper.py). A session with no duration cap could run
# indefinitely, so we warn about bookings within this horizon; a capped session
# uses its own cap when that reaches further. Env-tunable, in minutes.
RESERVATION_WALKUP_WARN_LOOKAHEAD_MIN = float(
    os.getenv("RESERVATION_WALKUP_WARN_LOOKAHEAD_MIN") or "180"
)

# Not env-tunable: the floor on a booking's length, and the small tolerance
# that lets "book starting now" survive clock skew / form-submit latency.
RESERVATION_MIN_MINUTES = 15
RESERVATION_START_GRACE_MIN = 2

# [C8] Plug.reservation_mode values, and the statuses that hold a slot
# against other bookings (a PENDING request does; it never gates a start).
RESERVATION_MODES = ("instant", "approval")
HOLD_STATUSES = (ReservationStatus.BOOKED, ReservationStatus.PENDING)


def reservation_mode_of(plug) -> str:
    """The plug's booking mode, "instant" for anything unrecognized (the
    services/plug_hours.py accessor convention)."""
    value = getattr(plug, "reservation_mode", None)
    return value if value in RESERVATION_MODES else "instant"


# [C8] No-show fee policy (env, same `or "x"` guard as above). The cap bounds
# what a CPO can set and is applied again when the fee is charged.
RESERVATION_NO_SHOW_FEE_MAX_COINS = to_money(
    os.getenv("RESERVATION_NO_SHOW_FEE_MAX_COINS") or "100"
)
# The sweep charges only a window whose grace lapsed within this many
# minutes: one that lapsed while the backend was down is never charged.
RESERVATION_NO_SHOW_SWEEP_MAX_LAG_MIN = float(
    os.getenv("RESERVATION_NO_SHOW_SWEEP_MAX_LAG_MIN") or "10"
)
NO_SHOW_SWEEP_BATCH = 50
# Gateway events that mean the charger was unreachable at some point, or (the
# safety cutoffs) that it was auto-set to MAINTENANCE (services/mqtt/alarms.py).
_OUTAGE_EVENT_TYPES = (
    "GATEWAY_BOOT", "PLUG_UNREACHABLE", "THERMAL_CUTOFF", "OVERCURRENT_CUTOFF",
)
# Audited CPO actions that take a plug out of service or hand it back
# (routers/cpo/_plugs.py). One since start_at means it may have been out.
_SERVICE_AUDIT_ACTIONS = (
    "plug.maintenance_enter", "plug.maintenance_clear", "plug.status_change",
)
# Queued charges that show the holder meant to charge in their window. A
# CANCELLED row was withdrawn by its owner; a STARTED one has a session,
# which the in-use check already sees.
_SHOW_QUEUE_STATUSES = (
    QueuedChargeStatus.WAITING, QueuedChargeStatus.FAILED, QueuedChargeStatus.EXPIRED,
)


def no_show_fee_of(plug) -> Decimal:
    """The no-show fee of a plug (or the snapshot on a reservation) as money,
    0 when unset or unrecognized, never above the server cap (the accessor
    convention above)."""
    value = getattr(plug, "no_show_fee_coins", None)
    if not isinstance(value, (Decimal, int, float)) or value <= 0:
        return ZERO_MONEY
    return min(to_money(value), RESERVATION_NO_SHOW_FEE_MAX_COINS)


def no_show_fee_amount(fee, available) -> Decimal:
    """What a no-show is charged: the booked fee, capped by the server max and
    by the driver's available balance, never below zero."""
    amount = min(to_money(fee), RESERVATION_NO_SHOW_FEE_MAX_COINS, to_money(available))
    return amount if amount > ZERO_MONEY else ZERO_MONEY


def no_show_skip_reason(plug, gateway, *, start_at: datetime, now: datetime) -> Optional[str]:
    """Why the charger may have been unusable during the no-show window, or
    None when every signal says it was usable throughout. Unknown counts as
    unusable: blocking a fee is the safe direction. Needs: the plug in
    service (not OFFLINE or MAINTENANCE), its gateway live now, and the plug
    powered now without a break since before start_at (powered_since is
    re-baselined whenever telemetry resumes after a gap)."""
    from backend.services.session_lifecycle import gateway_is_live, plug_is_powered

    if plug is None or plug.status in (PlugStatus.OFFLINE, PlugStatus.MAINTENANCE):
        return "plug out of service"
    if gateway is None or not gateway_is_live(gateway, now):
        return "gateway offline"
    since = plug.powered_since
    if not plug_is_powered(plug, now) or since is None:
        return "plug offline"
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if since > start_at:
        return "plug offline during the window"
    return None


def routine_occurs_in(
    weekdays: int, start_time: time, tz, start_at: datetime, end_at: datetime
) -> bool:
    """True when a weekly routine (weekday bitmask, Monday=1, local start
    time in zone tz) has an occurrence in [start_at, end_at). Pure; the
    services/routines.py due_occurrence arithmetic."""
    d: date = start_at.astimezone(tz).date() - timedelta(days=1)
    last = end_at.astimezone(tz).date()
    while d <= last:
        if weekdays & (1 << d.weekday()):
            occ = datetime.combine(d, start_time, tzinfo=tz).astimezone(timezone.utc)
            if start_at <= occ < end_at:
                return True
        d += timedelta(days=1)
    return False


async def expire_lapsed_reservations(
    db: AsyncSession,
    *,
    plug_id: Optional[int] = None,
    plug_ids: Optional[Iterable[int]] = None,
    user_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """
    Flip lapsed BOOKED reservations to EXPIRED: past start_at +
    RESERVATION_NO_SHOW_GRACE_MIN with no fulfilment (fulfilment would have
    made them FULFILLED already), or past end_at entirely. Returns the number
    of rows flipped.

    Deliberately does NOT commit — it runs inside the caller's transaction.
    That matters in the session-start gate, which holds the plug row lock
    (a commit there would release it mid-check); read-path callers (the list
    endpoints) commit afterwards themselves so the flip persists. Losing the
    flip to a caller's rollback is harmless — the next reader re-applies it.

    The optional filters (plug/user/tenant) just scope the UPDATE to the rows
    the caller is about to read, keeping it cheap and lock-narrow.
    """
    now = now or datetime.now(timezone.utc)
    conditions = [
        or_(
            and_(
                Reservation.status == ReservationStatus.BOOKED,
                or_(
                    Reservation.start_at <= now - timedelta(minutes=RESERVATION_NO_SHOW_GRACE_MIN),
                    Reservation.end_at <= now,
                ),
            ),
            # [C8] An unanswered approval request expires at its start.
            and_(
                Reservation.status == ReservationStatus.PENDING,
                Reservation.start_at <= now,
            ),
        ),
    ]
    if plug_id is not None:
        conditions.append(Reservation.plug_id == plug_id)
    if plug_ids is not None:
        conditions.append(Reservation.plug_id.in_(list(plug_ids)))
    if user_id is not None:
        conditions.append(Reservation.user_id == user_id)
    if tenant_id is not None:
        conditions.append(Reservation.tenant_id == tenant_id)

    # [C8] A lapsed PENDING request is stamped as settled, so the no-show
    # sweep never charges a booking the host never confirmed. Nothing is
    # charged here: only reap_reservation_no_shows_once charges.
    result = await db.execute(
        update(Reservation)
        .where(and_(*conditions))
        .values(
            status=ReservationStatus.EXPIRED,
            no_show_checked_at=case(
                (Reservation.status == ReservationStatus.PENDING, now),
                else_=Reservation.no_show_checked_at,
            ),
        )
    )
    return result.rowcount or 0


async def user_can_access_plug(db: AsyncSession, user_id: int, plug: Plug) -> bool:
    """
    The plug access rule, extracted verbatim from the session-start path
    (routers/sessions.py start_charging_session) for the booking endpoints:
    a plug is accessible when it is ungrouped (legacy/public to all), in a
    public group, or in a private group the user has joined.
    """
    if not plug.group_id:
        return True
    group = (await db.execute(
        select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
    )).scalar_one_or_none()
    if group is None or group.is_public:
        return True
    membership = (await db.execute(
        select(GroupMembership).where(
            and_(
                GroupMembership.user_id == user_id,
                GroupMembership.group_id == group.id,
            )
        )
    )).scalar_one_or_none()
    return membership is not None


def format_window(start_at: datetime, end_at: datetime) -> str:
    """
    Human-readable UTC window for notification bodies / error details, e.g.
    "2026-07-13 14:00–15:00 UTC" (the end keeps its date when it crosses
    midnight). The backend doesn't know the driver's timezone — the SPA
    renders localized times itself; these strings are explicitly UTC.
    """
    start_utc = start_at.astimezone(timezone.utc)
    end_utc = end_at.astimezone(timezone.utc)
    if start_utc.date() == end_utc.date():
        return f"{start_utc:%Y-%m-%d %H:%M}–{end_utc:%H:%M} UTC"
    return f"{start_utc:%Y-%m-%d %H:%M}–{end_utc:%Y-%m-%d %H:%M} UTC"


async def next_conflicting_reservation(
    db: AsyncSession,
    plug_id: int,
    exclude_user_id: int,
    *,
    horizon: datetime,
    now: Optional[datetime] = None,
) -> Optional[Reservation]:
    """The soonest BOOKED reservation on this plug held by someone OTHER than
    exclude_user_id that starts in (now, horizon] — i.e. one a session starting
    now would run into and be force-stopped for when the window opens (see
    services/session_reaper.py reap_reservation_starts_once). Returns the
    Reservation or None. Read-only and lock-free: the session-start path calls
    it only to warn the walk-up driver up front, off the plug-locked critical
    section, so it must never take a lock or block the start."""
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(Reservation)
        .where(
            and_(
                Reservation.plug_id == plug_id,
                Reservation.status == ReservationStatus.BOOKED,
                Reservation.user_id != exclude_user_id,
                Reservation.start_at > now,
                Reservation.start_at <= horizon,
            )
        )
        .order_by(Reservation.start_at)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def reap_reservation_no_shows_once(
    session_factory: Callable, now: Optional[datetime] = None
) -> int:
    """[C8] Charge the no-show fee for each confirmed booking whose
    RESERVATION_NO_SHOW_GRACE_MIN lapsed within the last
    RESERVATION_NO_SHOW_SWEEP_MAX_LAG_MIN minutes with no session (status
    BOOKED or already lazily EXPIRED, session_id NULL, fee snapshot > 0, not
    yet settled). Older rows are never charged: the backend may have been down
    when they lapsed. One transaction per row; one row's failure never aborts
    the sweep. Returns the number of fees actually charged."""
    now = now or datetime.now(timezone.utc)
    grace = timedelta(minutes=RESERVATION_NO_SHOW_GRACE_MIN)
    oldest = now - grace - timedelta(minutes=RESERVATION_NO_SHOW_SWEEP_MAX_LAG_MIN)
    async with session_factory() as db:
        rows = (await db.execute(
            select(Reservation.id, Reservation.user_id)
            .where(
                and_(
                    Reservation.no_show_checked_at.is_(None),
                    Reservation.no_show_fee_coins > 0,
                    Reservation.start_at <= now - grace,
                    Reservation.start_at > oldest,
                    Reservation.status.in_((ReservationStatus.BOOKED, ReservationStatus.EXPIRED)),
                    Reservation.session_id.is_(None),
                )
            )
            .order_by(Reservation.start_at)
            .limit(NO_SHOW_SWEEP_BATCH)
        )).all()

    charged = 0
    for reservation_id, user_id in rows:
        try:
            if await _settle_no_show(session_factory, reservation_id, user_id, now, grace):
                charged += 1
        except Exception:
            logger.exception(f"No-show fee settle failed for reservation {reservation_id}")
    if charged:
        logger.info(f"No-show sweep charged {charged} fee(s)")
    return charged


async def _settle_no_show(
    session_factory: Callable, reservation_id: int, user_id: int,
    now: datetime, grace: timedelta,
) -> bool:
    """Settle one candidate: stamp it (charged or skipped, so it is decided
    exactly once), then charge it when every check passes. True when a fee
    was charged."""
    from backend.services.notifications import notify
    from backend.services.wallet import available_balance, debit_wallet_clamped

    async with session_factory() as db:
        # Lock order user -> reservation: the session-start path holds the
        # user row, then the plug row, then touches this driver's lapsed
        # reservations, so taking the reservation first could deadlock.
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
        reservation = (await db.execute(
            select(Reservation)
            .where(
                and_(
                    Reservation.id == reservation_id,
                    Reservation.no_show_checked_at.is_(None),
                    Reservation.session_id.is_(None),
                    Reservation.status.in_((ReservationStatus.BOOKED, ReservationStatus.EXPIRED)),
                )
            )
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if reservation is None:
            return False
        reservation.status = ReservationStatus.EXPIRED
        reservation.no_show_checked_at = now

        plug = (await db.execute(
            select(Plug).where(Plug.id == reservation.plug_id)
        )).scalar_one_or_none()
        gateway = None
        if plug is not None:
            gateway = (await db.execute(
                select(Gateway).where(Gateway.id == plug.gateway_id)
            )).scalar_one_or_none()
        start_at = reservation.start_at
        reason = no_show_skip_reason(plug, gateway, start_at=start_at, now=now)
        if reason is None:
            assert plug is not None and gateway is not None  # checked just above
            reason = await _window_skip_reason(db, reservation, plug, gateway, grace)

        amount = ZERO_MONEY
        if reason is None:
            amount = no_show_fee_amount(
                reservation.no_show_fee_coins, await available_balance(db, user_id)
            )
        if reason is not None or amount <= ZERO_MONEY:
            await db.commit()
            logger.info(
                "No-show fee not charged",
                extra={"reservation_id": reservation_id, "reason": reason or "no available balance"},
            )
            return False

        assert plug is not None  # reason is None only with a plug
        actual, new_balance = await debit_wallet_clamped(db, user_id, amount)
        window = format_window(start_at, reservation.end_at)
        db.add(LedgerTransaction(
            user_id=user_id,
            amount=-actual,
            transaction_type=TransactionType.RESERVATION_NO_SHOW_FEE,
            description=f"No-show fee: {plug.name}, {window}"[:255],
            balance_after=new_balance,
            reservation_id=reservation_id,
        ))
        try:
            await db.commit()
        except IntegrityError:
            # The UNIQUE reservation_id: this booking was already charged.
            await db.rollback()
            return False

    await notify(
        user_id,
        "reservation_no_show_fee",
        "No-show fee charged",
        (
            f"{actual} coins were charged because your reservation on {plug.name} "
            f"({window}) was not used."
        ),
        plug_id=plug.id,
    )
    logger.info(
        "No-show fee charged",
        extra={"reservation_id": reservation_id, "user_id": user_id, "amount": str(actual)},
    )
    return True


async def _window_skip_reason(
    db: AsyncSession, reservation: Reservation, plug: Plug, gateway: Gateway,
    grace: timedelta,
) -> Optional[str]:
    """The no-show checks that need the database: why the window may not have
    been the holder's fault, or None. Every query is narrowed by an index
    (gateway events by gateway, audit rows by tenant and time, sessions by
    plug, queued charges and routines by user)."""
    from backend.services import plug_hours
    from backend.services.pricing import _zone

    start_at, end_at = reservation.start_at, reservation.end_at
    outage = (await db.execute(
        select(GatewayEvent.id)
        .where(
            and_(
                GatewayEvent.gateway_id == gateway.id,
                GatewayEvent.created_at >= start_at,
                GatewayEvent.event_type.in_(_OUTAGE_EVENT_TYPES),
                or_(GatewayEvent.plug_id.is_(None), GatewayEvent.plug_id == plug.id),
            )
        )
        .limit(1)
    )).scalar_one_or_none()
    if outage is not None:
        return "gateway or plug outage in the window"

    # Maintenance does not stop telemetry, so a plug put into maintenance
    # and cleared again before the sweep only shows up in the audit trail.
    serviced = (await db.execute(
        select(AuditLog.id)
        .where(
            and_(
                AuditLog.tenant_id == gateway.tenant_id,
                AuditLog.created_at >= start_at,
                AuditLog.action.in_(_SERVICE_AUDIT_ACTIONS),
                AuditLog.target_type == "plug",
                AuditLog.target_id == str(plug.id),
            )
        )
        .limit(1)
    )).scalar_one_or_none()
    if serviced is not None:
        return "plug out of service in the window"

    tz_name = None
    if gateway.tenant_id is not None:
        tz_name = (await db.execute(
            select(Tenant.timezone).where(Tenant.id == gateway.tenant_id)
        )).scalar_one_or_none()
    if plug_hours.hours_of(plug) is not None and not plug_hours.window_is_open(
        plug, tz_name, start_at, start_at + grace
    ):
        return "charger closed during the window"

    in_use = (await db.execute(
        select(ChargingSession.id)
        .where(
            and_(
                ChargingSession.plug_id == plug.id,
                ChargingSession.started_at < start_at + grace,
                or_(
                    ChargingSession.ended_at.is_(None),
                    ChargingSession.ended_at > start_at,
                ),
            )
        )
        .limit(1)
    )).scalar_one_or_none()
    if in_use is not None:
        return "plug in use during the window"

    # The holder may plug in and schedule their charge for later in their
    # own window (routers/sessions.py lets them): that is a show.
    queued = (await db.execute(
        select(QueuedCharge.id)
        .where(
            and_(
                QueuedCharge.user_id == reservation.user_id,
                QueuedCharge.plug_id == plug.id,
                QueuedCharge.status.in_(_SHOW_QUEUE_STATUSES),
                QueuedCharge.created_at < start_at + grace,
                QueuedCharge.expires_at > start_at,
                or_(QueuedCharge.not_before.is_(None), QueuedCharge.not_before < end_at),
            )
        )
        .limit(1)
    )).scalar_one_or_none()
    if queued is not None:
        return "holder scheduled a charge"

    # A weekly routine materializes its queued charge only at its start time,
    # so an occurrence later in the window has no row yet at the sweep.
    routines = (await db.execute(
        select(ChargeRoutine.weekdays, ChargeRoutine.start_time)
        .where(
            and_(
                ChargeRoutine.user_id == reservation.user_id,
                ChargeRoutine.plug_id == plug.id,
                ChargeRoutine.enabled.is_(True),
            )
        )
        .limit(10)
    )).all()
    tz = _zone(tz_name)
    for weekdays, start_time in routines:
        if routine_occurs_in(weekdays, start_time, tz, start_at, end_at):
            return "holder scheduled a charge"
    return None
