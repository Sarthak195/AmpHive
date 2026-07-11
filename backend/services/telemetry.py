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
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, AsyncGenerator

# Charging rate: coins deducted per kWh consumed during a session.
# Default 5.0 means 1 kWh costs 5 coins (= ₹5 at the default 1:1 coin:rupee rate).
COINS_PER_KWH = float(os.getenv("COINS_PER_KWH", "5.0"))

# A live telemetry snapshot older than this (seconds) is flagged `is_stale` in
# the stream so the frontend can warn the driver that the gateway link dropped
# instead of showing frozen values as if they were current. Should comfortably
# exceed the fastest firmware cadence (1 s while a listener is attached) plus
# network jitter; a dead gateway crosses it within a few missed reports.
TELEMETRY_STALE_AFTER_SEC = float(os.getenv("TELEMETRY_STALE_AFTER_SEC", "15"))


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
    relay_on: bool = False        # Plug's actual reported relay state (device_on)
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
            cls._instance._intervals: Dict[int, int] = {}
            cls._instance._active_listeners: Dict[int, int] = {}
        return cls._instance

    def increment_listeners(self, plug_id: int) -> int:
        """Increment the number of active SSE listeners for a plug."""
        self._active_listeners[plug_id] = self._active_listeners.get(plug_id, 0) + 1
        return self._active_listeners[plug_id]

    def decrement_listeners(self, plug_id: int) -> int:
        """Decrement the number of active SSE listeners for a plug."""
        current = self._active_listeners.get(plug_id, 0)
        if current > 0:
            self._active_listeners[plug_id] = current - 1
        else:
            self._active_listeners[plug_id] = 0
        return self._active_listeners[plug_id]

    def get_interval(self, plug_id: int) -> int:
        """Get the current polling interval (in ms) configured for a plug."""
        return self._intervals.get(plug_id, 10000)

    def set_interval(self, plug_id: int, interval: int) -> None:
        """Set the current polling interval (in ms) configured for a plug."""
        self._intervals[plug_id] = interval


    def update(self, plug_id: int, power_w: float, current_a: float,
               energy_kwh: float, status: str = "charging",
               cost_coins: Optional[float] = None,
               voltage_v: float = 230.0, relay_on: bool = False) -> None:
        """
        Called by the MQTT manager when a telemetry payload arrives from
        a gateway. Updates the in-memory snapshot and signals any waiting
        SSE consumers.

        If cost_coins is not provided, it is auto-calculated from
        energy_kwh * COINS_PER_KWH.
        """
        # Auto-calculate cost if not explicitly provided
        if cost_coins is None:
            cost_coins = round(energy_kwh * COINS_PER_KWH, 2)

        # Calculate session duration from start time
        if plug_id not in self._session_start_times:
            self._session_start_times[plug_id] = time.time()

        elapsed = int(time.time() - self._session_start_times[plug_id])

        self._data[plug_id] = TelemetrySnapshot(
            plug_id=plug_id,
            power_w=power_w,
            current_a=current_a,
            voltage_v=voltage_v,
            energy_kwh=energy_kwh,
            duration_sec=elapsed,
            cost_coins=cost_coins,
            status=status,
            relay_on=relay_on,
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
                snapshot_dict = snapshot.to_dict()
                if plug_id in self._session_start_times:
                    snapshot_dict["duration_sec"] = int(time.time() - self._session_start_times[plug_id])
                # Flag stale data so the client can warn the driver the gateway
                # link dropped rather than presenting frozen values as live.
                # Age is measured from when this snapshot was last written by MQTT.
                age = time.time() - snapshot.updated_at
                snapshot_dict["is_stale"] = age > TELEMETRY_STALE_AFTER_SEC
                snapshot_dict["age_sec"] = round(age, 1)
                yield snapshot_dict

                # Stop streaming if the session is completed
                if snapshot.status == "completed":
                    return
