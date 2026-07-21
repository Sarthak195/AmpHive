"""
Plug reservations (feat/reservations, 2026-07-12) — the first prospective
client's use case: a private society/office group sharing one charger.
Group members book a future window; during that window only the holder can
start a session (the gate lives in routers/sessions.py, inside the
plug-locked section of start_charging_session), and everyone with plug
access can see the upcoming schedule.

FREE in v1: no coin hold, no money movement anywhere in the lifecycle.

Policy (env, read via services/reservations.py so tests can monkeypatch the
module attributes):
    RESERVATION_MAX_HOURS (default 4)            — longest bookable window
    RESERVATION_MAX_ADVANCE_DAYS (default 7)     — how far ahead a window may start
    MAX_UPCOMING_RESERVATIONS_PER_USER (default 2) — BOOKED-with-future-end cap
    RESERVATION_NO_SHOW_GRACE_MIN (default 15)   — lazy no-show expiry grace
Plus two fixed rules: 15-minute minimum duration, and a 2-minute past-start
grace so "book starting now" survives clock skew / form-submit latency.

Expiry is LAZY (no background sweep): every read path here — and the
session-start gate — first runs expire_lapsed_reservations(), so a no-show
hold never blocks a booking or a walk-up start.

GET /api/plugs/{plug_id}/reservations lives here rather than in
routers/plugs.py to keep that file untouched by this feature except for the
reserved_now/next_reservation response fields.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    Gateway,
    Plug,
    PlugStatus,
    Reservation,
    ReservationStatus,
    User,
    UserRole,
)
from backend.schemas import (
    MyReservationsResponse,
    ReservationCreateRequest,
    ReservationResponse,
)
from backend.services import reservations as reservation_policy
from backend.services.auth import get_current_user
from backend.services.notifications import notify
from backend.services.reservations import (
    expire_lapsed_reservations,
    format_window,
    user_can_access_plug,
)

logger = logging.getLogger("amphive.api")
router = APIRouter()


def _reservation_response(
    r: Reservation,
    *,
    plug_name=None,
    user_name=None,
    current_user_id=None,
) -> ReservationResponse:
    return ReservationResponse(
        id=r.id,
        plug_id=r.plug_id,
        user_id=r.user_id,
        tenant_id=r.tenant_id,
        start_at=r.start_at.isoformat(),
        end_at=r.end_at.isoformat(),
        status=r.status.value,
        session_id=r.session_id,
        created_at=r.created_at.isoformat() if r.created_at else None,
        plug_name=plug_name,
        user_name=user_name,
        is_mine=(r.user_id == current_user_id) if current_user_id is not None else None,
    )


@router.post("/api/reservations", response_model=ReservationResponse)
async def create_reservation(
    req: ReservationCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Book a [start_at, end_at) window on a plug.

    Order of checks:
    1. Window shape (no DB): duration 15 min–RESERVATION_MAX_HOURS, start no
       further back than 2 min (grace) and no further ahead than
       RESERVATION_MAX_ADVANCE_DAYS → 400.
    2. Lock the USER row, lazily expire the caller's lapsed holds, then
       enforce MAX_UPCOMING_RESERVATIONS_PER_USER (BOOKED with end_at in the
       future) → 409. The user lock serializes two concurrent bookings by
       the same user so both can't slip under the cap. Lock order is
       user -> plug, matching start_charging_session, so no lock cycle.
    3. Lock the PLUG row (concurrent bookings on one plug serialize here —
       the overlap check below is race-free without a DB exclusion
       constraint), 404 if missing, access rule (403 — same
       ungrouped/public/joined-private rule as session start), MAINTENANCE
       → 409 (OFFLINE is fine: an offline-right-now charger can be booked
       for later; the gateway-liveness gate still runs at session start).
    4. Lazily expire the plug's lapsed holds, then reject any [start, end)
       intersection with a BOOKED reservation → 409 (half-open windows:
       back-to-back bookings sharing an edge are legal).
    On success: tenant_id denormalized from plug -> gateway -> tenant
    (mirrors session start), confirmation notification (feed + Socket.io +
    Web Push; never raises).
    """
    now = datetime.now(timezone.utc)
    start_at, end_at = req.start_at, req.end_at

    # 1. Window shape — 400s with precise reasons.
    if end_at <= start_at:
        raise HTTPException(status_code=400, detail="end_at must be after start_at.")
    duration = end_at - start_at
    if duration < timedelta(minutes=reservation_policy.RESERVATION_MIN_MINUTES):
        raise HTTPException(
            status_code=400,
            detail=f"Reservations must be at least {reservation_policy.RESERVATION_MIN_MINUTES} minutes long.",
        )
    if duration > timedelta(hours=reservation_policy.RESERVATION_MAX_HOURS):
        raise HTTPException(
            status_code=400,
            detail=f"Reservations can be at most {reservation_policy.RESERVATION_MAX_HOURS:g} hours long.",
        )
    if start_at < now - timedelta(minutes=reservation_policy.RESERVATION_START_GRACE_MIN):
        raise HTTPException(status_code=400, detail="Reservation start is in the past.")
    if start_at > now + timedelta(days=reservation_policy.RESERVATION_MAX_ADVANCE_DAYS):
        raise HTTPException(
            status_code=400,
            detail=(
                "Reservations can start at most "
                f"{reservation_policy.RESERVATION_MAX_ADVANCE_DAYS:g} days ahead."
            ),
        )

    # 2. User-row lock + per-user cap.
    locked_user = await db.execute(
        select(User).where(User.id == user.id).with_for_update()
    )
    user = locked_user.scalar_one()

    await expire_lapsed_reservations(db, user_id=user.id, now=now)

    count_result = await db.execute(
        select(func.count())
        .select_from(Reservation)
        .where(
            and_(
                Reservation.user_id == user.id,
                Reservation.status == ReservationStatus.BOOKED,
                Reservation.end_at > now,
            )
        )
    )
    upcoming_count = count_result.scalar_one()
    cap = reservation_policy.MAX_UPCOMING_RESERVATIONS_PER_USER
    if upcoming_count >= cap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {upcoming_count} upcoming "
                f"reservation{'s' if upcoming_count != 1 else ''} (limit {cap}). "
                "Cancel one before booking another."
            ),
        )

    # 3. Plug-row lock + access + service checks.
    plug_result = await db.execute(
        select(Plug).where(Plug.id == req.plug_id).with_for_update()
    )
    plug = plug_result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug {req.plug_id} not found.")

    if not await user_can_access_plug(db, user.id, plug):
        raise HTTPException(
            status_code=403,
            detail="You don't have access to this plug. Join the group first.",
        )

    if plug.status == PlugStatus.MAINTENANCE:
        raise HTTPException(
            status_code=409,
            detail="This plug is under maintenance and cannot be reserved.",
        )

    # 4. Overlap check against BOOKED windows, under the plug lock.
    await expire_lapsed_reservations(db, plug_id=plug.id, now=now)

    conflict_result = await db.execute(
        select(Reservation)
        .where(
            and_(
                Reservation.plug_id == plug.id,
                Reservation.status == ReservationStatus.BOOKED,
                Reservation.start_at < end_at,
                Reservation.end_at > start_at,
            )
        )
        .order_by(Reservation.start_at)
        .limit(1)
    )
    conflict = conflict_result.scalar_one_or_none()
    if conflict is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "That slot overlaps an existing reservation "
                f"({format_window(conflict.start_at, conflict.end_at)})."
            ),
        )

    gw_result = await db.execute(
        select(Gateway.tenant_id).where(Gateway.id == plug.gateway_id)
    )
    tenant_id = gw_result.scalar_one()

    reservation = Reservation(
        plug_id=plug.id,
        user_id=user.id,
        tenant_id=tenant_id,
        start_at=start_at,
        end_at=end_at,
        status=ReservationStatus.BOOKED,
    )
    db.add(reservation)
    await db.commit()
    await db.refresh(reservation)

    await notify(
        user.id,
        "reservation_booked",
        "Reservation confirmed",
        f"{plug.name} is booked for you: {format_window(start_at, end_at)}.",
        plug_id=plug.id,
    )

    logger.info(
        "Reservation booked",
        extra={
            "reservation_id": reservation.id, "user_id": user.id,
            "plug_id": plug.id, "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
        },
    )

    return _reservation_response(
        reservation, plug_name=plug.name, current_user_id=user.id
    )


@router.get("/api/reservations/my", response_model=MyReservationsResponse)
async def my_reservations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    The caller's reservations: `upcoming` = BOOKED with end_at in the future,
    soonest first (drives the Home "Your reservations" strip); `history` =
    everything else (cancelled/fulfilled/expired/past), newest first,
    capped at 20.
    """
    now = datetime.now(timezone.utc)
    expired = await expire_lapsed_reservations(db, user_id=user.id, now=now)
    if expired:
        await db.commit()  # read path — persist the lazy flip

    upcoming_rows = await db.execute(
        select(Reservation, Plug.name)
        .join(Plug, Plug.id == Reservation.plug_id)
        .where(
            and_(
                Reservation.user_id == user.id,
                Reservation.status == ReservationStatus.BOOKED,
                Reservation.end_at > now,
            )
        )
        .order_by(Reservation.start_at)
    )
    upcoming = [
        _reservation_response(r, plug_name=plug_name, current_user_id=user.id)
        for r, plug_name in upcoming_rows.all()
    ]

    history_rows = await db.execute(
        select(Reservation, Plug.name)
        .join(Plug, Plug.id == Reservation.plug_id)
        .where(
            and_(
                Reservation.user_id == user.id,
                or_(
                    Reservation.status != ReservationStatus.BOOKED,
                    Reservation.end_at <= now,
                ),
            )
        )
        .order_by(Reservation.start_at.desc(), Reservation.id.desc())
        .limit(20)
    )
    history = [
        _reservation_response(r, plug_name=plug_name, current_user_id=user.id)
        for r, plug_name in history_rows.all()
    ]

    return MyReservationsResponse(upcoming=upcoming, history=history)


@router.post("/api/reservations/{reservation_id}/cancel", response_model=ReservationResponse)
async def cancel_reservation(
    reservation_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Cancel a BOOKED reservation. Allowed: the owner, or a cpo/admin of the
    owning tenant. Anyone else gets 404 — existence isn't leaked by a 403,
    same convention as the invoice/dispute endpoints. Only BOOKED can be
    cancelled (409 otherwise — fulfilled/expired/cancelled are terminal).
    Row-locked + status re-checked, so a cancel racing the session-start
    fulfilment (which locks the plug, then updates this row in its own
    transaction) settles exactly once.
    """
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .with_for_update()
    )
    reservation = result.scalar_one_or_none()
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found.")

    is_owner = reservation.user_id == user.id
    is_tenant_operator = (
        user.role in (UserRole.CPO, UserRole.ADMIN)
        and user.tenant_id == reservation.tenant_id
    )
    if not (is_owner or is_tenant_operator):
        raise HTTPException(status_code=404, detail="Reservation not found.")

    if reservation.status != ReservationStatus.BOOKED:
        raise HTTPException(
            status_code=409,
            detail=f"This reservation is already {reservation.status.value}.",
        )

    reservation.status = ReservationStatus.CANCELLED
    await db.commit()
    await db.refresh(reservation)

    plug_name_result = await db.execute(
        select(Plug.name).where(Plug.id == reservation.plug_id)
    )
    plug_name = plug_name_result.scalar_one_or_none()

    if not is_owner:
        # The operator pulled the slot — tell the driver, or they'll show up.
        await notify(
            reservation.user_id,
            "reservation_cancelled",
            "Reservation cancelled",
            (
                f"Your reservation on {plug_name or f'plug {reservation.plug_id}'} "
                f"({format_window(reservation.start_at, reservation.end_at)}) "
                "was cancelled by the operator."
            ),
            severity="warning",
            plug_id=reservation.plug_id,
        )

    logger.info(
        "Reservation cancelled",
        extra={
            "reservation_id": reservation.id, "cancelled_by": user.id,
            "owner_user_id": reservation.user_id, "plug_id": reservation.plug_id,
        },
    )

    return _reservation_response(
        reservation, plug_name=plug_name, current_user_id=user.id
    )


@router.get("/api/plugs/{plug_id}/reservations", response_model=list[ReservationResponse])
async def plug_reservations(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    The plug's upcoming schedule: BOOKED windows that are current or start
    within RESERVATION_MAX_ADVANCE_DAYS (default 7 days — the booking
    horizon, so this always shows everything bookable-around), soonest
    first. Visible to any user with plug access — this is how group members
    see the schedule and book around each other; each entry carries the
    holder's name and `is_mine`.
    """
    plug_result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = plug_result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug {plug_id} not found.")

    if not await user_can_access_plug(db, user.id, plug):
        raise HTTPException(
            status_code=403,
            detail="You don't have access to this plug. Join the group first.",
        )

    now = datetime.now(timezone.utc)
    expired = await expire_lapsed_reservations(db, plug_id=plug.id, now=now)
    if expired:
        await db.commit()  # read path — persist the lazy flip

    horizon = now + timedelta(days=reservation_policy.RESERVATION_MAX_ADVANCE_DAYS)
    rows = await db.execute(
        select(Reservation, User.full_name)
        .join(User, User.id == Reservation.user_id)
        .where(
            and_(
                Reservation.plug_id == plug.id,
                Reservation.status == ReservationStatus.BOOKED,
                Reservation.end_at > now,
                Reservation.start_at < horizon,
            )
        )
        .order_by(Reservation.start_at)
    )
    return [
        _reservation_response(r, plug_name=plug.name, user_name=full_name,
                              current_user_id=user.id)
        for r, full_name in rows.all()
    ]
