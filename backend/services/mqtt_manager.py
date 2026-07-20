import asyncio
import functools
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Any, Optional

import paho.mqtt.client as mqtt

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


class MQTTManager:
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

    def start(self):
        logger.info(
            "Connecting to MQTT broker",
            extra={"broker_host": self.broker_host, "broker_port": self.broker_port},
        )
        self.client.connect_async(self.broker_host, self.broker_port, keepalive=60)
        self.client.loop_start()

    def stop(self):
        logger.info("Stopping MQTT client loop...")
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("Connected successfully to MQTT broker")
            # Subscribe to all gateway telemetry and status topics
            self.client.subscribe("amphive/gateways/+/telemetry", qos=0)
            self.client.subscribe("amphive/gateways/+/status", qos=1)
            # AmpHive Agent plug discovery (auto-populate) — retained announcements.
            self.client.subscribe("amphive/gateways/+/discovery", qos=1)
            # Firmware safety alarms (THERMAL/OVERCURRENT/UNAUTHORIZED_ON) + OTA
            # lifecycle events — persisted as GatewayEvents and surfaced to CPOs.
            self.client.subscribe("amphive/gateways/+/alarms", qos=1)
        else:
            logger.error("Failed to connect to MQTT broker", extra={"rc": rc})

    def _on_disconnect(self, client, userdata, rc, properties=None, reason_code=None):
        logger.warning("Disconnected from MQTT broker", extra={"rc": rc})

    def _on_message(self, client, userdata, msg):
        payload_str = msg.payload.decode("utf-8", errors="ignore")
        logger.debug(
            "Received MQTT message",
            extra={"topic": msg.topic, "payload": payload_str},
        )

        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON payload on MQTT topic",
                extra={"topic": msg.topic, "payload": payload_str},
            )
            return

        # Match status topic
        status_match = self.status_pattern.match(msg.topic)
        if status_match:
            gateway_id = status_match.group(1)
            self._handle_gateway_status(gateway_id, payload)
            return

        # Match telemetry topic
        telemetry_match = self.telemetry_pattern.match(msg.topic)
        if telemetry_match:
            gateway_id = telemetry_match.group(1)
            self._handle_gateway_telemetry(gateway_id, payload)
            return

        # Match discovery topic (AmpHive Agent auto-populate)
        discovery_match = self.discovery_pattern.match(msg.topic)
        if discovery_match:
            gateway_id = discovery_match.group(1)
            self._handle_gateway_plug_discovery(gateway_id, payload)
            return

        # Match alarms topic (firmware safety alarms + OTA lifecycle events)
        alarm_match = self.alarm_pattern.match(msg.topic)
        if alarm_match:
            gateway_id = alarm_match.group(1)
            self._handle_gateway_alarm(gateway_id, payload)
            return

    # -----------------------------------------------------------------------
    # Inbound telemetry handler — feeds TelemetryStore + persists to DB
    # -----------------------------------------------------------------------

    def _handle_gateway_telemetry(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process a telemetry payload from an ESP32 gateway.

        Expected MQTT payload (per the MQTT_CONTRACT):
        {
            "plug_id": 1,
            "watts": 1200.5,
            "kwh": 0.45,
            "voltage": 230.0,
            "current": 5.2,
            "status": "occupied" | "available",
            "session_id": "42"   # optional; echoed from the ON command, "" when idle
        }

        Actions:
        1. Feed the TelemetryStore so the SSE stream picks it up.
        2. Persist energy_kwh / peak_power_w to the active ChargingSession row
           and update the plug's current_power_w.
        """
        plug_id = payload.get("plug_id")
        if plug_id is None:
            logger.warning(
                "Telemetry missing plug_id, ignoring",
                extra={"gateway_id": gateway_id},
            )
            return
        try:
            plug_id = int(plug_id)
        except (TypeError, ValueError):
            logger.warning(
                "Telemetry has non-integer plug_id, ignoring",
                extra={"gateway_id": gateway_id, "raw_plug_id": repr(payload.get("plug_id"))},
            )
            return

        # Guarded casts (TD#25): a malformed value used to raise inside the
        # paho callback — the reading vanished with no log line and (paho
        # version depending) could kill message dispatch. Non-finite values
        # (NaN/inf parse fine as JSON-ish strings) are rejected too: NaN
        # watts would poison the peak-power comparison and session totals.
        try:
            watts = float(payload.get("watts", 0.0))
            kwh = float(payload.get("kwh", 0.0))
            voltage = float(payload.get("voltage", 230.0))
            current = float(payload.get("current", 0.0))
        except (TypeError, ValueError):
            logger.warning(
                "Telemetry has non-numeric fields, ignoring",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "payload": payload},
            )
            return
        if not all(math.isfinite(v) for v in (watts, kwh, voltage, current)):
            logger.warning(
                "Telemetry has non-finite values, ignoring",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "payload": payload},
            )
            return

        status = payload.get("status", "occupied")
        # Actual relay state as reported by the plug (firmware ≥ 1.5.0). Distinct
        # from `status` (our session state) — lets the UI show the physical relay.
        relay_on = bool(payload.get("relay", status == "occupied"))

        # Optional backend session id echoed by the firmware. Empty/absent when
        # the plug is idle, or on pre-session_id firmware. Used to attribute the
        # reading to the exact session rather than "the active session on this
        # plug" (matters if a reading arrives late / after the plug was reused).
        session_id = None
        raw_sid = payload.get("session_id")
        if raw_sid not in (None, ""):
            try:
                sid = int(raw_sid)
                session_id = sid if sid > 0 else None
            except (ValueError, TypeError):
                session_id = None

        # A resync frame drained from the gateway's offline buffer carries
        # `offline: true` (TD#24). It is a historical reading, so it may update
        # its own (still-ACTIVE) session's energy but must never drive live relay
        # actuation (REC-02) — see _persist_telemetry.
        is_offline = bool(payload.get("offline", False))

        # Map firmware status to telemetry store status
        telem_status = "charging" if status == "occupied" else "idle"

        logger.info(
            "Telemetry received",
            extra={
                "gateway_id": gateway_id,
                "plug_id": plug_id,
                "session_id": session_id,
                "watts": round(watts, 1),
                "kwh": round(kwh, 3),
                "current_a": round(current, 1),
                "voltage_v": round(voltage, 0),
                "status": status,
            },
        )

        # --- 1. Feed the in-memory TelemetryStore (for the live stream) ---
        # This callback runs on the paho network thread. TelemetryStore.update()
        # signals asyncio.Events that live on the server's event loop, and
        # asyncio.Event.set() is NOT thread-safe when called from another thread —
        # it can fail to wake stream() waiters or corrupt loop state. Marshal the
        # update onto the loop so the whole store stays single-threaded.
        # (cost_coins is left to TelemetryStore to auto-calc from this plug's
        # snapshotted per-session rate, falling back to COINS_PER_KWH.)
        if self.telemetry_store and self.event_loop:
            self.event_loop.call_soon_threadsafe(
                functools.partial(
                    self.telemetry_store.update,
                    plug_id, watts, current, kwh, telem_status,
                    voltage_v=voltage, relay_on=relay_on,
                )
            )
        elif self.telemetry_store:
            # No loop reference (e.g. unit tests): safe to call directly.
            self.telemetry_store.update(
                plug_id=plug_id,
                power_w=watts,
                current_a=current,
                energy_kwh=kwh,
                status=telem_status,
                voltage_v=voltage,
                relay_on=relay_on,
            )

        # --- 2. Build the raw sample for time-series persistence ---
        # Buffered + batch-flushed by TelemetryPersistenceService. This is where
        # voltage/current/status (parsed above but not used for session totals)
        # get persisted to telemetry_readings. When a DB is available the
        # enqueue is deferred into _persist_telemetry so it only happens after
        # the plug-ownership check (a gateway must not write history rows for
        # another gateway's plug); with no DB there is nothing to check
        # against, so enqueue directly (unit tests / standalone use).
        sample = None
        if self.telemetry_persistence:
            sample = {
                "plug_id": plug_id,
                "recorded_at": datetime.now(timezone.utc),
                "power_w": watts,
                "energy_kwh": kwh,
                "voltage_v": voltage,
                "current_a": current,
                "status": status,
            }
            if not (self.db_session_factory and self.event_loop):
                self.telemetry_persistence.enqueue(sample)

        # --- 3. Persist authoritative session totals (async, fire-and-forget) ---
        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_telemetry(gateway_id, plug_id, watts, kwh, session_id, sample, relay_on, is_offline),
                self.event_loop,
            )
            # Telemetry proves the gateway is alive — refresh its liveness
            # marker (throttled). Status messages alone only arrive on
            # connect/LWT, so a long-connected gateway would otherwise look
            # stale to the session-start liveness gate.
            if self._should_bump_gateway_seen(gateway_id):
                asyncio.run_coroutine_threadsafe(
                    self._persist_gateway_seen(gateway_id),
                    self.event_loop,
                )

    def _should_bump_gateway_seen(self, gateway_id: str) -> bool:
        """Rate-limit last_seen_at refreshes to one per gateway per
        GATEWAY_SEEN_BUMP_INTERVAL_SEC. Runs on the paho thread only."""
        now = time.monotonic()
        last = self._gateway_seen_bumped.get(gateway_id)
        if last is not None and (now - last) < GATEWAY_SEEN_BUMP_INTERVAL_SEC:
            return False
        self._gateway_seen_bumped[gateway_id] = now
        return True

    async def _persist_gateway_seen(self, gateway_id: str):
        """Mark a gateway ONLINE + freshly seen because telemetry arrived from
        it (also heals a gateway stuck OFFLINE after a missed retained status)."""
        from backend.database.models import Gateway, GatewayStatus
        from sqlalchemy import select

        try:
            async with self.db_session_factory() as session:
                result = await session.execute(
                    select(Gateway).where(Gateway.id == gateway_id)
                )
                gateway = result.scalar_one_or_none()
                if gateway:
                    gateway.status = GatewayStatus.ONLINE
                    gateway.last_seen_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception as e:
            logger.error(
                "Failed to refresh last_seen_at for gateway",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )

    # -----------------------------------------------------------------------
    # Inbound alarm handler — persists GatewayEvents + broadcasts to clients
    # -----------------------------------------------------------------------
    # Map a firmware alarm/event string to a UI severity. Safety cutoffs and an
    # unauthorized energize are operator-critical; OTA lifecycle notices are
    # informational. Unknown strings default to "warning" so nothing is silently
    # swallowed.
    _EVENT_SEVERITY = {
        "THERMAL_CUTOFF": "critical",
        "OVERCURRENT_CUTOFF": "critical",
        # OVERCURRENT_CAP is a soft/policy cap trip (the car drew more than the
        # operator-set per-plug cap, below the P110's own hardware cutoff) — a
        # warning, not a hardware fault.
        "OVERCURRENT_CAP": "warning",
        # LOCAL_LIMIT_CUTOFF is the software agent's local watchdog hitting the
        # session's own kWh/duration limit — an expected end-of-session (the
        # agent cut the plug OFF locally), not a fault.
        "LOCAL_LIMIT_CUTOFF": "info",
        "UNAUTHORIZED_ON": "critical",
        "OTA_STARTED": "info",
        "OTA_OK_REBOOTING": "info",
        "OTA_FAILED": "warning",
        "OTA_REFUSED_SESSION_ACTIVE": "info",
        "OTA_START_FAILED": "warning",
    }

    _EVENT_DETAIL = {
        "UNAUTHORIZED_ON": "Plug switched ON with no active session (physical button / app / stale resume) — forced OFF locally.",
        "THERMAL_CUTOFF": "Plug reported overheat — session cut off locally.",
        "OVERCURRENT_CUTOFF": "Plug reported over-current — session cut off locally.",
        "OVERCURRENT_CAP": "Plug drew more than its configured current cap — session stopped locally.",
        "LOCAL_LIMIT_CUTOFF": "Session hit its energy/duration limit — the gateway agent cut the plug off locally.",
    }

    def _handle_gateway_alarm(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process an alarm/event from a gateway (topic .../alarms). Two shapes:
          {"error": "UNAUTHORIZED_ON", "plug_id": 1}   # safety alarm/fault
          {"event": "OTA_STARTED"}                       # OTA lifecycle notice
        Persists a GatewayEvent (audit + CPO feed) and broadcasts it so a live
        client can react (e.g. warn the driver, flash the operator's alert feed).
        """
        event_type = payload.get("error") or payload.get("event")
        if not event_type:
            logger.warning(
                "Alarm missing error/event field",
                extra={"gateway_id": gateway_id, "payload": payload},
            )
            return

        event_type = str(event_type)[:48]
        severity = self._EVENT_SEVERITY.get(event_type, "warning")
        detail = self._EVENT_DETAIL.get(event_type)

        plug_id = payload.get("plug_id")
        try:
            plug_id = int(plug_id) if plug_id is not None else None
        except (ValueError, TypeError):
            plug_id = None

        logger.warning(
            "Gateway alarm",
            extra={
                "gateway_id": gateway_id,
                "plug_id": plug_id,
                "event_type": event_type,
                "severity": severity,
            },
        )

        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_gateway_event(gateway_id, plug_id, event_type, severity, detail),
                self.event_loop,
            )

    async def _persist_gateway_event(self, gateway_id: str, plug_id: Optional[int],
                                     event_type: str, severity: str, detail: Optional[str]):
        """Store the event (tenant resolved from the gateway) and broadcast it."""
        from backend.database.models import Gateway, GatewayEvent

        from sqlalchemy import select

        try:
            async with self.db_session_factory() as session:
                gw = (await session.execute(
                    select(Gateway).where(Gateway.id == gateway_id)
                )).scalar_one_or_none()
                if not gw:
                    logger.warning(
                        "Alarm for unknown gateway, dropping event",
                        extra={"gateway_id": gateway_id},
                    )
                    return

                event = GatewayEvent(
                    tenant_id=gw.tenant_id,
                    gateway_id=gateway_id,
                    plug_id=plug_id,
                    event_type=event_type,
                    severity=severity,
                    detail=detail,
                )
                session.add(event)
                await session.commit()
                event_id = event.id
                created_at = event.created_at
        except Exception as e:
            logger.error(
                "Failed to persist gateway event",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "error": str(e)},
            )
            return

        # Broadcast to connected clients (best-effort; import late to avoid a
        # circular import at module load).
        try:
            from backend.services.socketio_manager import emit_gateway_alarm
            await emit_gateway_alarm({
                "id": event_id,
                "gateway_id": gateway_id,
                "plug_id": plug_id,
                "event_type": event_type,
                "severity": severity,
                "detail": detail,
                "created_at": created_at.isoformat() if created_at else None,
            })
        except Exception as e:
            logger.error(
                "Failed to broadcast gateway alarm",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "error": str(e)},
            )

        # A finalize-worthy alarm means the firmware already forced the relay OFF
        # and cleared its local session — finalize the backend session to match
        # (bills the recorded energy, frees the plug, notifies the driver).
        # Previously the session sat ACTIVE until the reaper noticed, and the
        # driver never learned why charging stopped. This includes OVERCURRENT_CAP
        # (a soft cap trip): the firmware stopped charging, so the backend must
        # finalize too — otherwise the plug is orphaned OCCUPIED until the reaper.
        if plug_id is not None and event_type in self._FINALIZE_ALARM_REASONS:
            await self._finalize_session_after_cutoff(plug_id, event_type)

        # Fault console (additive): hardware safety cutoffs ALSO auto-enter the
        # plug into MAINTENANCE so a NEW session can't start until an operator
        # clears it (session-start already 409s any non-AVAILABLE plug).
        # Deliberately sequenced AFTER the finalize call above, in this same
        # coroutine — not an independently scheduled task — so the MAINTENANCE
        # flip can't be raced/overwritten by finalize_charging_session's own
        # `plug.status = AVAILABLE`. Runs even when no session was found above
        # (a cutoff with no active session still needs the plug taken out of
        # service). Env-gated via AUTO_MAINTENANCE_ON_CRITICAL_ALARM.
        # OVERCURRENT_CAP is deliberately EXCLUDED (not in the maintenance set):
        # it's a policy limit on a HEALTHY plug, so the plug stays AVAILABLE after
        # finalize rather than forcing an operator to clear maintenance each trip.
        if (
            AUTO_MAINTENANCE_ON_CRITICAL_ALARM
            and plug_id is not None
            and event_type in self._MAINTENANCE_ALARM_REASONS
        ):
            await self._auto_enter_maintenance(plug_id, event_type)

    # Alarms after which the firmware has already stopped charging locally, so the
    # backend must finalize the session to match. The value is the finalize reason,
    # which routes the driver notification (see session_lifecycle.finalize_*).
    _FINALIZE_ALARM_REASONS = {
        "THERMAL_CUTOFF": "safety cutoff: plug reported overheat",
        "OVERCURRENT_CUTOFF": "safety cutoff: plug reported over-current",
        "OVERCURRENT_CAP": "current cap exceeded: plug drew over its configured limit",
        # The software agent's local watchdog (agent/amphive_agent/core.py)
        # already cut the plug OFF and cleared its local session — finalize so
        # the session bills and the driver is notified instead of the session
        # orphaning ACTIVE until the reaper. A healthy plug: no maintenance.
        "LOCAL_LIMIT_CUTOFF": "limit reached: session hit its energy/duration limit",
    }

    # The subset that also takes the plug OUT OF SERVICE — genuine hardware faults
    # only. A cap trip (OVERCURRENT_CAP) is intentionally NOT here.
    _MAINTENANCE_ALARM_REASONS = {"THERMAL_CUTOFF", "OVERCURRENT_CUTOFF"}

    async def _finalize_session_after_cutoff(self, plug_id: int, event_type: str):
        """Finalize the ACTIVE session (if any) on a plug the firmware just cut
        off. Race-safe: finalize row-locks and re-checks ACTIVE, so a
        concurrent user stop / reaper settles exactly once."""
        from backend.database.models import ChargingSession, SessionStatus
        from backend.services.session_lifecycle import finalize_charging_session
        from sqlalchemy import select

        try:
            async with self.db_session_factory() as db:
                session_id = (await db.execute(
                    select(ChargingSession.id).where(
                        ChargingSession.plug_id == plug_id,
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                )).scalar_one_or_none()
            if session_id is None:
                return
            async with self.db_session_factory() as db:
                outcome = await finalize_charging_session(
                    db, session_id, reason=self._FINALIZE_ALARM_REASONS[event_type]
                )
            if outcome is not None:
                logger.warning(
                    f"Finalized session {session_id} after {event_type} on plug {plug_id}"
                )
        except Exception:
            logger.exception(f"Post-cutoff finalize failed for plug {plug_id} ({event_type})")

    async def _auto_enter_maintenance(self, plug_id: int, event_type: str):
        """
        Force a plug into MAINTENANCE after a hardware safety cutoff (called
        from _persist_gateway_event, right after _finalize_session_after_cutoff
        — see the ordering note there). The firmware has already force-OFF'd
        the relay locally — this only stops a *new* session from being started
        on the plug (session-start 409s any non-AVAILABLE plug) until an
        operator clears it via POST /api/cpo/plugs/{id}/maintenance. No-op if
        the plug is unknown or already MAINTENANCE.
        """
        from backend.database.models import Plug, PlugStatus
        from sqlalchemy import select

        try:
            async with self.db_session_factory() as session:
                plug = (await session.execute(
                    select(Plug).where(Plug.id == plug_id)
                )).scalar_one_or_none()
                if plug is None:
                    logger.warning(
                        "Auto-maintenance: plug not found, skipping",
                        extra={"plug_id": plug_id},
                    )
                    return
                if plug.status == PlugStatus.MAINTENANCE:
                    return
                plug.status = PlugStatus.MAINTENANCE
                await session.commit()
        except Exception as e:
            logger.error(
                "Auto-maintenance failed",
                extra={"plug_id": plug_id, "error": str(e)},
            )
            return

        logger.warning(
            "Plug auto-set to MAINTENANCE after safety alarm (operator must clear it)",
            extra={"plug_id": plug_id, "event_type": event_type},
        )
        try:
            from backend.services.socketio_manager import emit_plug_status
            await emit_plug_status(plug_id, PlugStatus.MAINTENANCE.value)
        except Exception as e:
            logger.error(
                "Failed to broadcast auto-maintenance plug_status",
                extra={"plug_id": plug_id, "error": str(e)},
            )

    async def _persist_telemetry(self, gateway_id: str, plug_id: int, watts: float, kwh: float,
                                 session_id: Optional[int] = None,
                                 sample: Optional[Dict[str, Any]] = None,
                                 relay_on: bool = False,
                                 is_offline: bool = False):
        """
        Persist the latest telemetry snapshot to the database:
        - Verify the claimed plug actually belongs to the publishing gateway
          (the broker ACLs scope *topics* to a gateway, not payload claims — a
          compromised gateway could otherwise attribute energy/billing to
          another tenant's plug).
        - Update `plugs.current_power_w` so the plug list shows real-time power.
        - Enqueue the raw `sample` for time-series persistence (deferred here
          from the handler so it sits behind the same ownership check).
        - Update the target `charging_sessions` row with cumulative energy and
          peak power, so that even if the server crashes, the last-known values
          are saved.

        Session selection: prefer the firmware-reported `session_id` (guarded so
        it must be ACTIVE and on this plug — never mutate a finalized, already
        billed session), and fall back to "the ACTIVE session on this plug" when
        no id was reported. The plug-id fallback is unambiguous in normal
        operation (one ACTIVE session per plug), but the explicit id avoids
        misattributing a late/replayed reading after the plug was reused.
        """
        # Import here to avoid circular imports at module level
        from backend.database.models import Plug, ChargingSession, SessionStatus

        # Captured for the post-commit balance/limit checks (see below).
        updated_session_id: Optional[int] = None
        updated_user_id: Optional[int] = None
        updated_rate: Optional[Decimal] = None
        updated_hold_coins: Optional[Decimal] = None
        updated_max_kwh: Optional[float] = None
        updated_max_duration: Optional[int] = None
        updated_started_at: Optional[datetime] = None
        # [Pricing v2] Segment accrual state, captured AFTER an in-frame reprice
        # so the exhaustion check and the live-cost mirror both see the new
        # rate. `reprice` is (new_rate, boundary) when a TOD boundary closed a
        # segment this frame, else None.
        updated_settled: Optional[Decimal] = None
        updated_segment_start: Optional[float] = None
        reprice = None

        try:
            async with self.db_session_factory() as session:
                from sqlalchemy import select, and_

                # Ownership check: the payload's plug_id must name a plug of
                # the gateway that published on this topic.
                plug_result = await session.execute(
                    select(Plug).where(Plug.id == plug_id)
                )
                plug = plug_result.scalar_one_or_none()
                if plug is None or plug.gateway_id != gateway_id:
                    logger.warning(
                        "Telemetry plug ownership mismatch, dropping reading",
                        extra={
                            "gateway_id": gateway_id,
                            "plug_id": plug_id,
                            "actual_owner_gateway_id": plug.gateway_id if plug else None,
                        },
                    )
                    return

                # [Plug power] Stamp the per-plug liveness clock. powered_since
                # re-baselines to now whenever telemetry resumes after a gap
                # longer than PLUG_POWER_STALE_SEC (a mains/relay power-cycle);
                # last_telemetry_at is the freshness signal plug_is_powered()
                # reads. Distinct from the never-written plugs.last_seen_at.
                now = datetime.now(timezone.utc)
                prev = plug.last_telemetry_at
                if prev is None or (now - prev).total_seconds() > PLUG_POWER_STALE_SEC:
                    plug.powered_since = now
                plug.last_telemetry_at = now

                # Update plug's current power reading
                plug.current_power_w = watts

                # Raw time-series sample — enqueue now that ownership is proven.
                if self.telemetry_persistence and sample is not None:
                    self.telemetry_persistence.enqueue(sample)

                # Find the session to update (see docstring for selection rules).
                if session_id is not None:
                    where_clause = and_(
                        ChargingSession.id == session_id,
                        ChargingSession.plug_id == plug_id,
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                else:
                    where_clause = and_(
                        ChargingSession.plug_id == plug_id,
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                # [REC-04] Lock the row the way the reaper reprice does
                # (session_reaper.py) so the forward-only reprice + rate_changed
                # emit below can't race a concurrent frame or the reaper's sweep.
                sess_result = await session.execute(
                    select(ChargingSession).where(where_clause).with_for_update()
                )
                active_session = sess_result.scalar_one_or_none()

                # [REC-05] An idle frame — no session_id AND relay off (firmware
                # emits session_id="" + session_kwh=0 when its session_active is
                # false) — reached an ACTIVE session only via the plug-id
                # fallback. It must NOT be attributed to that session: overwriting
                # its energy (with 0) or refreshing its staleness clock would
                # zero/keep-alive a freshly-started session this frame predates.
                # Require a matching session_id or a live relay to touch it.
                frame_is_idle = session_id is None and not relay_on
                if active_session is not None and not frame_is_idle:
                    # [REC-01] Energy is monotonic: never bill down. A reading
                    # below the stored total (meter reset) re-baselines to the
                    # max, matching the guard already on the peak-power write.
                    active_session.energy_kwh = max(active_session.energy_kwh or 0.0, kwh)
                    # Track peak power — only update if this reading is higher
                    if watts > active_session.peak_power_w:
                        active_session.peak_power_w = watts
                    # Staleness signal read by the session reaper.
                    active_session.last_telemetry_at = datetime.now(timezone.utc)
                    # [Pricing v2] Forward-only reprice if a TOD slot boundary
                    # has passed. Cheap: a flat session (rate_valid_until None)
                    # or one still inside its segment returns with no DB query;
                    # only the crossing frame re-resolves the tariff and closes
                    # out the segment in place, so the captures below — and the
                    # same-frame exhaustion check — bill at the new rate.
                    from backend.services.pricing import reprice_session_if_due
                    reprice = await reprice_session_if_due(session, active_session, plug)
                    updated_session_id = active_session.id
                    updated_user_id = active_session.user_id
                    updated_rate = active_session.rate_coins_per_kwh
                    updated_settled = active_session.settled_cost_coins
                    updated_segment_start = active_session.rate_segment_start_kwh
                    updated_hold_coins = active_session.hold_coins
                    updated_max_kwh = active_session.max_kwh
                    updated_max_duration = active_session.max_duration_seconds
                    updated_started_at = active_session.started_at

                # [REC-02] Level-triggered OFF reconciliation. The relay is
                # reported ON but no ACTIVE session owns this plug — a prior OFF
                # was lost/failed while the gateway stayed connected, so the
                # reconnect-only _republish_off_for_orphaned_plugs never fired.
                # Re-send OFF (idempotent — a no-op on an already-off plug),
                # using the same best-effort wait=False publish the reconnect
                # path uses so we don't block the loop on the broker ack.
                # Skipped for offline-resync frames (TD#24): those are historical
                # readings, not the plug's live relay state — an id-scoped lookup
                # that misses a since-finalized session must not actuate the relay.
                elif active_session is None and relay_on and not is_offline:
                    self.send_plug_command(
                        gateway_id, plug_id, "OFF", local_ip=plug.local_ip, wait=False
                    )
                    logger.info(
                        "Republished OFF for relay-on plug with no ACTIVE session",
                        extra={"gateway_id": gateway_id, "plug_id": plug_id},
                    )

                await session.commit()
        except Exception as e:
            logger.error(
                "Failed to persist telemetry",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "error": str(e)},
            )
            return

        # Prepaid protection: if the accrued energy cost has reached the driver's
        # wallet balance, auto-stop so they can't keep charging for free past a
        # drained wallet (the finalize path only clamps the debit — it doesn't
        # stop the session). Done in a separate txn after the persist commit.
        if updated_session_id is not None and updated_user_id is not None:
            # [REC-11] After a restart the live-cost mirror is empty (start_session
            # ran in the now-dead process), so update() billed this frame at the
            # env default and restarted the elapsed timer. Rebuild the mirror from
            # the row we just loaded so subsequent frames stream the session's real
            # rate/segment/started_at. No-op once the plug is already tracked.
            if self.telemetry_store is not None:
                self.telemetry_store.hydrate_session(
                    plug_id, updated_rate, updated_settled,
                    updated_segment_start, updated_started_at,
                )
            # [Pricing v2] A TOD boundary closed a segment this frame: keep the
            # live-cost mirror exact and tell the driver (forward-only). Both
            # best-effort, post-commit — the billing state already persisted.
            if reprice is not None:
                new_rate, _boundary = reprice
                if self.telemetry_store is not None:
                    self.telemetry_store.set_segment_state(
                        plug_id, updated_settled, updated_segment_start, new_rate
                    )
                from backend.services.billing import rate_changed_body
                from backend.services.notifications import notify
                await notify(
                    updated_user_id, "rate_changed", "Charging rate changed",
                    rate_changed_body(new_rate), severity="info",
                    plug_id=plug_id, session_id=updated_session_id,
                )
            await self._maybe_auto_stop_on_exhaustion(
                updated_session_id, updated_user_id, kwh, updated_rate,
                updated_hold_coins, updated_settled, updated_segment_start,
            )
            # [Session limits] User-set stop conditions, mirrored backend-side
            # (see _maybe_auto_stop_on_limits). Sequenced AFTER the exhaustion
            # check deliberately: if both trip on the same frame the
            # exhaustion path finalizes first (its reason wins — revenue
            # protection) and this call's finalize re-check returns None; if
            # a limit fires here instead, the earlier exhaustion call was a
            # covered no-op (at most a low-balance warning) — either way the
            # session settles exactly once via the shared finalize row lock.
            await self._maybe_auto_stop_on_limits(
                updated_session_id, updated_user_id, kwh,
                updated_max_kwh, updated_max_duration, updated_started_at,
            )

    async def _maybe_auto_stop_on_exhaustion(self, session_id: int, user_id: int, energy_kwh: float,
                                              rate_coins_per_kwh: Optional[Decimal] = None,
                                              hold_coins: Optional[Decimal] = None,
                                              settled_cost_coins: Optional[Decimal] = None,
                                              rate_segment_start_kwh: Optional[float] = None):
        """Finalize an ACTIVE session once its accrued cost meets/exceeds its
        exhaustion threshold. No-op when disabled, when the threshold still
        covers the energy, or when the session is already gone (finalize
        re-checks ACTIVE under a row lock, so this is race-safe).

        `rate_coins_per_kwh` is the session's SNAPSHOTTED rate (see
        services/pricing.py resolve_rate_for_plug / models.py
        ChargingSession.rate_coins_per_kwh) — the accrued cost must use the
        same rate this session will actually be billed at, not the global
        default, or a tariff'd session could auto-stop too early/late. NULL
        (legacy sessions with no snapshot) falls back to the env default.

        [Auth holds] `hold_coins` is the session's own authorization-hold
        reservation (ChargingSession.hold_coins, snapshotted at start — see
        routers/sessions.py start_charging_session / services/wallet.py
        available_balance). When set, THIS — not the driver's whole wallet
        balance — is the exhaustion threshold: a concurrent second session
        may be holding the rest of that balance, so this session must only
        auto-stop when it exhausts its OWN reservation, never a figure a
        sibling session is also counting on. No DB read is needed in that
        case (the threshold is already in hand). NULL (legacy sessions
        predating this column) falls back to the live wallet balance,
        matching the pre-hold behavior exactly."""
        if not AUTO_STOP_ON_BALANCE_EXHAUSTED or not self.db_session_factory:
            return
        from types import SimpleNamespace
        from backend.services.billing import session_cost
        from backend.services.money import to_money
        from backend.services.telemetry import COINS_PER_KWH

        try:
            # [Pricing v2] Accrue against the session's segment state (settled
            # closed-segment coins + open segment at the current rate) so a
            # session that crossed a TOD boundary stops at the RIGHT accrued
            # cost, not a flat re-multiply. NULL segment fields (flat/legacy)
            # collapse session_cost to energy_cost(energy_kwh, rate) exactly.
            accrued_cost = session_cost(
                SimpleNamespace(
                    rate_coins_per_kwh=rate_coins_per_kwh,
                    settled_cost_coins=settled_cost_coins,
                    rate_segment_start_kwh=rate_segment_start_kwh,
                ),
                energy_kwh,
            )
            if accrued_cost <= 0:
                return
            if hold_coins is not None:
                threshold = hold_coins
            else:
                from backend.database.models import User
                from sqlalchemy import select
                async with self.db_session_factory() as db:
                    user = (await db.execute(
                        select(User).where(User.id == user_id)
                    )).scalar_one_or_none()
                    if user is None:
                        return
                    threshold = user.coin_balance
            if accrued_cost < threshold:
                # Still covered — maybe warn (once per session) as the cost
                # approaches the threshold, so the driver sees the auto-stop
                # coming even with the app closed.
                if (
                    LOW_BALANCE_WARN_FRACTION > 0
                    and float(accrued_cost) >= float(threshold) * LOW_BALANCE_WARN_FRACTION
                    and session_id not in self._low_balance_warned
                ):
                    if len(self._low_balance_warned) > 1000:
                        self._low_balance_warned.clear()
                    self._low_balance_warned.add(session_id)
                    remaining = to_money(threshold - accrued_cost)
                    kwh_left = float(remaining) / COINS_PER_KWH if COINS_PER_KWH else 0.0
                    from backend.services.notifications import notify
                    await notify(
                        user_id,
                        "low_balance",
                        "Balance running low",
                        f"Your current session has used most of your wallet — "
                        f"~{remaining:.2f} coins (≈{kwh_left:.2f} kWh) left before "
                        f"charging auto-stops. Top up to keep charging.",
                        severity="warning",
                        session_id=session_id,
                    )
                return
            # Threshold exhausted — stop through the shared finalize path
            # (own txn; row-locks + re-checks ACTIVE so a concurrent user stop
            # or the reaper settles this exactly once).
            from backend.services.session_lifecycle import finalize_charging_session
            reason = (
                "auto-stopped: session hold exhausted" if hold_coins is not None
                else "auto-stopped: wallet balance exhausted"
            )
            async with self.db_session_factory() as db:
                outcome = await finalize_charging_session(
                    db, session_id, reason=reason
                )
            self._low_balance_warned.discard(session_id)
            if outcome is not None:
                logger.warning(
                    "Auto-stopped session: exhaustion threshold reached",
                    extra={
                        "session_id": session_id,
                        "user_id": user_id,
                        "energy_kwh": outcome["energy_kwh"],
                        "coins_spent": outcome["coins_spent"],
                        "reason": reason,
                    },
                )
        except Exception:
            logger.exception(
                "Balance-exhaustion auto-stop failed",
                extra={"session_id": session_id, "user_id": user_id},
            )

    async def _maybe_auto_stop_on_limits(self, session_id: int, user_id: int, energy_kwh: float,
                                          max_kwh: Optional[float] = None,
                                          max_duration_seconds: Optional[int] = None,
                                          started_at: Optional[datetime] = None):
        """[Session limits] Finalize an ACTIVE session once it reaches ITS OWN
        stop conditions (ChargingSession.max_kwh / max_duration_seconds,
        snapshotted from the start request — routers/sessions.py
        start_charging_session).

        Backend mirror of the firmware's local watchdogs: the gateway gets
        the same limits in the MQTT ON payload and cuts the relay when they
        trip, but publishes NO alarm for those cutoffs — so without this
        check the session would sit ACTIVE (plug pinned OCCUPIED, driver
        unbilled) until the staleness reaper. Telemetry arrives ~every 1 s
        during an active session, so this stops within ~a second of crossing
        the limit — usually BEFORE the firmware cutoff even matters.

        Same contract as _maybe_auto_stop_on_exhaustion above: runs in its
        own txn after the telemetry persist commit, goes through the shared
        finalize path (row-locked ACTIVE re-check → race-safe against a
        concurrent user stop / reaper / the exhaustion auto-stop), and is a
        no-op when disabled or when no limit has been reached. The energy
        limit is checked first: when both trip on one frame, "energy limit
        reached" is the truer reason (energy is measured; elapsed time is
        merely implied by it). NULL limits (legacy sessions predating the
        columns) disable the corresponding check — matching their pre-limit
        behavior exactly. A naive legacy started_at is treated as UTC (same
        convention as gateway_is_live / finalize_charging_session)."""
        if not AUTO_STOP_ON_LIMITS or not self.db_session_factory:
            return

        reason: Optional[str] = None
        if max_kwh is not None and energy_kwh >= max_kwh:
            reason = "auto-stopped: energy limit reached"
        elif max_duration_seconds is not None and started_at is not None:
            started = started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed_sec = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed_sec >= max_duration_seconds:
                reason = "auto-stopped: time limit reached"
        if reason is None:
            return

        try:
            from backend.services.session_lifecycle import finalize_charging_session
            async with self.db_session_factory() as db:
                outcome = await finalize_charging_session(db, session_id, reason=reason)
            if outcome is not None:
                logger.info(
                    "Auto-stopped session: user-set charging limit reached",
                    extra={
                        "session_id": session_id,
                        "user_id": user_id,
                        "energy_kwh": outcome["energy_kwh"],
                        "coins_spent": outcome["coins_spent"],
                        "max_kwh": max_kwh,
                        "max_duration_seconds": max_duration_seconds,
                        "reason": reason,
                    },
                )
        except Exception:
            logger.exception(
                "Charging-limit auto-stop failed",
                extra={"session_id": session_id, "user_id": user_id},
            )

    # -----------------------------------------------------------------------
    # Inbound status handler — updates gateway online/offline state in DB
    # -----------------------------------------------------------------------

    def _handle_gateway_status(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process a gateway status update (online/offline).
        Updates the gateway's status and last_seen_at in the database.
        """
        status = payload.get("status", "offline")
        # Firmware version rides on the `online` status payload ({"fw": "..."}).
        fw = payload.get("fw")
        fw = str(fw)[:32] if fw else None
        logger.info(
            "Gateway status update",
            extra={"gateway_id": gateway_id, "status": status, "fw": fw},
        )

        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_gateway_status(gateway_id, status, fw),
                self.event_loop,
            )

    async def _persist_gateway_status(self, gateway_id: str, status: str, firmware_version: Optional[str] = None):
        """Persist gateway online/offline status (+ reported fw) to the database."""
        from backend.database.models import Gateway, GatewayStatus

        # Whether this message flipped the stored liveness state. Subscriptions
        # re-issue on every connect, so a backend/broker reconnect replays the
        # retained status message; keying the offline-notify and the socket push
        # off a *transition* (rather than the raw status) keeps a retained replay
        # from re-notifying drivers or re-broadcasting (REC-08).
        became_online = False
        became_offline = False
        try:
            async with self.db_session_factory() as session:
                from sqlalchemy import select

                result = await session.execute(
                    select(Gateway).where(Gateway.id == gateway_id)
                )
                gateway = result.scalar_one_or_none()
                if gateway:
                    is_online = status == "online"
                    was_offline = gateway.status == GatewayStatus.OFFLINE
                    became_online = is_online and was_offline
                    became_offline = not is_online and not was_offline
                    gateway.status = GatewayStatus.ONLINE if is_online else GatewayStatus.OFFLINE
                    # Do NOT stamp last_seen_at from a status message: a retained
                    # `online` replayed on reconnect would falsely refresh
                    # liveness for a possibly-wedged gateway. Telemetry — the real
                    # heartbeat — is what stamps last_seen_at (REC-09).
                    # Only overwrite the recorded fw when the payload carried one
                    # (the LWT/offline message has no fw — don't clobber it).
                    if firmware_version:
                        gateway.firmware_version = firmware_version
                    await session.commit()
                    logger.info(
                        "Gateway DB status updated",
                        extra={"gateway_id": gateway_id, "status": status},
                    )
                else:
                    logger.warning(
                        "Gateway not found in DB, ignoring status update",
                        extra={"gateway_id": gateway_id},
                    )
        except Exception as e:
            logger.error(
                "Failed to persist gateway status",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )
            return

        if status == "online":
            await self._republish_off_for_orphaned_plugs(gateway_id)
            # Push the current plug roster (retained) so a gateway that just
            # (re)connected builds/reconciles its slot table without waiting for
            # a command. Fires on every `online` incl. a retained replay — the
            # retained roster makes that idempotent (not gated on became_online).
            await self._publish_roster_for_gateway(gateway_id)
        elif became_offline:
            # Only on a real ONLINE->OFFLINE transition — not a retained replay.
            await self._notify_drivers_gateway_offline(gateway_id)

        # Push connectivity to clients on both transition directions so charger
        # lists flip reachable/unreachable immediately, ahead of the
        # telemetry-timeout path (Faster-offline Lever 1).
        if became_online or became_offline:
            await self._broadcast_plug_connectivity(gateway_id, became_online)

    async def _notify_drivers_gateway_offline(self, gateway_id: str):
        """
        A gateway going offline (LWT) mid-session means telemetry — and with it
        billing and remote stop — is gone; the reaper will finalize the session
        after SESSION_STALE_TIMEOUT_SEC. Tell each affected driver now rather
        than letting them discover a frozen monitor.
        """
        from backend.database.models import ChargingSession, Plug, SessionStatus
        from sqlalchemy import select

        try:
            async with self.db_session_factory() as session:
                rows = (await session.execute(
                    select(ChargingSession.id, ChargingSession.user_id, Plug.id, Plug.name)
                    .join(Plug, ChargingSession.plug_id == Plug.id)
                    .where(
                        Plug.gateway_id == gateway_id,
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                )).all()
        except Exception:
            logger.exception(f"Offline-notification query failed for gateway {gateway_id}")
            return

        from backend.services.notifications import notify
        for session_id, user_id, plug_id, plug_name in rows:
            await notify(
                user_id,
                "charger_offline",
                "Charger connection lost",
                f"{plug_name} went offline during your session. The plug's own "
                f"safety limits still apply; if it doesn't reconnect, the "
                f"session will be closed and billed for the energy recorded "
                f"so far.",
                severity="warning",
                plug_id=plug_id,
                session_id=session_id,
            )

    async def _broadcast_plug_connectivity(self, gateway_id: str, gateway_online: bool):
        """
        On a gateway online<->offline transition, emit plug_connectivity for each
        of its plugs so the frontend flags/clears the plug as unreachable at once,
        without waiting on the telemetry-timeout path (Faster-offline Lever 1).
        """
        from backend.database.models import Plug
        from sqlalchemy import select

        try:
            async with self.db_session_factory() as session:
                plug_ids = (await session.execute(
                    select(Plug.id).where(Plug.gateway_id == gateway_id)
                )).scalars().all()
        except Exception:
            logger.exception(f"plug_connectivity query failed for gateway {gateway_id}")
            return

        from backend.services.socketio_manager import emit_plug_connectivity
        for plug_id in plug_ids:
            await emit_plug_connectivity(plug_id, gateway_online)

    async def _republish_off_for_orphaned_plugs(self, gateway_id: str):
        """
        On gateway reconnect, re-send OFF to each of its plugs that has no
        ACTIVE session. OFF commands aren't retained, so a gateway that was
        dead when its session got finalized (e.g. by the session reaper) never
        received one — and its NVS crash recovery resumes the session on
        reboot with the relay ON and nobody billing (observed 2026-07-07).
        Idempotent: an OFF to an already-off plug is a no-op on the firmware.

        [Queued charge] A WAITING queued charge is NOT an ACTIVE session, so
        Scenario B (gateway + plug both lost power, power returns, the Tapo
        auto-resumes its relay, the gateway reboots and reconnects) would see
        "no ACTIVE session" and OFF the very plug the debounced auto-start is
        pending on. Skip a plug that has a live WAITING queued charge which is
        still MID-DEBOUNCE (not powered yet, or powered for < auto_start_delay):
        it lost power so it's already off, and re-OFFing it here would fight the
        pending auto-start. Leave it OFF and let the queue sweep
        (services/session_reaper.py reap_queued_starts_once) own the actual
        energize — a proper start with a hold, caps, and a fresh session_id once
        the debounce elapses. Plugs with no queued charge (and eligible/past-
        debounce ones the sweep is about to start) keep the existing orphan-OFF.
        """
        from backend.database.models import (
            ChargingSession, Plug, QueuedCharge, QueuedChargeStatus, SessionStatus,
            Tenant,
        )
        from backend.services.session_lifecycle import plug_is_powered
        from backend.services.session_start import auto_start_delay

        try:
            async with self.db_session_factory() as session:
                from datetime import datetime, timedelta, timezone
                from sqlalchemy import select

                plug_result = await session.execute(
                    select(Plug.id, Plug.local_ip).where(Plug.gateway_id == gateway_id)
                )
                # {plug_id: local_ip} — the OFF carries local_ip so a rebooted
                # multi-plug gateway can learn the plug and actuate it (TD#20).
                plug_ips = {pid: ip for pid, ip in plug_result.all()}
                if not plug_ips:
                    return

                active_result = await session.execute(
                    select(ChargingSession.plug_id).where(
                        ChargingSession.plug_id.in_(list(plug_ips.keys())),
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                )
                active_plug_ids = set(active_result.scalars().all())

                # [Queued charge] Plugs with a live WAITING queued charge that is
                # still mid-debounce — leave these OFF for the queue sweep to
                # energize properly, don't orphan-OFF them here. Computed BEFORE
                # the synchronous publish loop so no await splits the snapshot.
                now = datetime.now(timezone.utc)
                queued_rows = await session.execute(
                    select(Plug, Tenant)
                    .join(QueuedCharge, QueuedCharge.plug_id == Plug.id)
                    .join(Tenant, Tenant.id == QueuedCharge.tenant_id)
                    .where(
                        QueuedCharge.plug_id.in_(list(plug_ips.keys())),
                        QueuedCharge.status == QueuedChargeStatus.WAITING,
                    )
                )
                queued_hold_plug_ids = set()
                for plug, tenant in queued_rows.all():
                    powered_since = plug.powered_since
                    if powered_since is not None and powered_since.tzinfo is None:
                        powered_since = powered_since.replace(tzinfo=timezone.utc)
                    mid_debounce = (
                        not plug_is_powered(plug, now)
                        or powered_since is None
                        or (now - powered_since)
                        < timedelta(minutes=auto_start_delay(tenant, plug))
                    )
                    if mid_debounce:
                        queued_hold_plug_ids.add(plug.id)

                # [REC-06] Publish each OFF INSIDE the same session, with no
                # await between the ACTIVE snapshot above and these synchronous
                # publishes: that leaves no yield point for a session started in
                # the gap to commit and then be wrongly killed. (Previously the
                # snapshot was taken, the session closed — an await boundary —
                # and only then were OFFs published, racing a fresh start.)
                for plug_id, local_ip in plug_ips.items():
                    if plug_id not in active_plug_ids and plug_id not in queued_hold_plug_ids:
                        # wait=False: we're on the event loop — don't block it on
                        # the broker ack for a best-effort cleanup publish.
                        self.send_plug_command(gateway_id, plug_id, "OFF", local_ip=local_ip, wait=False)
                        logger.info(
                            "Republished OFF on reconnect (no ACTIVE session)",
                            extra={"gateway_id": gateway_id, "plug_id": plug_id},
                        )
        except Exception as e:
            logger.error(
                "OFF republish on reconnect failed",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )

    # -----------------------------------------------------------------------
    # Inbound plug-discovery handler — auto-populate agent-discovered plugs
    # -----------------------------------------------------------------------

    def _handle_gateway_plug_discovery(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process a plug-discovery announcement from an AmpHive Agent.

        Expected payload (docs/AMPHIVE_AGENT.md):
            {"unique_id": "kasa:AA:BB:..", "provider": "kasa",
             "model": "KP115", "alias": "Bay 3", "capabilities": ["switch","power","energy"]}

        The backend is authoritative for plug_id: it upserts a Plug keyed by the
        stable `unique_id` (the DB assigns `plugs.id`), then publishes the current
        {unique_id: plug_id} map back so the agent adopts the assigned ids. This
        is required because the MQTT plug_id IS the global `plugs.id` used by the
        telemetry handler — an agent must not invent its own.
        """
        unique_id = payload.get("unique_id")
        if not unique_id:
            logger.warning(
                "Discovery missing unique_id, ignoring",
                extra={"gateway_id": gateway_id},
            )
            return
        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_plug_discovery(gateway_id, payload),
                self.event_loop,
            )

    async def _persist_plug_discovery(self, gateway_id: str, payload: Dict[str, Any]):
        """Upsert the discovered plug by unique_id, then publish the assignment map."""
        from backend.database.models import Gateway, Plug
        from sqlalchemy import select

        unique_id = payload.get("unique_id")
        alias = str(payload.get("alias") or unique_id)[:100]
        model = str(payload.get("model") or "agent")[:50]
        local_ip = str(payload.get("ip") or "agent")[:45]

        try:
            async with self.db_session_factory() as session:
                # Only auto-populate for a gateway that already exists (is claimed).
                gateway = (
                    await session.execute(select(Gateway).where(Gateway.id == gateway_id))
                ).scalar_one_or_none()
                if gateway is None:
                    logger.warning(
                        "Discovery for unknown gateway, ignoring (claim the gateway first)",
                        extra={"gateway_id": gateway_id},
                    )
                    return

                existing = (
                    await session.execute(
                        select(Plug).where(
                            Plug.gateway_id == gateway_id, Plug.unique_id == unique_id
                        )
                    )
                ).scalar_one_or_none()

                if existing is None:
                    session.add(Plug(
                        gateway_id=gateway_id, name=alias, local_ip=local_ip,
                        plug_model=model, unique_id=unique_id,
                    ))
                    logger.info(
                        "Auto-populated new plug from discovery",
                        extra={"gateway_id": gateway_id, "unique_id": unique_id, "alias": alias},
                    )
                else:
                    existing.name = alias
                    existing.plug_model = model

                await session.commit()

                # Rebuild the full {unique_id: plug_id} map for this gateway.
                rows = (
                    await session.execute(
                        select(Plug.unique_id, Plug.id).where(
                            Plug.gateway_id == gateway_id, Plug.unique_id.is_not(None)
                        )
                    )
                ).all()
                assignments = {uid: pid for uid, pid in rows}
        except Exception as e:
            logger.error(
                "Failed to persist plug discovery",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )
            return

        # Hand the authoritative ids back to the agent (retained, so it survives
        # an agent restart / late subscribe).
        self.client.publish(
            f"amphive/gateways/{gateway_id}/assign",
            json.dumps(assignments), qos=1, retain=True,
        )
        logger.info(
            "Published plug assignments",
            extra={"gateway_id": gateway_id, "assignments": assignments},
        )
        # Keep the ESP-consumed roster in sync with agent-discovered plugs.
        await self._publish_roster_for_gateway(gateway_id)

    # -----------------------------------------------------------------------
    # Plug roster (amphive/gateways/{gw}/config) — backend-pushed, retained
    # -----------------------------------------------------------------------

    def publish_plug_roster(self, gateway_id: str, plugs: list) -> None:
        """
        Publish the retained per-gateway plug roster on
        `amphive/gateways/{gw}/config` so the firmware builds its slot table
        proactively — add / re-IP / remove — instead of learning each plug
        lazily from the `local_ip` on an ON/OFF command (and instead of a single
        plug IP baked in at captive-portal setup).

        `plugs` is a list of `{plug_id, local_ip, max_current_a}` dicts
        (`max_current_a` may be None → firmware uses its default cap; the plug's
        display name is deliberately omitted — the firmware slot has no name and
        4 long names could overflow the device's 512-byte inbound buffer). Empty
        list is a valid roster (a gateway with no plugs → free all non-active
        slots on-device). Retained, so a rebooting gateway gets the current
        roster the instant it subscribes. Mirrors the retained `assign` publish;
        fire-and-forget (no wait_for_publish). The topic sits under the gateway's
        own subtree, so the existing `pattern readwrite amphive/gateways/%u/#`
        ACL already permits the gateway to subscribe it — no ACL change (SEC §8.5).
        """
        if self.client is None:
            return
        payload = {"v": 1, "plugs": plugs}
        self.client.publish(
            f"amphive/gateways/{gateway_id}/config",
            json.dumps(payload), qos=1, retain=True,
        )
        logger.info(
            "Published plug roster",
            extra={"gateway_id": gateway_id, "plug_count": len(plugs)},
        )

    async def _publish_roster_for_gateway(self, gateway_id: str) -> None:
        """Load a gateway's plugs and publish the retained roster (see
        `publish_plug_roster`). Used on gateway (re)connect and after a discovery
        upsert; the CPO plug-CRUD path builds the roster from its own request
        session instead (routers/cpo.py `_publish_gateway_roster`)."""
        from backend.database.models import Plug
        from sqlalchemy import select

        try:
            async with self.db_session_factory() as session:
                rows = (await session.execute(
                    select(Plug.id, Plug.local_ip, Plug.max_current_a)
                    .where(Plug.gateway_id == gateway_id)
                )).all()
            roster = [
                {"plug_id": pid, "local_ip": ip, "max_current_a": cap}
                for pid, ip, cap in rows
            ]
            self.publish_plug_roster(gateway_id, roster)
        except Exception as e:
            logger.error(
                "Failed to publish plug roster",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )

    # -----------------------------------------------------------------------
    # Outbound command publisher
    # -----------------------------------------------------------------------

    def send_plug_command(self, gateway_id: str, plug_id: int, action: str, max_duration: int = 14400, max_kwh: float = 30.0, session_id: Optional[int] = None, local_ip: Optional[str] = None, max_current_a: Optional[float] = None, wait: bool = True) -> bool:
        """
        Sends an ON/OFF command to a specific plug registered under a gateway.
        Topic: amphive/gateways/{gateway_id}/plugs/{plug_id}/commands
        Payload: {"action": "ON"/"OFF", "max_duration_seconds": X, "max_kwh": Y}

        When `session_id` is given (session start), it is included as a string.
        The firmware persists it for crash recovery and echoes it back in
        telemetry so the backend can attribute a reading to the exact session
        rather than just "the active session on this plug".

        When `max_current_a` is given (session start ON), it is included as the
        plug's effective current cap (amps) for on-device enforcement — the plug
        measures real current and can trip its relay if it exceeds this. The
        caller resolves it via services/caps.py effective_plug_cap (the plug's
        own cap, or DEFAULT_PLUG_CAP_A). Older firmware ignores the extra field,
        so this is backward-safe; OFF/cleanup publishes omit it.

        When `local_ip` is given, it is included so a multi-plug gateway
        (TD#20) knows which physical plug to actuate — and can learn a plug it
        hasn't seen before (e.g. after a reboot) without a static on-device
        roster. The DB is the source of truth for `plugs.local_ip`; pass it on
        every ON/OFF. Older single-plug firmware ignores the extra field and
        falls back to its provisioned target plug, so this is backward-safe.

        `wait=False` skips the blocking wait for the broker ack — for
        best-effort publishes issued from the event loop (blocking it up to
        3 s per publish would stall every other coroutine).
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {
            "action": action.upper(),
            "max_duration_seconds": max_duration,
            "max_kwh": max_kwh
        }
        if session_id is not None:
            payload["session_id"] = str(session_id)
        if local_ip:
            payload["local_ip"] = local_ip
        if max_current_a is not None:
            payload["max_current_a"] = max_current_a

        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            if not wait:
                logger.info(
                    "Published plug command (no-wait)",
                    extra={
                        "gateway_id": gateway_id, "plug_id": plug_id,
                        "action": payload["action"], "topic": topic,
                    },
                )
                return info.rc == mqtt.MQTT_ERR_SUCCESS
            info.wait_for_publish(timeout=3.0)
            logger.info(
                "Published plug command",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "action": payload["action"], "topic": topic, "session_id": session_id,
                },
            )
            return info.is_published()
        except Exception as e:
            logger.error(
                "Failed to publish plug command",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "topic": topic, "error": str(e)},
            )
            return False

    def send_gateway_ota(self, gateway_id: str, plug_id: int, firmware_url: str) -> bool:
        """
        Trigger an OTA firmware update on a gateway.

        The firmware subscribes only to the per-plug command topic
        (amphive/gateways/{gw}/plugs/+/commands), so the OTA command rides one
        of the gateway's plug topics; the firmware ignores the plug_id for
        OTA (it's a gateway-scoped action) and does not touch active_plug_id.
        Payload: {"action": "OTA", "url": "<http(s) url>"}. The gateway
        refuses the update while a session is active and reboots into the
        passive slot on success (rollback-protected).
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {"action": "OTA", "url": firmware_url}
        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            info.wait_for_publish(timeout=3.0)
            logger.info(
                "Published OTA command",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "firmware_url": firmware_url, "topic": topic,
                },
            )
            return info.is_published()
        except Exception as e:
            logger.error(
                "Failed to publish OTA command",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "topic": topic, "error": str(e)},
            )
            return False

    def send_plug_interval(self, gateway_id: str, plug_id: int, interval_ms: int) -> bool:
        """
        Sends a SET_INTERVAL command to a specific plug registered under a gateway.
        Topic: amphive/gateways/{gateway_id}/plugs/{plug_id}/commands
        Payload: {"action": "SET_INTERVAL", "interval_ms": X}
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {
            "action": "SET_INTERVAL",
            "interval_ms": interval_ms
        }

        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            info.wait_for_publish(timeout=3.0)
            logger.info(
                "Published interval command",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "interval_ms": interval_ms, "topic": topic,
                },
            )
            return info.is_published()
        except Exception as e:
            logger.error(
                "Failed to publish interval command",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "topic": topic, "error": str(e)},
            )
            return False

    def send_plug_limits(self, gateway_id: str, plug_id: int, max_kwh: float, max_duration_seconds: int, local_ip: Optional[str] = None, max_current_a: Optional[float] = None) -> bool:
        """
        Sends a SET_LIMITS command to a specific plug registered under a gateway.
        Topic: amphive/gateways/{gateway_id}/plugs/{plug_id}/commands
        Payload: {"action": "SET_LIMITS", "max_kwh": X, "max_duration_seconds": Y, "local_ip": "<str>", "max_current_a": Z}

        Updates a RUNNING session's watchdog thresholds (energy + duration caps)
        in place. Unlike ON, the firmware does **not** re-read the meter baseline
        or touch start_energy_kwh/start_time_s/session_active/session_id — so
        billing is unaffected. The firmware ignores it when no session is active.
        `local_ip` targets the physical plug on a multi-plug gateway (TD#20),
        mirroring ON/OFF; the empty-string default is harmless for older
        single-plug firmware, which falls back to its provisioned target.

        When `max_current_a` is given, the firmware re-arms the running
        session's OVERCURRENT_CAP watchdog at it (and resets the debounce);
        omitted (None), the key is left out of the payload and the on-device
        cap is untouched — mirroring the ON path's optional cap. The caller
        resolves it via services/caps.py effective_plug_cap. Older firmware
        ignores the extra field, so this is backward-safe.
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {
            "action": "SET_LIMITS",
            "max_kwh": max_kwh,
            "max_duration_seconds": max_duration_seconds,
            "local_ip": local_ip or "",
        }
        if max_current_a is not None:
            payload["max_current_a"] = max_current_a

        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            info.wait_for_publish(timeout=3.0)
            logger.info(
                "Published limits command",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "max_kwh": max_kwh, "max_duration_seconds": max_duration_seconds,
                    "topic": topic,
                },
            )
            return info.is_published()
        except Exception as e:
            logger.error(
                "Failed to publish limits command",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "topic": topic, "error": str(e)},
            )
            return False
