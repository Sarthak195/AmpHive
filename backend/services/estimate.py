"""
Pre-start charge estimate (C3): DISPLAY ONLY.

Answers "what would this charge cost and when would it finish" for the
charge-setup modal. It reads the tariff and the caller's own history and
writes nothing; it must never feed the wallet hold, admission, the frozen
rate or billing (those stay in services/session_start.py).

The cost walk reuses billing's own pieces so the preview and the bill cannot
drift apart: the tariff chain and slot resolution from services/pricing.py
(_resolve_tariff_and_tz, _slot_rate_and_bound, the core of resolve_rate_window)
and services/billing.py close_out_segment / session_cost run on a throwaway
session-shaped object, so a charge crossing a tariff change is priced per
window exactly as a real session is. Tariffs are GST-inclusive and billing
adds no fixed fee (services/invoices.py: coins_spent is the final amount), so
neither is added here.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    ChargingSession,
    Plug,
    SessionStatus,
    TariffSlot,
)
from backend.services.billing import close_out_segment, session_cost
from backend.services.caps import effective_plug_cap
from backend.services.money import ZERO_MONEY, to_money
from backend.services.plug_hours import (
    clamp_start_duration,
    closed_detail,
    hours_of,
    max_minutes_of,
)
from backend.services.pricing import (
    _resolve_tariff_and_tz,
    _slot_rate_and_bound,
    _to_local,
    _zone,
    default_rate,
)

# The mains voltage the telemetry layer assumes when a frame carries none.
NOMINAL_VOLTAGE_V = 230.0
# History window and minimum length for the "your last session here" basis.
HISTORY_DAYS = 30
MIN_HISTORY_SECONDS = 5 * 60
# A walk crosses a handful of slot boundaries per day; this only stops a
# malformed tariff from looping.
_MAX_WINDOWS = 200
# The longest session the API allows (max_duration_min le=1440) and the start
# path's admission horizon: an estimate never looks further than this, so a
# low-power history with a big kWh limit cannot show a year-long finish or a
# cost from a truncated walk.
MAX_ESTIMATE_MINUTES = 1440.0


def rated_power_w(plug: Plug) -> float:
    """The plug's rated draw: the current cap the start path applies
    (effective_plug_cap: max_current_a or the default cap) at nominal volts."""
    return effective_plug_cap(plug) * NOMINAL_VOLTAGE_V


async def resolve_power_basis(
    db: AsyncSession, plug: Plug, user_id: int, now: datetime
) -> Tuple[float, str]:
    """(power_w, basis). The caller's average power over their most recent
    completed session on this plug (last 30 days, at least 5 minutes, some
    energy), never above the rated power; else the rated power."""
    rated = rated_power_w(plug)
    row = (
        await db.execute(
            select(
                ChargingSession.energy_kwh,
                ChargingSession.started_at,
                ChargingSession.ended_at,
            )
            .where(
                ChargingSession.user_id == user_id,
                ChargingSession.plug_id == plug.id,
                ChargingSession.status == SessionStatus.COMPLETED,
                ChargingSession.ended_at.is_not(None),
                ChargingSession.ended_at >= now - timedelta(days=HISTORY_DAYS),
            )
            .order_by(ChargingSession.ended_at.desc())
            .limit(1)
        )
    ).first()
    if row is not None and row.ended_at is not None and row.started_at is not None:
        seconds = (row.ended_at - row.started_at).total_seconds()
        energy = float(row.energy_kwh or 0.0)
        if seconds >= MIN_HISTORY_SECONDS and energy > 0:
            return min(energy * 3_600_000.0 / seconds, rated), "recent_session"
    return rated, "rated_power"


def limited_energy_and_duration(
    power_w: float, max_kwh: Optional[float], max_duration_min: Optional[int],
    hours_cap_min: Optional[float] = None,
) -> Tuple[float, float]:
    """(energy_kwh, duration_min) for the limit that binds first, capped at
    24 h (the energy returned is what is delivered within the cap).
    ``hours_cap_min`` is the plug-hours clamp (closing time and
    max_session_minutes) the start path would apply, so the estimate ends
    where the real session would."""
    kw = power_w / 1000.0
    candidates = []
    if max_kwh is not None:
        candidates.append((max_kwh * 60.0) / kw)
    if max_duration_min is not None:
        candidates.append(float(max_duration_min))
    if hours_cap_min is not None:
        candidates.append(float(hours_cap_min))
    duration_min = min(min(candidates), MAX_ESTIMATE_MINUTES)
    return kw * duration_min / 60.0, duration_min


def walk_cost(
    slots,
    base_rate: Decimal,
    tz,
    start_at: datetime,
    duration_min: float,
    power_w: float,
) -> Tuple[Decimal, List[Tuple[datetime, Decimal]]]:
    """PURE, DB-FREE. Price a constant-power charge window by window.

    Returns (cost, rate_changes) where rate_changes lists (instant, new_rate)
    for each mid-charge change. Energy accrued under an earlier rate stays
    priced at it, via billing's close_out_segment / session_cost.
    """
    kw = power_w / 1000.0
    finish = start_at + timedelta(minutes=duration_min)
    sim = SimpleNamespace(
        rate_coins_per_kwh=None,
        rate_segment_start_kwh=0.0,
        settled_cost_coins=ZERO_MONEY,
        rate_valid_until=None,
    )
    changes: List[Tuple[datetime, Decimal]] = []
    t = start_at
    for _ in range(_MAX_WINDOWS):
        slot_rate, boundary = _slot_rate_and_bound(slots, _to_local(t, tz))
        rate = slot_rate if slot_rate is not None else base_rate
        if sim.rate_coins_per_kwh is None:
            sim.rate_coins_per_kwh = rate
        elif rate != sim.rate_coins_per_kwh:
            at_energy = kw * (t - start_at).total_seconds() / 3600.0
            close_out_segment(sim, rate, at_energy, boundary)
            changes.append((t, rate))
        if boundary is None or boundary >= finish or boundary <= t:
            break
        t = boundary
    energy = kw * duration_min / 60.0
    return session_cost(sim, energy), changes


async def estimate_charge(
    db: AsyncSession,
    plug: Plug,
    user_id: int,
    max_kwh: Optional[float],
    max_duration_min: Optional[int],
    start_at: Optional[datetime] = None,
) -> dict:
    now = datetime.now(timezone.utc)
    start = start_at if start_at is not None else now
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    power_w, basis = await resolve_power_basis(db, plug, user_id, now)
    tariff, tz_name = await _resolve_tariff_and_tz(db, plug)

    # [C4 plug hours] Apply the same clamp begin_active_session does, at the
    # estimated start: a plug closed then would refuse the start (409 here
    # too), and an open one ends by its closing time and max_session_minutes.
    hours_cap_min: Optional[float] = None
    if hours_of(plug) is not None or max_minutes_of(plug) is not None:
        try:
            cap_s = clamp_start_duration(plug, tz_name, start, None)
        except ValueError:
            raise HTTPException(status_code=409, detail=closed_detail(plug, tz_name, start))
        if cap_s is not None:
            hours_cap_min = cap_s / 60.0
    energy_kwh, duration_min = limited_energy_and_duration(
        power_w, max_kwh, max_duration_min, hours_cap_min
    )

    if tariff is None:
        slots, base = [], default_rate()
    else:
        slots = (
            await db.execute(select(TariffSlot).where(TariffSlot.tariff_id == tariff.id))
        ).scalars().all()
        base = to_money(tariff.price_per_kwh)
    cost, changes = walk_cost(slots, base, _zone(tz_name), start, duration_min, power_w)

    return {
        "energy_kwh": round(energy_kwh, 2),
        "duration_min": int(round(duration_min)),
        "finish_at": (start + timedelta(minutes=duration_min)).isoformat(),
        "cost_coins": str(cost),
        "rate_changes": [{"at": at.isoformat(), "rate": str(rate)} for at, rate in changes],
        "basis": basis,
        "power_w": int(round(power_w)),
        "is_estimate": True,
    }
