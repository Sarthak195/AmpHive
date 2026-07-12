"""
Reservation policy + shared helpers (feat/reservations, 2026-07-12).

Group members book a future window on a charger (routers/reservations.py);
during that window only the holder can start a session (the gate in
routers/sessions.py start_charging_session). Reservations are FREE in v1 —
no coin hold, no money movement anywhere in the lifecycle.

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
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    ChargerGroup, GroupMembership, Plug, Reservation, ReservationStatus,
)

# Longest bookable window, in hours.
RESERVATION_MAX_HOURS = float(os.getenv("RESERVATION_MAX_HOURS") or "4")
# How far ahead a window may START, in days.
RESERVATION_MAX_ADVANCE_DAYS = float(os.getenv("RESERVATION_MAX_ADVANCE_DAYS") or "7")
# Cap of BOOKED-with-end_at-in-the-future reservations per user (409 past it).
MAX_UPCOMING_RESERVATIONS_PER_USER = int(os.getenv("MAX_UPCOMING_RESERVATIONS_PER_USER") or "2")
# No-show grace: a BOOKED reservation still unfulfilled this many minutes
# after start_at lazily flips to EXPIRED and stops blocking anyone.
RESERVATION_NO_SHOW_GRACE_MIN = float(os.getenv("RESERVATION_NO_SHOW_GRACE_MIN") or "15")

# Not env-tunable: the floor on a booking's length, and the small tolerance
# that lets "book starting now" survive clock skew / form-submit latency.
RESERVATION_MIN_MINUTES = 15
RESERVATION_START_GRACE_MIN = 2


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
        Reservation.status == ReservationStatus.BOOKED,
        or_(
            Reservation.start_at <= now - timedelta(minutes=RESERVATION_NO_SHOW_GRACE_MIN),
            Reservation.end_at <= now,
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

    result = await db.execute(
        update(Reservation)
        .where(and_(*conditions))
        .values(status=ReservationStatus.EXPIRED)
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
