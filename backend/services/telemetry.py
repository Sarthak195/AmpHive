"""
AmpHive Telemetry Service
=========================
In-memory store for real-time plug telemetry, fed by MQTT messages.
Provides an async SSE generator for streaming live session data to the frontend.

Design decisions:
- In-memory dict (not Redis) because we're running a single backend instance.
  If scaling to multi-replica K8s, migrate this to Redis Pub/Sub.
- asyncio.Event is used to notify SSE consumers when new telemetry arrives,
  avoiding polling loops and reducing CPU usage.
- Each plug has its own telemetry entry. The SSE endpoint filters by session_id
  to stream only the relevant plug's data to the requesting driver.
"""

import asyncio
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, AsyncGenerator


@dataclass
class TelemetrySnapshot:
    """
    A single telemetry reading from a smart plug, as reported by the ESP32
    gateway over MQTT. Updated every ~15 seconds from the hardware, but the
    SSE stream sends to the frontend every 1 second (interpolated or latest).
    """
    plug_id: int = 0
    power_w: float = 0.0          # Instantaneous power draw in Watts
    current_a: float = 0.0        # Current draw in Amps
    voltage_v: float = 230.0      # Voltage (typically 230V in India)
    energy_kwh: float = 0.0       # Cumulative energy consumed this session
    duration_sec: int = 0         # Session elapsed time in seconds
    cost_coins: float = 0.0       # Running cost deducted from wallet
    status: str = "idle"          # "idle" | "starting" | "charging" | "completed"
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class TelemetryStore:
    """
    Singleton in-memory store holding the latest telemetry for each active plug.
    The MQTT manager writes to this store; SSE endpoints read from it.
    """
    _instance: Optional["TelemetryStore"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data: Dict[int, TelemetrySnapshot] = {}
            cls._instance._events: Dict[int, asyncio.Event] = {}
            cls._instance._session_start_times: Dict[int, float] = {}
        return cls._instance

    def update(self, plug_id: int, power_w: float, current_a: float,
               energy_kwh: float, status: str = "charging",
               cost_coins: float = 0.0) -> None:
        """
        Called by the MQTT manager when a telemetry payload arrives from
        a gateway. Updates the in-memory snapshot and signals any waiting
        SSE consumers.
        """
        # Calculate session duration from start time
        if plug_id not in self._session_start_times:
            self._session_start_times[plug_id] = time.time()

        elapsed = int(time.time() - self._session_start_times[plug_id])

        self._data[plug_id] = TelemetrySnapshot(
            plug_id=plug_id,
            power_w=power_w,
            current_a=current_a,
            energy_kwh=energy_kwh,
            duration_sec=elapsed,
            cost_coins=cost_coins,
            status=status,
        )

        # Signal any SSE consumers waiting on this plug's data
        if plug_id in self._events:
            self._events[plug_id].set()

    def start_session(self, plug_id: int) -> None:
        """Mark the start of a new charging session for a plug."""
        self._session_start_times[plug_id] = time.time()
        self._data[plug_id] = TelemetrySnapshot(
            plug_id=plug_id, status="starting"
        )
        # Create or reset the event for this plug
        self._events[plug_id] = asyncio.Event()

    def end_session(self, plug_id: int) -> None:
        """Clean up session tracking when a session ends."""
        if plug_id in self._session_start_times:
            del self._session_start_times[plug_id]
        if plug_id in self._data:
            self._data[plug_id].status = "completed"
        if plug_id in self._events:
            self._events[plug_id].set()  # Wake up any listeners so they see "completed"

    def get_latest(self, plug_id: int) -> Optional[TelemetrySnapshot]:
        """Get the latest telemetry snapshot for a plug."""
        return self._data.get(plug_id)

    async def stream(self, plug_id: int) -> AsyncGenerator[dict, None]:
        """
        Async generator that yields telemetry snapshots for a plug.
        Used by the SSE endpoint to stream data to the frontend.
        Yields every 1 second, using the latest available data.
        """
        # Ensure there's an event for this plug
        if plug_id not in self._events:
            self._events[plug_id] = asyncio.Event()

        while True:
            # Wait for new data or timeout after 1 second
            try:
                await asyncio.wait_for(self._events[plug_id].wait(), timeout=1.0)
                self._events[plug_id].clear()
            except asyncio.TimeoutError:
                pass  # Still yield the latest snapshot even if no new data arrived

            snapshot = self.get_latest(plug_id)
            if snapshot:
                yield snapshot.to_dict()

                # Stop streaming if the session is completed
                if snapshot.status == "completed":
                    return
