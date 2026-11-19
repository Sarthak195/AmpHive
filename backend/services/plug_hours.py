"""
[Plug hours] Weekly opening hours, max session length, amenity tags and an
access note per plug (alembic 0049_plug_hours). PURE and DB-FREE: every call
site (session start, the queued-start sweep, reservation booking, the plug
read routes) passes in the plug and the tenant timezone it already has.

Opening hours reuse the TariffSlot window semantics (services/pricing.py):
each window is {"days": Mon=bit0..Sun=bit6 mask, "start": minute, "end":
minute}, a half-open [start, end) minute-of-day range in the tenant's local
wall-clock. A window that crosses midnight is stored as TWO windows
([start, 1440) on its days, [0, end) on the following days), so resolution
never spans a day. NULL hours means open 24x7 (the pre-0049 behaviour).

Blocking is the safe direction: a closed plug refuses new starts and new
bookings, and a start near closing time is clamped so the firmware's own
duration limit ends it by the close. A session that is already running is
never stopped at closing time (no new remote relay-off trigger).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.services.pricing import _to_local, _zone, slot_overlaps

# Fixed amenity allowlist (no free-form tags reach the driver UI).
AMENITY_TAGS = ("restroom", "cafe", "wifi", "covered", "lit", "cctv", "accessible")
ACCESS_NOTE_MAX = 280
# Upper bound on stored windows (seven days, a few windows each).
MAX_WINDOWS = 28
# A start with less than this left before closing is refused as closed: a
# near-zero duration limit would start the relay only to stop it at once, and
# 0 is never a safe value for the firmware watchdog (services/mqtt_manager.py
# firmware_duration).
MIN_START_SECONDS = 60
# How far ahead open_until / next_open scan before calling the plug "always
# open" / "never opens".
_SCAN_DAYS = 8


def _rotate_days(mask: int) -> int:
    """The weekday mask shifted to the following day (Sun wraps to Mon)."""
    return ((mask << 1) | (mask >> 6)) & 127


def normalize_hours(windows) -> Optional[list]:
    """
    Validate CPO input into the stored form. Each item is a mapping with
    integer ``days`` (1..127), ``start`` (0..1439) and ``end`` (0..1440,
    0 read as midnight = 1440). ``end`` before ``start`` crosses midnight and
    is split into two windows. Returns None (24x7) for None or an empty list:
    an ad-hoc closure is maintenance mode, not an empty schedule.
    Raises ValueError on a malformed, zero-length or overlapping window.
    """
    if not windows:
        return None
    out: list = []
    for w in windows:
        days, start, end = int(w["days"]), int(w["start"]), int(w["end"])
        if not 1 <= days <= 127:
            raise ValueError("days must select at least one weekday.")
        if not 0 <= start <= 1439 or not 0 <= end <= 1440:
            raise ValueError("Times must be within the day.")
        if end == 0:
            end = 1440
        if end == start:
            raise ValueError("A window must not start and end at the same time.")
        if end > start:
            out.append({"days": days, "start": start, "end": end})
        else:
            out.append({"days": days, "start": start, "end": 1440})
            if end > 0:
                out.append({"days": _rotate_days(days), "start": 0, "end": end})
    if len(out) > MAX_WINDOWS:
        raise ValueError(f"At most {MAX_WINDOWS} windows.")
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            if slot_overlaps(a["start"], a["end"], a["days"], b["start"], b["end"], b["days"]):
                raise ValueError("Opening-hours windows must not overlap.")
    return out


def normalize_amenities(tags) -> Optional[list]:
    """Allowlisted, de-duplicated amenity tags in allowlist order; None when
    empty. Raises ValueError on an unknown tag."""
    if not tags:
        return None
    unknown = [t for t in tags if t not in AMENITY_TAGS]
    if unknown:
        raise ValueError(f"Unknown amenity: {unknown[0]}. Allowed: {', '.join(AMENITY_TAGS)}.")
    picked = [t for t in AMENITY_TAGS if t in set(tags)]
    return picked or None


# --- Plug field accessors (a malformed or non-list value reads as unset) ---

def hours_of(plug) -> Optional[list]:
    value = getattr(plug, "opening_hours", None)
    return value if isinstance(value, list) and value else None


def max_minutes_of(plug) -> Optional[int]:
    value = getattr(plug, "max_session_minutes", None)
    return value if isinstance(value, int) and value > 0 else None


def amenities_of(plug) -> Optional[list]:
    value = getattr(plug, "amenities", None)
    return value if isinstance(value, list) and value else None


def access_note_of(plug) -> Optional[str]:
    value = getattr(plug, "access_note", None)
    return value if isinstance(value, str) and value else None


# --- Pure resolution on a tz-aware LOCAL datetime ---

def _covering(windows, at_local: datetime):
    weekday = at_local.weekday()
    minute = at_local.hour * 60 + at_local.minute
    for w in windows:
        if (int(w["days"]) >> weekday) & 1 and int(w["start"]) <= minute < int(w["end"]):
            return w
    return None


def _day_start(at_local: datetime) -> datetime:
    return at_local.replace(hour=0, minute=0, second=0, microsecond=0)


def is_open(windows, at_local: datetime) -> bool:
    """Open at ``at_local``? None/empty hours = always open."""
    if not windows:
        return True
    return _covering(windows, at_local) is not None


def open_until(windows, at_local: datetime) -> Optional[datetime]:
    """The local instant the plug next closes, following touching windows
    across midnight and same-day edges (22:00-24:00 then 00:00-06:00 closes
    at 06:00). ``at_local`` itself when closed now; None when it stays open
    for the whole scan (24x7, or no hours at all)."""
    if not windows:
        return None
    cur = at_local
    limit = at_local + timedelta(days=_SCAN_DAYS)
    while cur < limit:
        w = _covering(windows, cur)
        if w is None:
            return cur
        cur = _day_start(cur) + timedelta(minutes=int(w["end"]))
    return None


def next_open(windows, at_local: datetime) -> Optional[datetime]:
    """The local instant a CLOSED plug next opens (None when no window ever
    opens within the scan, or when there are no hours at all)."""
    if not windows:
        return None
    minute = at_local.hour * 60 + at_local.minute
    day0 = _day_start(at_local)
    for delta in range(_SCAN_DAYS):
        day = day0 + timedelta(days=delta)
        starts = [
            int(w["start"]) for w in windows
            if (int(w["days"]) >> day.weekday()) & 1 and (delta > 0 or int(w["start"]) > minute)
        ]
        if starts:
            return day + timedelta(minutes=min(starts))
    return None


def hours_status(plug, tz_name, at: datetime):
    """``(open_now, changes_at)`` for the driver UI: changes_at is the next
    close when open, the next open when closed (ISO strings are built by the
    caller). ``(None, None)`` for a 24x7 plug."""
    windows = hours_of(plug)
    if windows is None:
        return None, None
    at_local = _to_local(at, _zone(tz_name))
    if is_open(windows, at_local):
        return True, open_until(windows, at_local)
    return False, next_open(windows, at_local)


def window_is_open(plug, tz_name, start_at: datetime, end_at: datetime) -> bool:
    """[Reservations] True when the plug is open for the whole booked
    ``[start_at, end_at)``. Always True for a 24x7 plug."""
    windows = hours_of(plug)
    if windows is None:
        return True
    start_local = _to_local(start_at, _zone(tz_name))
    if not is_open(windows, start_local):
        return False
    close = open_until(windows, start_local)
    return close is None or close.astimezone(timezone.utc) >= end_at


def clamp_start_duration(
    plug, tz_name, now: datetime, max_duration_seconds: Optional[int]
) -> Optional[int]:
    """
    [Session start] The duration limit a start at ``now`` may run with: the
    driver's own limit, shortened so it ends by the current window's closing
    time and by the plug's max_session_minutes. Returns the input unchanged
    for a 24x7 plug with no max length. Raises ValueError when the plug is
    closed now (or closes within MIN_START_SECONDS).
    """
    limits = [max_duration_seconds] if max_duration_seconds is not None else []
    windows = hours_of(plug)
    if windows is not None:
        now_local = _to_local(now, _zone(tz_name))
        if not is_open(windows, now_local):
            raise ValueError("closed")
        close = open_until(windows, now_local)
        if close is not None:
            # UTC subtraction: two aware datetimes in one ZoneInfo subtract as
            # wall-clock, which is wrong across a DST change.
            remaining = int((close.astimezone(timezone.utc) - now).total_seconds())
            if remaining < MIN_START_SECONDS:
                raise ValueError("closed")
            limits.append(remaining)
    minutes = max_minutes_of(plug)
    if minutes is not None:
        limits.append(minutes * 60)
    return min(limits) if limits else None


def closed_detail(plug, tz_name, now: datetime) -> str:
    """The 409 detail for a start refused by opening hours."""
    open_now, changes_at = hours_status(plug, tz_name, now)
    if open_now:
        return "This charger is closing now. Try again when it reopens."
    if changes_at is not None:
        return f"This charger is closed right now. It opens at {changes_at:%H:%M}."
    return "This charger is closed right now."


def driver_fields(plug, tz_name, now: datetime) -> dict:
    """The PlugResponse fields for the driver plug card and detail.
    hours_changes_at is the next close (open) or next open (closed) as an ISO
    instant, rendered by the client in tenant_timezone."""
    open_now, changes_at = hours_status(plug, tz_name, now)
    return {
        "opening_hours": hours_of(plug),
        "open_now": open_now,
        "hours_changes_at": changes_at.isoformat() if changes_at is not None else None,
        "max_session_minutes": max_minutes_of(plug),
        "amenities": amenities_of(plug),
        "access_note": access_note_of(plug),
    }


def duration_cap(plug, tz_name, started_at: datetime) -> Optional[int]:
    """[Session limits PATCH] The longest max_duration_seconds a session that
    started at ``started_at`` may carry: by the closing time of the window it
    started in and by max_session_minutes, the same bounds the start clamp
    applied. None when neither applies, so a driver can not raise a running
    session's limit past what the start would have allowed."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    try:
        return clamp_start_duration(plug, tz_name, started_at, None)
    except ValueError:
        # Hours edited since the start: the max length still applies.
        minutes = max_minutes_of(plug)
        return minutes * 60 if minutes is not None else None
