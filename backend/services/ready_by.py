"""
"Ready by T" charge planner (F6R): pick the cheapest ONE contiguous charging
window that ends by the driver's deadline.

It plans and nothing more. The window becomes a normal scheduled queued start
(POST /api/sessions/queue with not_before, see routers/sessions.py), so the
plug is only ever switched on by the existing start paths with their wallet
hold, admission, reservation, opening-hours and current-cap checks. There is
no PAUSED state, no split window and no re-energizing: one start, one run,
stopped by its own energy or duration limit.

Pricing reuses the C3 estimate's pieces (power basis, walk_cost, the plug-hours
clamp) so the plan and the bill cannot disagree.
"""
import asyncio
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Plug, TariffSlot
from backend.services.estimate import (
    limited_energy_and_duration,
    resolve_power_basis,
    walk_cost,
)
from backend.services.money import to_money
from backend.services.plug_hours import clamp_start_duration
from backend.services.pricing import _resolve_tariff_and_tz, _zone, default_rate
from backend.services.session_start import SCHEDULE_MAX_AHEAD_HOURS

# The earliest start offered: scheduled starts must lie strictly after "now"
# when the queue endpoint validates them a moment later.
MIN_LEAD_SECONDS = 60


def _ceil_minute(dt: datetime) -> datetime:
    floored = dt.replace(second=0, microsecond=0)
    return floored if floored == dt else floored + timedelta(minutes=1)


def _refuse(code: str, message: str, status_code: int = 409, **extra) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message, **extra}
    )


def required_minutes(
    power_w: float, max_kwh: Optional[float], max_duration_s: Optional[int]
) -> int:
    """Whole minutes the limit that binds first needs at ``power_w``."""
    limit_min = math.ceil(max_duration_s / 60.0) if max_duration_s is not None else None
    _, duration_min = limited_energy_and_duration(power_w, max_kwh, limit_min)
    return max(1, math.ceil(duration_min - 1e-9))


def choose_window(
    *,
    plug,
    tz_name: Optional[str],
    slots,
    base_rate,
    power_w: float,
    duration_min: int,
    now: datetime,
    ready_by: datetime,
) -> dict:
    """PURE, DB-FREE. The cheapest start ``s`` with ``now + lead <= s`` and
    ``s + duration <= ready_by`` such that the plug is open for the whole run
    (closing time and max_session_minutes, via clamp_start_duration) and ``s``
    is inside the schedule horizon. Ties go to the earliest start. Raises the
    structured 409 ``ready_by_unreachable`` (with the earliest possible finish
    when there is one) when no window fits.

    Cost is piecewise linear in ``s`` with breakpoints on whole minutes (tariff
    slots, opening hours and the whole-minute duration), so scanning whole
    minutes finds the true minimum."""
    tz = _zone(tz_name)
    run = timedelta(minutes=duration_min)
    lo = _ceil_minute(now + timedelta(seconds=MIN_LEAD_SECONDS))
    horizon = now + timedelta(hours=SCHEDULE_MAX_AHEAD_HOURS)
    latest = ready_by.replace(second=0, microsecond=0) - run
    run_s = duration_min * 60

    def feasible(s: datetime) -> bool:
        try:
            cap = clamp_start_duration(plug, tz_name, s, None)
        except ValueError:
            return False
        return cap is None or cap >= run_s

    best = None  # (cost, start)
    earliest_ok = None
    s = lo
    while s <= horizon:  # a schedule may start no later than this
        if feasible(s):
            if earliest_ok is None:
                earliest_ok = s
            if s > latest:
                break  # past the deadline; only the earliest-ok start was still needed
            cost, changes = walk_cost(slots, base_rate, tz, s, duration_min, power_w)
            if best is None or cost < best[0]:
                best = (cost, s, changes)
            if not slots:
                break  # flat tariff: every feasible start costs the same, earliest wins
        elif s > latest and earliest_ok is not None:
            break
        s += timedelta(minutes=1)

    if best is None:
        if earliest_ok is None:
            raise _refuse(
                "ready_by_unreachable",
                "This charger has no opening long enough for that charge in the next 24 hours.",
            )
        finish = earliest_ok + run
        raise _refuse(
            "ready_by_unreachable",
            "That is too soon for this charge. The earliest it can be ready is "
            f"{_to_local_hhmm(finish, tz)}.",
            earliest_finish_at=finish.isoformat(),
        )
    cost, start, changes = best
    return {
        "start": start,
        "finish": start + run,
        "cost": cost,
        "rate_changes": changes,
    }


def _to_local_hhmm(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


async def plan_ready_by(
    db: AsyncSession,
    plug: Plug,
    user_id: int,
    ready_by: datetime,
    max_kwh: Optional[float],
    max_duration_s: Optional[int],
) -> dict:
    """Plan a ready-by-T charge. Returns the plan dict the routes serialize;
    ``max_duration_seconds`` is the duration limit the scheduled row must carry
    (the driver's own, tightened to the deadline so the run ends by T even if
    the car draws less than the estimate)."""
    if max_kwh is None and max_duration_s is None:
        raise _refuse(
            "ready_by_needs_limit",
            "Set an energy or time target for a ready-by charge.",
            status_code=422,
        )
    now = datetime.now(timezone.utc)
    ready_by = ready_by.astimezone(timezone.utc)
    if not (now < ready_by <= now + timedelta(hours=SCHEDULE_MAX_AHEAD_HOURS)):
        raise _refuse(
            "ready_by_out_of_range",
            "Pick a ready-by time within the next 24 hours.",
            status_code=400,
        )

    power_w, basis = await resolve_power_basis(db, plug, user_id, now)
    tariff, tz_name = await _resolve_tariff_and_tz(db, plug)
    if tariff is None:
        slots, base = [], default_rate()
    else:
        slots = (
            await db.execute(select(TariffSlot).where(TariffSlot.tariff_id == tariff.id))
        ).scalars().all()
        base = to_money(tariff.price_per_kwh)

    duration_min = required_minutes(power_w, max_kwh, max_duration_s)
    # A whole-minute scan over up to 24 h is pure CPU: keep it off the event loop.
    chosen = await asyncio.to_thread(
        choose_window,
        plug=plug, tz_name=tz_name, slots=slots, base_rate=base, power_w=power_w,
        duration_min=duration_min, now=now, ready_by=ready_by,
    )
    start = chosen["start"]
    until_deadline = int((ready_by - start).total_seconds())
    duration_cap = until_deadline if max_duration_s is None else min(max_duration_s, until_deadline)
    return {
        "not_before": start,
        "max_duration_seconds": duration_cap,
        "finish_at": chosen["finish"],
        "duration_min": duration_min,
        "energy_kwh": round(power_w / 1000.0 * duration_min / 60.0, 2),
        "cost_coins": str(chosen["cost"]),
        "rate_changes": [
            {"at": at.isoformat(), "rate": str(rate)} for at, rate in chosen["rate_changes"]
        ],
        "basis": basis,
        "power_w": int(round(power_w)),
        "ready_by": ready_by,
        "tz_name": tz_name,
        "is_estimate": True,
    }
