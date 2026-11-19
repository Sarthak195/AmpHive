"""
[Idle fee C1] Opt-in, capped per-minute fee for a car left plugged in after it
looks fully charged. Terms live on the Tariff (idle_grace_min,
idle_fee_per_min, idle_fee_cap; all 0 = off, the default) and are snapshotted
onto the session at start (services/session_start.py), so a tariff edit never
changes a running session.

What counts as idle (all of it measured, never assumed):
  - the session is ACTIVE and the F1 detector already judged the car full
    (ChargingSession.full_notified_at, durable across a restart);
  - a LIVE frame reports the relay ON with draw below CHARGE_FULL_IDLE_W;
  - telemetry is fresh: a gap between live frames longer than
    PLUG_POWER_STALE_SEC (gateway offline, plug silent) is never counted, and
    an open stretch is closed at its last idle evidence at finalize (idle_end), so a
    reaper stop for lost telemetry bills nothing for the silent tail.
A relay-ON frame at or above CHARGE_FULL_IDLE_W after the "looks full" signal
means the car is drawing again (a car-side pause can look full), so idle was
not continuous since that signal: the session is voided for the fee for good
(idle_seconds = IDLE_VOID, written once). The F1 detector and its notice fire
once per session, so a later idle stretch would have no fresh signal and no
notice; it is never billed. A relay-OFF frame only pauses the stretch.

A session whose driver opted into auto_stop_when_full never accrues anything.

State is written on transitions only (idle_started_at set/cleared,
idle_seconds banked), on the session row the telemetry path already holds
locked; there is no per-frame write. Settlement is once, in
finalize_charging_session (services/session_lifecycle.py), as its own
IDLE_FEE ledger line, capped at the snapshot cap and at what the session's
hold has left after the energy debit, then clamped to the wallet.

Known limit: once a session stops the relay is OFF and the plug reads 0 W, so
a car left on the bay AFTER the session ends cannot be detected; nothing here
claims to.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Tuple

from backend.services.money import ZERO_MONEY, to_money

logger = logging.getLogger("amphive.api")

# A car-side pause (scheduled charging, battery conditioning) can look full,
# so an enabled fee always gives at least this much grace. The tariff routes
# enforce it; the snapshot below never weakens it.
IDLE_GRACE_FLOOR_MIN = 15

IdleTerms = Tuple[int, Decimal, Decimal]  # (grace_min, per_min, cap)

# idle_seconds sentinel: the car drew again after the "looks full" signal, so
# this session never owes an idle fee (see the module docstring).
IDLE_VOID = -1


def _voided(session) -> bool:
    seconds = session.idle_seconds
    return isinstance(seconds, int) and not isinstance(seconds, bool) and seconds < 0


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _amount(value) -> Decimal:
    """A stored money value as Decimal; anything that is not a number (NULL,
    or an unset attribute) reads as 0, which means "no fee"."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        return ZERO_MONEY
    return to_money(value)


def _grace(value) -> int:
    minutes = value if isinstance(value, int) and not isinstance(value, bool) else 0
    return max(minutes, IDLE_GRACE_FLOOR_MIN)


def tariff_terms(tariff) -> Optional[IdleTerms]:
    """The tariff's idle terms, or None when the fee is off (per-minute or cap
    at 0, or no tariff at all)."""
    if tariff is None:
        return None
    per_min = _amount(tariff.idle_fee_per_min)
    cap = _amount(tariff.idle_fee_cap)
    if per_min <= ZERO_MONEY or cap <= ZERO_MONEY:
        return None
    return _grace(tariff.idle_grace_min), per_min, cap


async def resolve_idle_terms(db, plug) -> Optional[IdleTerms]:
    """Idle terms of the tariff that bills this plug (the same plug -> group
    -> tenant default chain as the rate). A failed lookup is logged and means
    no fee for that session: the fee is optional and never blocks a start."""
    from backend.services.pricing import _resolve_tariff_and_tz

    try:
        tariff, _tz = await _resolve_tariff_and_tz(db, plug)
    except Exception:
        logger.exception("Idle fee terms lookup failed; session starts with no idle fee")
        return None
    return tariff_terms(tariff)


def session_terms(session) -> Optional[IdleTerms]:
    """The session's snapshotted terms, or None when it can never accrue a fee
    (no fee configured, or the driver opted into auto-stop when full)."""
    if session.auto_stop_when_full:
        return None
    per_min = _amount(session.idle_fee_per_min)
    cap = _amount(session.idle_fee_cap)
    if per_min <= ZERO_MONEY or cap <= ZERO_MONEY:
        return None
    return _grace(session.idle_grace_min), per_min, cap


def track_idle(session, now: datetime, watts: float, relay_on: bool,
               prev_live_at: Optional[datetime]) -> None:
    """Feed one LIVE frame carrying a power reading of this ACTIVE session.
    Called under the session row lock. prev_live_at is the plug's previous
    live frame time (plugs.last_telemetry_at before this frame restamped it).
    Mutates the row only on a transition."""
    from backend.services.mqtt_manager import CHARGE_FULL_IDLE_W, PLUG_POWER_STALE_SEC

    if session.full_notified_at is None or session_terms(session) is None:
        return
    if _voided(session):
        return
    now = _utc(now)
    started = _utc(session.idle_started_at)
    prev = _utc(prev_live_at)

    # Telemetry went stale since the last live frame: bank the stretch up to
    # that frame and count nothing for the silence.
    if started is not None and prev is not None and (
        (now - prev).total_seconds() > PLUG_POWER_STALE_SEC
    ):
        session.idle_seconds = (session.idle_seconds or 0) + max(
            0, int((prev - started).total_seconds())
        )
        session.idle_started_at = started = None

    if relay_on and watts >= CHARGE_FULL_IDLE_W:
        # The car is drawing again: it was not done, so idle was not
        # continuous since the signal. Void the fee for this session (once).
        session.idle_started_at = None
        session.idle_seconds = IDLE_VOID
        return
    if not relay_on:
        # Not a reading of the car: pause.
        if started is not None:
            session.idle_seconds = (session.idle_seconds or 0) + max(
                0, int((now - started).total_seconds())
            )
            session.idle_started_at = None
        return
    if started is None:
        session.idle_started_at = now


def idle_end(session, plug) -> Optional[datetime]:
    """Where an open idle stretch ends at the stop: the EARLIER of the plug's
    last live frame (plugs.last_telemetry_at, never moved by offline-resync
    replays) and the session's last attributed frame
    (charging_sessions.last_telemetry_at, never moved by a frame that is not
    this session's, such as a relay-OFF frame after a firmware cutoff)."""
    ends = [t for t in (_utc(plug.last_telemetry_at), _utc(session.last_telemetry_at)) if t]
    return min(ends) if ends else None


def idle_total_seconds(session, last_live_at: Optional[datetime]) -> int:
    """Banked idle seconds plus the open stretch closed at the plug's last
    live frame (never at now, so a silent tail is never billed). 0 for a
    session voided by a renewed draw."""
    if _voided(session):
        return 0
    total = int(session.idle_seconds or 0)
    started = _utc(session.idle_started_at)
    last = _utc(last_live_at)
    if started is not None and last is not None and last > started:
        total += int((last - started).total_seconds())
    return total


def idle_fee_due(session, last_live_at: Optional[datetime]) -> Decimal:
    """The fee before the hold and wallet clamps: whole idle minutes past the
    grace times the per-minute rate, at most the cap. 0 when the session has
    no fee, was never judged full, drew again after that (voided), or the
    driver opted into auto-stop."""
    terms = session_terms(session)
    if terms is None or session.full_notified_at is None or _voided(session):
        return ZERO_MONEY
    grace_min, per_min, cap = terms
    billable_min = max(0, idle_total_seconds(session, last_live_at) - grace_min * 60) // 60
    return to_money(min(per_min * billable_min, cap))


def terms_text(grace_min: int, per_min: Decimal, cap: Decimal) -> str:
    """One line for notices: the same numbers the UI shows."""
    return (
        f"An idle fee of {per_min:.2f} coins per minute starts {grace_min} minutes "
        f"after charging finishes, at most {cap:.2f} coins."
    )
