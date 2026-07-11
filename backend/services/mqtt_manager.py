import asyncio
import functools
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Any, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger("amphive.mqtt")
logging.basicConfig(level=logging.INFO)

# Telemetry refreshes gateways.last_seen_at (the liveness signal that gates
# session starts) at most this often per gateway — it arrives every ~1-10 s
# and each refresh is a DB write.
GATEWAY_SEEN_BUMP_INTERVAL_SEC = 60.0

# Prepaid protection: auto-stop a session once its accrued energy cost reaches
# the driver's wallet balance, so a drained wallet can't keep charging for free
# (the finalize path clamps the debit but doesn't end the session on its own).
# Env-toggleable; on by default.
AUTO_STOP_ON_BALANCE_EXHAUSTED = os.getenv("AUTO_STOP_ON_BALANCE_EXHAUSTED", "true").lower() in ("1", "true", "yes")


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
        logger.info(f"Connecting to MQTT broker at {self.broker_host}:{self.broker_port}...")
        self.client.connect_async(self.broker_host, self.broker_port, keepalive=60)
        self.client.loop_start()

    def stop(self):
        logger.info("Stopping MQTT client loop...")
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("Connected successfully to MQTT Broker.")
            # Subscribe to all gateway telemetry and status topics
            self.client.subscribe("amphive/gateways/+/telemetry", qos=0)
            self.client.subscribe("amphive/gateways/+/status", qos=1)
            # AmpHive Agent plug discovery (auto-populate) — retained announcements.
            self.client.subscribe("amphive/gateways/+/discovery", qos=1)
            # Firmware safety alarms (THERMAL/OVERCURRENT/UNAUTHORIZED_ON) + OTA
            # lifecycle events — persisted as GatewayEvents and surfaced to CPOs.
            self.client.subscribe("amphive/gateways/+/alarms", qos=1)
        else:
            logger.error(f"Failed to connect to MQTT broker, return code {rc}")

    def _on_disconnect(self, client, userdata, rc, properties=None, reason_code=None):
        logger.warning(f"Disconnected from MQTT broker with code: {rc}")

    def _on_message(self, client, userdata, msg):
        payload_str = msg.payload.decode("utf-8", errors="ignore")
        logger.debug(f"Received message on topic {msg.topic}: {payload_str}")
        
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON payload on topic {msg.topic}: {payload_str}")
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
            logger.warning(f"Telemetry from gateway {gateway_id} missing plug_id, ignoring.")
            return
        try:
            plug_id = int(plug_id)
        except (TypeError, ValueError):
            logger.warning(
                f"Telemetry from gateway {gateway_id} has non-integer plug_id "
                f"{payload.get('plug_id')!r}, ignoring."
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
                f"Telemetry from gateway {gateway_id} plug {plug_id} has "
                f"non-numeric fields, ignoring: {payload}"
            )
            return
        if not all(math.isfinite(v) for v in (watts, kwh, voltage, current)):
            logger.warning(
                f"Telemetry from gateway {gateway_id} plug {plug_id} has "
                f"non-finite values, ignoring: {payload}"
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

        # Map firmware status to telemetry store status
        telem_status = "charging" if status == "occupied" else "idle"

        logger.info(
            f"Telemetry from gw={gateway_id} plug={plug_id}: "
            f"{watts:.1f}W, {kwh:.3f}kWh, {current:.1f}A, {voltage:.0f}V [{status}]"
        )

        # --- 1. Feed the in-memory TelemetryStore (for the live stream) ---
        # This callback runs on the paho network thread. TelemetryStore.update()
        # signals asyncio.Events that live on the server's event loop, and
        # asyncio.Event.set() is NOT thread-safe when called from another thread —
        # it can fail to wake stream() waiters or corrupt loop state. Marshal the
        # update onto the loop so the whole store stays single-threaded.
        # (cost_coins is left to TelemetryStore to auto-calc via COINS_PER_KWH.)
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
                self._persist_telemetry(gateway_id, plug_id, watts, kwh, session_id, sample),
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
            logger.error(f"Failed to refresh last_seen_at for gateway {gateway_id}: {e}")

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
            logger.warning(f"Alarm from gateway {gateway_id} missing error/event: {payload}")
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
            f"ALARM from gw={gateway_id} plug={plug_id}: {event_type} ({severity})"
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
                    logger.warning(f"Alarm for unknown gateway {gateway_id}; dropping event.")
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
            logger.error(f"Failed to persist gateway event for {gateway_id}: {e}")
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
            logger.error(f"Failed to broadcast gateway alarm for {gateway_id}: {e}")

    async def _persist_telemetry(self, gateway_id: str, plug_id: int, watts: float, kwh: float,
                                 session_id: Optional[int] = None,
                                 sample: Optional[Dict[str, Any]] = None):
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

        # Captured for the post-commit balance check (see below).
        updated_session_id: Optional[int] = None
        updated_user_id: Optional[int] = None

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
                    owner = "does not exist" if plug is None else f"belongs to gateway {plug.gateway_id}"
                    logger.warning(
                        f"Telemetry from gateway {gateway_id} claims plug {plug_id}, "
                        f"which {owner} — dropping reading."
                    )
                    return

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
                sess_result = await session.execute(
                    select(ChargingSession).where(where_clause)
                )
                active_session = sess_result.scalar_one_or_none()
                if active_session:
                    active_session.energy_kwh = kwh
                    # Track peak power — only update if this reading is higher
                    if watts > active_session.peak_power_w:
                        active_session.peak_power_w = watts
                    # Staleness signal read by the session reaper.
                    active_session.last_telemetry_at = datetime.now(timezone.utc)
                    updated_session_id = active_session.id
                    updated_user_id = active_session.user_id

                await session.commit()
        except Exception as e:
            logger.error(f"Failed to persist telemetry for plug {plug_id}: {e}")
            return

        # Prepaid protection: if the accrued energy cost has reached the driver's
        # wallet balance, auto-stop so they can't keep charging for free past a
        # drained wallet (the finalize path only clamps the debit — it doesn't
        # stop the session). Done in a separate txn after the persist commit.
        if updated_session_id is not None and updated_user_id is not None:
            await self._maybe_auto_stop_on_exhaustion(updated_session_id, updated_user_id, kwh)

    async def _maybe_auto_stop_on_exhaustion(self, session_id: int, user_id: int, energy_kwh: float):
        """Finalize an ACTIVE session once its accrued cost meets/exceeds the
        user's wallet balance. No-op when disabled, when the wallet still
        covers the energy, or when the session is already gone (finalize
        re-checks ACTIVE under a row lock, so this is race-safe)."""
        if not AUTO_STOP_ON_BALANCE_EXHAUSTED or not self.db_session_factory:
            return
        from backend.database.models import User
        from backend.services.money import to_money
        from backend.services.telemetry import COINS_PER_KWH
        from sqlalchemy import select

        try:
            accrued_cost = to_money(energy_kwh * COINS_PER_KWH)
            if accrued_cost <= 0:
                return
            async with self.db_session_factory() as db:
                user = (await db.execute(
                    select(User).where(User.id == user_id)
                )).scalar_one_or_none()
                if user is None or accrued_cost < user.coin_balance:
                    return
            # Wallet is exhausted — stop through the shared finalize path
            # (own txn; row-locks + re-checks ACTIVE so a concurrent user stop
            # or the reaper settles this exactly once).
            from backend.services.session_lifecycle import finalize_charging_session
            async with self.db_session_factory() as db:
                outcome = await finalize_charging_session(
                    db, session_id, reason="auto-stopped: wallet balance exhausted"
                )
            if outcome is not None:
                logger.warning(
                    f"Auto-stopped session {session_id} (user {user_id}): wallet exhausted "
                    f"at {outcome['energy_kwh']} kWh / {outcome['coins_spent']} coins."
                )
        except Exception:
            logger.exception(f"Balance-exhaustion auto-stop failed for session {session_id}")

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
        logger.info(f"Gateway {gateway_id} status: {status}" + (f" fw={fw}" if fw else ""))

        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_gateway_status(gateway_id, status, fw),
                self.event_loop,
            )

    async def _persist_gateway_status(self, gateway_id: str, status: str, firmware_version: Optional[str] = None):
        """Persist gateway online/offline status (+ reported fw) to the database."""
        from backend.database.models import Gateway, GatewayStatus

        try:
            async with self.db_session_factory() as session:
                from sqlalchemy import select

                result = await session.execute(
                    select(Gateway).where(Gateway.id == gateway_id)
                )
                gateway = result.scalar_one_or_none()
                if gateway:
                    gateway.status = GatewayStatus.ONLINE if status == "online" else GatewayStatus.OFFLINE
                    gateway.last_seen_at = datetime.now(timezone.utc)
                    # Only overwrite the recorded fw when the payload carried one
                    # (the LWT/offline message has no fw — don't clobber it).
                    if firmware_version:
                        gateway.firmware_version = firmware_version
                    await session.commit()
                    logger.info(f"Gateway {gateway_id} DB status updated to {status}")
                else:
                    logger.warning(f"Gateway {gateway_id} not found in DB, ignoring status update.")
        except Exception as e:
            logger.error(f"Failed to persist gateway status for {gateway_id}: {e}")
            return

        if status == "online":
            await self._republish_off_for_orphaned_plugs(gateway_id)

    async def _republish_off_for_orphaned_plugs(self, gateway_id: str):
        """
        On gateway reconnect, re-send OFF to each of its plugs that has no
        ACTIVE session. OFF commands aren't retained, so a gateway that was
        dead when its session got finalized (e.g. by the session reaper) never
        received one — and its NVS crash recovery resumes the session on
        reboot with the relay ON and nobody billing (observed 2026-07-07).
        Idempotent: an OFF to an already-off plug is a no-op on the firmware.
        """
        from backend.database.models import ChargingSession, Plug, SessionStatus

        try:
            async with self.db_session_factory() as session:
                from sqlalchemy import select

                plug_result = await session.execute(
                    select(Plug.id).where(Plug.gateway_id == gateway_id)
                )
                plug_ids = list(plug_result.scalars().all())
                if not plug_ids:
                    return

                active_result = await session.execute(
                    select(ChargingSession.plug_id).where(
                        ChargingSession.plug_id.in_(plug_ids),
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                )
                active_plug_ids = set(active_result.scalars().all())

            for plug_id in plug_ids:
                if plug_id not in active_plug_ids:
                    # wait=False: we're on the event loop — don't block it on
                    # the broker ack for a best-effort cleanup publish.
                    self.send_plug_command(gateway_id, plug_id, "OFF", wait=False)
                    logger.info(
                        f"Republished OFF to gw={gateway_id} plug={plug_id} "
                        f"on reconnect (no ACTIVE session)"
                    )
        except Exception as e:
            logger.error(f"OFF republish on reconnect failed for gateway {gateway_id}: {e}")

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
            logger.warning(f"Discovery from gateway {gateway_id} missing unique_id, ignoring.")
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
                        f"Discovery for unknown gateway {gateway_id}, ignoring "
                        f"(claim the gateway first)."
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
                    logger.info(f"Auto-populated new plug '{alias}' ({unique_id}) on {gateway_id}")
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
            logger.error(f"Failed to persist plug discovery for {gateway_id}: {e}")
            return

        # Hand the authoritative ids back to the agent (retained, so it survives
        # an agent restart / late subscribe).
        self.client.publish(
            f"amphive/gateways/{gateway_id}/assign",
            json.dumps(assignments), qos=1, retain=True,
        )
        logger.info(f"Published plug assignments for {gateway_id}: {assignments}")

    # -----------------------------------------------------------------------
    # Outbound command publisher
    # -----------------------------------------------------------------------

    def send_plug_command(self, gateway_id: str, plug_id: int, action: str, max_duration: int = 14400, max_kwh: float = 30.0, session_id: Optional[int] = None, wait: bool = True) -> bool:
        """
        Sends an ON/OFF command to a specific plug registered under a gateway.
        Topic: amphive/gateways/{gateway_id}/plugs/{plug_id}/commands
        Payload: {"action": "ON"/"OFF", "max_duration_seconds": X, "max_kwh": Y}

        When `session_id` is given (session start), it is included as a string.
        The firmware persists it for crash recovery and echoes it back in
        telemetry so the backend can attribute a reading to the exact session
        rather than just "the active session on this plug".

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

        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            if not wait:
                logger.info(f"Published command (no-wait) to {topic}: {payload_str}")
                return info.rc == mqtt.MQTT_ERR_SUCCESS
            info.wait_for_publish(timeout=3.0)
            logger.info(f"Published command to {topic}: {payload_str}")
            return info.is_published()
        except Exception as e:
            logger.error(f"Failed to publish command to {topic}: {e}")
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
            logger.info(f"Published OTA command to {topic}: {payload_str}")
            return info.is_published()
        except Exception as e:
            logger.error(f"Failed to publish OTA command to {topic}: {e}")
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
            logger.info(f"Published interval command to {topic}: {payload_str}")
            return info.is_published()
        except Exception as e:
            logger.error(f"Failed to publish interval command to {topic}: {e}")
            return False

