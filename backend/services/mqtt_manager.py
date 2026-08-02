import asyncio
import logging
import os
import re
from typing import Callable, Dict, Optional

import paho.mqtt.client as mqtt

from backend.services.mqtt.alarms import MQTTAlarmMixin
from backend.services.mqtt.commands import MQTTCommandMixin
from backend.services.mqtt.connection import MQTTConnectionMixin
from backend.services.mqtt.discovery import MQTTDiscoveryMixin
from backend.services.mqtt.logs import MQTTLogsMixin
from backend.services.mqtt.router import MQTTRouterMixin
from backend.services.mqtt.status import MQTTStatusMixin
from backend.services.mqtt.telemetry import MQTTTelemetryMixin

# Root logger configuration (JSON formatter, correlation id) is owned by
# backend.logging_config.configure_logging(), called once from main.py's
# startup — this module just gets its named logger and propagates to root.
logger = logging.getLogger("amphive.mqtt")

# Telemetry refreshes gateways.last_seen_at (the liveness signal that gates
# session starts) at most this often per gateway — it arrives every ~1-10 s
# and each refresh is a DB write.
GATEWAY_SEEN_BUMP_INTERVAL_SEC = 60.0

# Prepaid protection: auto-stop a session once its accrued energy cost reaches
# the driver's wallet balance, so a drained wallet can't keep charging for free
# (the finalize path clamps the debit but doesn't end the session on its own).
# Env-toggleable; on by default.
AUTO_STOP_ON_BALANCE_EXHAUSTED = os.getenv("AUTO_STOP_ON_BALANCE_EXHAUSTED", "true").lower() in ("1", "true", "yes")

# Driver notification: warn once per session when the accrued cost crosses
# this fraction of the wallet balance (0 disables). Pairs with the in-app
# monitor warning, but reaches drivers who are not watching the app.
LOW_BALANCE_WARN_FRACTION = float(os.getenv("LOW_BALANCE_WARN_FRACTION", "0.8"))

# [Session limits] Backend mirror of the per-session stop conditions
# (ChargingSession.max_kwh / max_duration_seconds, snapshotted from the
# start request): auto-stop once the persisted energy or the elapsed time
# reaches the session's own limit. The firmware enforces the same limits
# locally (relay OFF) but publishes NO alarm on those cutoffs, so without
# this mirror the session would linger ACTIVE (still holding the plug
# OCCUPIED) until the staleness reaper. Same shape as
# AUTO_STOP_ON_BALANCE_EXHAUSTED above; env-toggleable, on by default. The
# session reaper carries a duration backstop under the same env var.
AUTO_STOP_ON_LIMITS = os.getenv("AUTO_STOP_ON_LIMITS", "true").lower() in ("1", "true", "yes")

# Fault console: a hardware SAFETY cutoff (THERMAL_CUTOFF/OVERCURRENT_CUTOFF)
# has already force-OFF'd the plug locally in firmware — auto-flip it to
# MAINTENANCE so a new session can't be *started* on it (session-start already
# 409s any non-AVAILABLE plug) until an operator clears it via
# POST /api/cpo/plugs/{id}/maintenance. Runs AFTER the safety-cutoff
# auto-finalize below (same coroutine, sequenced) so it isn't raced/overwritten
# by finalize_charging_session's own plug.status = AVAILABLE. Env-toggleable;
# on by default.
AUTO_MAINTENANCE_ON_CRITICAL_ALARM = os.getenv("AUTO_MAINTENANCE_ON_CRITICAL_ALARM", "true").lower() in ("1", "true", "yes")

# [Plug power] A plug counts as powered only while its firmware keeps reporting
# telemetry: last_telemetry_at is stamped on every inbound frame, and a gap
# longer than this window re-baselines powered_since (a mains/relay power-cycle).
# plug_is_powered() (services/session_lifecycle.py) reads the same window as the
# freshness threshold. Healthy plugs report every ~1-10 s, so ~90 s tolerates a
# few missed frames without flapping. Env-overridable.
PLUG_POWER_STALE_SEC = int(os.getenv("PLUG_POWER_STALE_SEC", "90"))

# [Opt-in charging limits] Charging limits (max_duration_seconds / max_kwh)
# are opt-in: a session with no explicit limit charges until the driver (or a
# real safety net) stops it — never a hidden default duration/energy. But the
# firmware's local relay watchdog (firmware/main/main.c, the ON and
# SET_LIMITS command handlers) has no concept of "no limit" today:
#   - an ABSENT max_duration_seconds/max_kwh field in the command payload
#     falls back to the firmware's OWN hard-coded default (14400 s / 30 kWh —
#     the exact old behavior this feature removes), and
#   - a PRESENT-but-zero value is read literally, which trips the watchdog
#     on the very next poll (`elapsed_s >= 0` and `consumed_kwh >= 0` are
#     always true) — an instant cutoff, not "unlimited".
# So neither omitting the field nor sending 0 encodes "no limit" on
# already-deployed firmware. These sentinels stand in for it instead: values
# large enough that no real charging session could ever reach them (a
# single-phase AC plug tops out at a few kW, and MAX_PLAUSIBLE_KWH already
# bounds a telemetry frame's session energy at 1000 kWh, well under
# UNLIMITED_MAX_KWH — see services/mqtt/telemetry.py), while staying safely
# inside the firmware's uint32_t-seconds / float-kWh range. The REAL stop
# conditions for an "unlimited" session are the backend-side safety nets
# (balance exhaustion, gateway-offline/staleness reaping, overcurrent, plug
# caps) — these sentinels only keep the on-device duration/energy watchdog
# from tripping FIRST. Env-overridable for tests/exotic fleets.
UNLIMITED_DURATION_SECONDS = int(
    os.getenv("UNLIMITED_DURATION_SECONDS", str(10 * 365 * 24 * 3600))
)  # ~10 years
UNLIMITED_MAX_KWH = float(os.getenv("UNLIMITED_MAX_KWH", "999999.0"))  # ~1 GWh


def firmware_duration(max_duration_seconds: Optional[int]) -> int:
    """The value to publish as the gateway's `max_duration_seconds` watchdog
    field: the driver's own explicit limit, or UNLIMITED_DURATION_SECONDS
    when they set none (see the module comment above for why this can't be
    0 or an omitted field)."""
    return (
        max_duration_seconds
        if max_duration_seconds is not None
        else UNLIMITED_DURATION_SECONDS
    )


def firmware_max_kwh(max_kwh: Optional[float]) -> float:
    """The value to publish as the gateway's `max_kwh` watchdog field: the
    driver's own explicit limit, or UNLIMITED_MAX_KWH when they set none."""
    return max_kwh if max_kwh is not None else UNLIMITED_MAX_KWH


class MQTTManager(
    MQTTConnectionMixin,
    MQTTRouterMixin,
    MQTTCommandMixin,
    MQTTTelemetryMixin,
    MQTTAlarmMixin,
    MQTTStatusMixin,
    MQTTDiscoveryMixin,
    MQTTLogsMixin,
):
    """
    Facade over the broker connection/lifecycle, inbound topic routing,
    telemetry ingestion, alarm handling, gateway-status reconciliation, plug
    discovery, and outbound command publishing — each of which is broken out
    into its own mixin module under services/mqtt/ (god-object split; see
    services/mqtt/__init__.py for why mixins rather than delegating
    collaborator objects). This class is the one public surface: every call
    site, test, and monkeypatch continues to target `MQTTManager` exactly as
    before the split — the mixins just organize where each method's source
    lives.
    """

    _instance: Optional["MQTTManager"] = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(MQTTManager, cls).__new__(cls)
        return cls._instance

    def __init__(
        self,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        telemetry_store=None,
        db_session_factory: Optional[Callable] = None,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
        telemetry_persistence=None,
    ):
        # Prevent re-initialization if already initialized
        if hasattr(self, "client"):
            return

        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password
        self.telemetry_store = telemetry_store
        self.db_session_factory = db_session_factory
        self.event_loop = event_loop
        # Buffered batch-flush sink for time-series persistence (optional).
        self.telemetry_persistence = telemetry_persistence
        # Per-gateway monotonic timestamp of the last last_seen_at refresh
        # (see GATEWAY_SEEN_BUMP_INTERVAL_SEC). Only touched on the paho thread.
        self._gateway_seen_bumped: Dict[str, float] = {}
        # Session ids already sent a low-balance warning (once per session).
        # Only touched on the event loop. Bounded by an occasional full clear —
        # worst case a long-running session gets one repeat warning.
        # ponytail: this is in-memory, so a mid-session restart re-fires the
        # warning once (same tolerated worst case as the clear above). The
        # auto-stop itself re-reads the DB under lock and stays exact — only
        # the advisory nudge repeats — so a persisted dedupe query isn't worth
        # a column/migration here.
        self._low_balance_warned: set = set()

        self.client = mqtt.Client(client_id="amphive_backend_server", callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # Regex mappings for topics
        # gateways/{gateway_id}/telemetry
        self.telemetry_pattern = re.compile(r"^amphive/gateways/([^/]+)/telemetry$")
        # gateways/{gateway_id}/status
        self.status_pattern = re.compile(r"^amphive/gateways/([^/]+)/status$")
        # gateways/{gateway_id}/discovery  (AmpHive Agent plug discovery)
        self.discovery_pattern = re.compile(r"^amphive/gateways/([^/]+)/discovery$")
        # gateways/{gateway_id}/alarms  (firmware safety alarms + OTA lifecycle)
        self.alarm_pattern = re.compile(r"^amphive/gateways/([^/]+)/alarms$")
        # gateways/{gateway_id}/logs  (firmware WARN/ERROR log lines, plain
        # text — TD#28, fw >= 2.1.0-direct)
        self.logs_pattern = re.compile(r"^amphive/gateways/([^/]+)/logs$")
