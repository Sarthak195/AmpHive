"""
[Notification prefs C7] In-memory detectors for two suppressible session
notices, fed beside the charge-complete detector (charge_full.py) from
MQTTTelemetryMixin._persist_telemetry with REAL live samples only: a live
frame (never an offline-resync replay) that carries a `watts` value, belongs
to the plug's ACTIVE session and reports the relay ON.

nearly_done: once per session, on the first sample where the session's
  energy reaches NEARLY_DONE_FRACTION of its max_kwh but is still below it (a
  frame that already reached max_kwh is the limit stop's receipt instead).

power_dropped: once per session. After the session has drawn at least
  CHARGE_FULL_ACTIVE_W (its persisted peak_power_w), live power stays below
  POWER_DROP_FRACTION of that peak for POWER_DROP_MINUTES of sample time
  while staying at or above CHARGE_FULL_IDLE_W. The timer restarts on any
  sample at or above the drop threshold, on any sample below the idle
  threshold (that is charge_full's territory, so a drop straight to idle never
  produces this notice), on a relay-OFF frame, and on a gap between samples
  longer than the reaper's stale window. Missing frames are never zero-filled.

State is keyed by plug and replaced when a new session id shows up on that
plug, so it stays bounded with no cleanup. The timers are in memory only (a
restart restarts the power-drop timer). The once-per-session dedupe is
durable: the caller claims ChargingSession.nearly_done_notified_at /
power_dropped_notified_at (migration 0051) when a detector fires and skips a
notice the row already records, so a restart neither repeats nor loses one.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

NEARLY_DONE_FRACTION = 0.9
POWER_DROP_FRACTION = 0.5
POWER_DROP_MINUTES = 3.0


@dataclass
class _PlugState:
    session_id: int
    low_since: Optional[datetime] = None
    last_at: Optional[datetime] = None
    dropped_fired: bool = False
    nearly_fired: bool = False


class SessionEventDetector:
    """Synchronous and DB-free: the caller sends the notifications."""

    def __init__(self) -> None:
        self._plugs: Dict[int, _PlugState] = {}

    def _state(self, plug_id: int, session_id: int) -> _PlugState:
        st = self._plugs.get(plug_id)
        if st is None or st.session_id != session_id:
            st = _PlugState(session_id=session_id)
            self._plugs[plug_id] = st
        return st

    def observe_energy(self, plug_id: int, session_id: int, energy_kwh: Optional[float],
                       max_kwh: Optional[float]) -> bool:
        """True exactly once per session, when energy first sits in
        [NEARLY_DONE_FRACTION * max_kwh, max_kwh)."""
        if not max_kwh or max_kwh <= 0 or energy_kwh is None:
            return False
        st = self._state(plug_id, session_id)
        if st.nearly_fired:
            return False
        if max_kwh * NEARLY_DONE_FRACTION <= energy_kwh < max_kwh:
            st.nearly_fired = True
            return True
        return False

    def observe_power(self, plug_id: int, session_id: int, at: datetime, watts: float,
                      peak_w: Optional[float]) -> bool:
        """Feed one real power sample. True exactly once per session: on the
        sample that completes POWER_DROP_MINUTES of a sustained, non-idle drop."""
        from backend.services.mqtt_manager import CHARGE_FULL_ACTIVE_W, CHARGE_FULL_IDLE_W
        from backend.services.session_reaper import STALE_TIMEOUT_SEC

        st = self._state(plug_id, session_id)
        if st.dropped_fired:
            return False
        if st.last_at is not None and (at - st.last_at).total_seconds() > STALE_TIMEOUT_SEC:
            st.low_since = None
        st.last_at = at

        peak = max(peak_w or 0.0, watts)
        if (
            peak < CHARGE_FULL_ACTIVE_W
            or watts < CHARGE_FULL_IDLE_W
            or watts >= peak * POWER_DROP_FRACTION
        ):
            st.low_since = None
            return False
        if st.low_since is None:
            st.low_since = at
        if (at - st.low_since).total_seconds() >= POWER_DROP_MINUTES * 60:
            st.dropped_fired = True
            return True
        return False

    def interrupt(self, plug_id: int, session_id: int) -> None:
        """A live relay-OFF frame of this session: restart the drop timer."""
        self._state(plug_id, session_id).low_since = None
