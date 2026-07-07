import asyncio
import json
import logging
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

        watts = float(payload.get("watts", 0.0))
        kwh = float(payload.get("kwh", 0.0))
        voltage = float(payload.get("voltage", 230.0))
        current = float(payload.get("current", 0.0))
        status = payload.get("status", "occupied")

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
                self.telemetry_store.update,
                plug_id, watts, current, kwh, telem_status,
            )
        elif self.telemetry_store:
            # No loop reference (e.g. unit tests): safe to call directly.
            self.telemetry_store.update(
                plug_id=plug_id,
                power_w=watts,
                current_a=current,
                energy_kwh=kwh,
                status=telem_status,
            )

        # --- 2. Enqueue a raw sample for time-series persistence ---
        # Buffered + batch-flushed by TelemetryPersistenceService. This is where
        # voltage/current/status (parsed above but not used for session totals)
        # get persisted to telemetry_readings.
        if self.telemetry_persistence:
            self.telemetry_persistence.enqueue({
                "plug_id": plug_id,
                "recorded_at": datetime.now(timezone.utc),
                "power_w": watts,
                "energy_kwh": kwh,
                "voltage_v": voltage,
                "current_a": current,
                "status": status,
            })

        # --- 3. Persist authoritative session totals (async, fire-and-forget) ---
        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_telemetry(plug_id, watts, kwh, session_id),
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

    async def _persist_telemetry(self, plug_id: int, watts: float, kwh: float, session_id: Optional[int] = None):
        """
        Persist the latest telemetry snapshot to the database:
        - Update `plugs.current_power_w` so the plug list shows real-time power.
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

        try:
            async with self.db_session_factory() as session:
                from sqlalchemy import select, and_

                # Update plug's current power reading
                plug_result = await session.execute(
                    select(Plug).where(Plug.id == plug_id)
                )
                plug = plug_result.scalar_one_or_none()
                if plug:
                    plug.current_power_w = watts

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

                await session.commit()
        except Exception as e:
            logger.error(f"Failed to persist telemetry for plug {plug_id}: {e}")

    # -----------------------------------------------------------------------
    # Inbound status handler — updates gateway online/offline state in DB
    # -----------------------------------------------------------------------

    def _handle_gateway_status(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process a gateway status update (online/offline).
        Updates the gateway's status and last_seen_at in the database.
        """
        status = payload.get("status", "offline")
        logger.info(f"Gateway {gateway_id} status: {status}")
        
        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_gateway_status(gateway_id, status),
                self.event_loop,
            )

    async def _persist_gateway_status(self, gateway_id: str, status: str):
        """Persist gateway online/offline status to the database."""
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

