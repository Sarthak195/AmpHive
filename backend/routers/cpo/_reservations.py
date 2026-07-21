"""
CPO Plug Reservations routes — split out of the original monolithic cpo.py
(2026-07-21 package split, TD#7 follow-up). Booking/cancelling happens on
the driver side (routers/reservations.py) — this is the CPO's tenant-scoped
view of the schedule (plus cancel via the shared
POST /api/reservations/{id}/cancel, which admits the owning tenant's
cpo/admin).
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import Plug, Reservation, ReservationStatus, User
from backend.services.rbac import require_role
from backend.services.reservations import expire_lapsed_reservations

from ._common import _require_tenant_id

router = APIRouter()


@router.get("/api/cpo/reservations")
async def cpo_list_reservations(
    user: User = Depends(require_role("cpo", "admin")),
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
    if expired:
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
        conditions.append(Reservation.status == ReservationStatus.BOOKED)
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
                "user_email": user_email,
                "user_name": user_name,
                "start_at": r.start_at.isoformat(),
                "end_at": r.end_at.isoformat(),
                "status": r.status.value,
                "session_id": r.session_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r, plug_name, user_email, user_name in rows.all()
        ],
    }
