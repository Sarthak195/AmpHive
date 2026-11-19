"""
CPO Plug Reservations routes — split out of the original monolithic cpo.py
(2026-07-21 package split, TD#7 follow-up). Booking/cancelling happens on
the driver side (routers/reservations.py) — this is the CPO's tenant-scoped
view of the schedule (plus cancel via the shared
POST /api/reservations/{id}/cancel, which admits the owning tenant's
cpo/admin).

[C8] On a plug in approval mode a booking arrives PENDING; the tenant's
cpo/admin answers it here with approve (-> BOOKED) or decline (-> DECLINED),
and the driver is notified either way. Both lock the plug row and then the
reservation row (the cancel path's order), so an approve racing the driver's
own cancel settles exactly once. A cpo_viewer never reaches them: the global
read-only guard answers 403 to every non-GET.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import Plug, Reservation, ReservationStatus, Tenant, User
from backend.services.notifications import notify
from backend.services.plug_hours import hours_of, window_is_open
from backend.services.reservations import (
    HOLD_STATUSES,
    expire_lapsed_reservations,
    format_window,
)

from ._common import (
    _require_tenant_id,
    is_cpo_viewer,
    redact_for,
    redact_name_for,
    require_cpo_tenant,
)

router = APIRouter()


@router.get("/api/cpo/reservations")
async def cpo_list_reservations(
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
    upcoming_only: bool = False,
    limit: int = 50,
    offset: int = 0,
):
    """List the tenant's plug reservations, newest window first. Tenant-
    scoped; optional `status` filter (booked/cancelled/fulfilled/expired —
    400 otherwise, matching the CpoPlugUpdateRequest.status validation
    convention) and `upcoming_only` (BOOKED with end_at in the future).
    Lazily expires lapsed holds first so the operator never sees a no-show
    still shown as BOOKED.

    [redesign/ui-v3 contract §4] Paginated: {total, items} with the house
    limit/offset params (limit capped at 200); `total` counts the full
    filtered set, not the page."""
    tenant_id = _require_tenant_id(user)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    now = datetime.now(timezone.utc)

    expired = await expire_lapsed_reservations(db, tenant_id=tenant_id, now=now)
    # [F10] A read-only viewer still sees lapsed holds as expired, but its
    # GET never persists the flip (the request session rolls back on close).
    if expired and not is_cpo_viewer(user):
        await db.commit()  # read path — persist the lazy flip

    conditions = [Reservation.tenant_id == tenant_id]
    if status is not None:
        try:
            conditions.append(Reservation.status == ReservationStatus(status))
        except ValueError:
            valid = ", ".join(s.value for s in ReservationStatus)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{status}'. Must be one of: {valid}.",
            )
    if upcoming_only:
        conditions.append(Reservation.status.in_(HOLD_STATUSES))
        conditions.append(Reservation.end_at > now)

    total = (
        await db.execute(
            select(func.count(Reservation.id)).where(and_(*conditions))
        )
    ).scalar() or 0

    rows = await db.execute(
        select(Reservation, Plug.name, User.email, User.full_name)
        .join(Plug, Plug.id == Reservation.plug_id)
        .join(User, User.id == Reservation.user_id)
        .where(and_(*conditions))
        .order_by(Reservation.start_at.desc(), Reservation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [
            {
                "id": r.id,
                "plug_id": r.plug_id,
                "plug_name": plug_name,
                "user_id": r.user_id,
                "user_email": redact_for(user, user_email),
                "user_name": redact_name_for(user, user_name),
                "start_at": r.start_at.isoformat(),
                "end_at": r.end_at.isoformat(),
                "status": r.status.value,
                "session_id": r.session_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r, plug_name, user_email, user_name in rows.all()
        ],
    }


async def _answer_request(
    db: AsyncSession, user: User, reservation_id: int, approve: bool
) -> dict:
    """Approve or decline one of the tenant's PENDING requests. 404 for a
    missing or other-tenant reservation (existence not leaked); 409 once it is
    no longer PENDING (already answered, withdrawn, or expired at start_at)."""
    tenant_id = _require_tenant_id(user)
    plug_id = (await db.execute(
        select(Reservation.plug_id).where(
            and_(Reservation.id == reservation_id, Reservation.tenant_id == tenant_id)
        )
    )).scalar_one_or_none()
    if plug_id is None:
        raise HTTPException(status_code=404, detail="Reservation not found.")

    # Plug row lock first, then the reservation row: the same order as the
    # driver cancel, booking and session-start paths. The entity is loaded for
    # the first time under its own lock, so its status is never stale.
    plug = (await db.execute(
        select(Plug).where(Plug.id == plug_id).with_for_update()
    )).scalar_one()
    plug_name = plug.name
    await expire_lapsed_reservations(db, plug_id=plug_id)
    reservation = (await db.execute(
        select(Reservation).where(Reservation.id == reservation_id).with_for_update()
    )).scalar_one()
    if reservation.status != ReservationStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"This request is already {reservation.status.value}.",
        )

    # [C8 + C4] Approval books the slot, so it obeys the plug's opening hours
    # as a direct booking does: hours narrowed after the request was made
    # refuse the approval (the request stays PENDING; decline it instead).
    if approve and hours_of(plug) is not None:
        tz_name = (await db.execute(
            select(Tenant.timezone).where(Tenant.id == tenant_id)
        )).scalar_one_or_none()
        if not window_is_open(plug, tz_name, reservation.start_at, reservation.end_at):
            raise HTTPException(
                status_code=409,
                detail="The charger is closed during part of this slot under its current opening hours. Decline the request instead.",
            )

    reservation.status = ReservationStatus.BOOKED if approve else ReservationStatus.DECLINED
    await db.commit()

    window = format_window(reservation.start_at, reservation.end_at)
    if approve:
        await notify(
            reservation.user_id,
            "reservation_approved",
            "Reservation approved",
            f"{plug_name} is booked for you: {window}.",
            plug_id=plug_id,
        )
    else:
        await notify(
            reservation.user_id,
            "reservation_declined",
            "Reservation declined",
            f"The host declined your request for {plug_name} ({window}).",
            severity="warning",
            plug_id=plug_id,
        )
    return {
        "id": reservation.id,
        "plug_id": plug_id,
        "status": reservation.status.value,
        "start_at": reservation.start_at.isoformat(),
        "end_at": reservation.end_at.isoformat(),
    }


@router.post("/api/cpo/reservations/{reservation_id}/approve")
async def cpo_approve_reservation(
    reservation_id: int,
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
):
    """[C8] Approve a PENDING request: it becomes an ordinary BOOKED hold."""
    return await _answer_request(db, user, reservation_id, approve=True)


@router.post("/api/cpo/reservations/{reservation_id}/decline")
async def cpo_decline_reservation(
    reservation_id: int,
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
):
    """[C8] Decline a PENDING request: the slot is released at once."""
    return await _answer_request(db, user, reservation_id, approve=False)
