"""
Plug reliability / uptime — REACHABILITY, not availability-to-charge.

No new table: `telemetry_readings` row-presence over time IS the signal. A
live gateway+plug pushes a reading roughly every ~10s idle / ~1s active
(services/telemetry_persistence.py's buffered flush of the MQTT telemetry
stream), so an hour with zero rows means the plug was unreachable for that
whole hour — gateway offline, mains power out, or a network partition. This
module never looks at PlugStatus/session state, so a plug that is 100%
reachable but permanently OCCUPIED (or in MAINTENANCE) still reads ~100%
here: it measures "can we hear from this plug", not "could a driver have
started a charge on it". Callers (GET /api/plugs/{id}/reliability) should
frame the number to users as "online" / "reachable", never "available".

Backed by services/telemetry_persistence.py's own retention knob
(TELEMETRY_RETENTION_DAYS): a fresh module-level read (not an import of that
module's constant) so this stays correct if either module's env var is
patched independently in tests.
"""
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Plug, TelemetryReading

# The default (and maximum) reporting window.
DEFAULT_WINDOW_DAYS = 7

# A plug with less than this much history can't produce a meaningful
# percentage (a brand-new plug with 20 minutes of perfect data would
# otherwise misleadingly read "100% online, 7d"). None is returned instead —
# the frontend badge quiet-fails (renders nothing) on None the same as on a
# fetch error.
MIN_SAMPLE_HOURS = 3


def _effective_window(now: datetime, plug_created: Optional[datetime], retention_days: int) -> datetime:
    """The cutoff instant `plug_uptime_7d` counts from: the later of
    (now - effective window) and the plug's own creation time. Pure/DB-free —
    split out so the window-capping rule (see plug_uptime_7d's docstring) is
    unit-testable without a database."""
    window_days = DEFAULT_WINDOW_DAYS
    if retention_days > 0:
        window_days = min(window_days, retention_days)
    window_start = now - timedelta(days=window_days)
    return max(window_start, plug_created) if plug_created else window_start


def _uptime_pct(buckets_with_data: int, elapsed_hours: float) -> float:
    """The reachability percentage for one window: buckets that saw at least
    one telemetry row, over the number of hourly buckets that could possibly
    have elapsed. Pure/DB-free — `possible_buckets` is a ceiling (a trailing
    partial hour still counts as one bucket to fill), so the ratio never
    exceeds 100% even right at the window edge."""
    possible_buckets = max(1, math.ceil(elapsed_hours))
    return round(min(100.0, (buckets_with_data / possible_buckets) * 100.0), 1)


async def plug_uptime_7d(db: AsyncSession, plug: Plug) -> dict:
    """
    One grouped query: COUNT(DISTINCT hourly bucket with >=1 telemetry row)
    over `cutoff = max(now - window, plug.created_at)`, divided by the
    number of hourly buckets actually elapsed in that span.

    Window: min(DEFAULT_WINDOW_DAYS, TELEMETRY_RETENTION_DAYS) — never asks
    about a span wider than what telemetry_persistence.py's pruning actually
    keeps around. TELEMETRY_RETENTION_DAYS=0 means retention is DISABLED
    (rows are never pruned, per that module's own convention), so a 0 does
    NOT cap the window here — only a positive value narrower than
    DEFAULT_WINDOW_DAYS does. Further capped by the plug's own age: a plug
    created 2 days ago can't have 7 days of history to report on.

    Returns:
        {"uptime_pct": float | None,   # None if under MIN_SAMPLE_HOURS old
         "sample_window_days": float,  # the EFFECTIVE window actually used
         "last_seen_at": datetime | None}
    """
    now = datetime.now(timezone.utc)
    retention_days = int(os.getenv("TELEMETRY_RETENTION_DAYS", "0"))
    cutoff = _effective_window(now, plug.created_at, retention_days)

    elapsed_hours = max(0.0, (now - cutoff).total_seconds() / 3600.0)

    if elapsed_hours < MIN_SAMPLE_HOURS:
        return {
            "uptime_pct": None,
            "sample_window_days": round(elapsed_hours / 24.0, 3),
            "last_seen_at": plug.last_telemetry_at,
        }

    bucket = func.date_trunc("hour", TelemetryReading.recorded_at)
    result = await db.execute(
        select(func.count(func.distinct(bucket))).where(
            TelemetryReading.plug_id == plug.id,
            TelemetryReading.recorded_at >= cutoff,
        )
    )
    buckets_with_data = result.scalar_one() or 0

    return {
        "uptime_pct": _uptime_pct(buckets_with_data, elapsed_hours),
        "sample_window_days": round(elapsed_hours / 24.0, 3),
        "last_seen_at": plug.last_telemetry_at,
    }
