"""
[Charge complete] In-memory "the car looks fully charged" detector.

Fed by MQTTTelemetryMixin._persist_telemetry with REAL power samples only: a
live frame (never an offline-resync replay) that carries a `watts` value, is
attributed to the plug's ACTIVE session, and reports the relay ON. A frame with
the relay OFF is not a reading of the car, so it only restarts the idle timer.
Missing frames are never zero-filled: the timer advances only on the
timestamps of samples that actually arrived.

Rule (thresholds live in services/mqtt_manager.py, read at call time so tests
can monkeypatch them like the other telemetry knobs):
  1. the session has drawn at least CHARGE_FULL_ACTIVE_W at least once (its
     persisted peak_power_w, so a backend restart does not forget it);
  2. the sample is at least CHARGE_FULL_GRACE_MIN after the session started;
  3. power then stays below CHARGE_FULL_IDLE_W continuously for
     CHARGE_FULL_MINUTES, measured between sample timestamps. Any sample at or
     above the idle threshold restarts the timer, and so does a gap between
     samples longer than the reaper's stale window (the gateway was offline,
     so nothing is known about that stretch).
It fires once per session. State is keyed by plug and replaced when a new
session id shows up on that plug, so it stays bounded with no cleanup.

State is in memory only. A backend restart clears it: the idle timer starts
over (detection is only delayed), and a session that had already fired can
fire once more, which repeats one notification at most (an auto-stopped
session is no longer ACTIVE, so it is never stopped twice).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

# Stop reason passed to finalize_charging_session. Must not contain the
# substrings finalize keys its notification title on ("exhausted",
# "limit reached", "reserved", "telemetry lost") or a "]" (the ledger
# description wraps it in [...] and the session-detail route parses it back).
CHARGE_COMPLETE_REASON = "auto-stopped: charge complete"


@dataclass
class _PlugState:
    session_id: int
    idle_since: Optional[datetime] = None
    last_at: Optional[datetime] = None
    fired: bool = False


class ChargeFullDetector:
    """Per-plug low-draw state machine. Synchronous and DB-free: the caller
    owns the DB, notifications and the stop."""

    def __init__(self) -> None:
        self._plugs: Dict[int, _PlugState] = {}

    def _state(self, plug_id: int, session_id: int) -> _PlugState:
        st = self._plugs.get(plug_id)
        if st is None or st.session_id != session_id:
            st = _PlugState(session_id=session_id)
            self._plugs[plug_id] = st
        return st

    def observe(self, plug_id: int, session_id: int, at: datetime, watts: float,
                peak_w: float, started_at: Optional[datetime]) -> bool:
        """Feed one real power sample. True exactly once per session: on the
        sample that completes CHARGE_FULL_MINUTES of continuous low draw."""
        from backend.services.mqtt_manager import (
            CHARGE_FULL_ACTIVE_W,
            CHARGE_FULL_GRACE_MIN,
            CHARGE_FULL_IDLE_W,
            CHARGE_FULL_MINUTES,
        )
        from backend.services.session_reaper import STALE_TIMEOUT_SEC

        st = self._state(plug_id, session_id)
        if st.fired:
            return False
        if st.last_at is not None and (at - st.last_at).total_seconds() > STALE_TIMEOUT_SEC:
            st.idle_since = None
        st.last_at = at

        if watts >= CHARGE_FULL_IDLE_W:
            st.idle_since = None
            return False
        if max(peak_w or 0.0, watts) < CHARGE_FULL_ACTIVE_W:
            return False
        if started_at is not None:
            started = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
            if at < started + timedelta(minutes=CHARGE_FULL_GRACE_MIN):
                return False

        if st.idle_since is None:
            st.idle_since = at
        if (at - st.idle_since).total_seconds() >= CHARGE_FULL_MINUTES * 60:
            st.fired = True
            return True
        return False

    def interrupt(self, plug_id: int, session_id: int) -> None:
        """A live frame of this session that is not a reading of the car (relay
        reported OFF): restart the idle timer."""
        self._state(plug_id, session_id).idle_since = None
