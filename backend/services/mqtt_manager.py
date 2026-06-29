import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Callable, Dict, Any, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger("amphive.mqtt")
logging.basicConfig(level=logging.INFO)


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
            "status": "occupied" | "available"
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

        # Map firmware status to telemetry store status
        telem_status = "charging" if status == "occupied" else "idle"

        logger.info(
            f"Telemetry from gw={gateway_id} plug={plug_id}: "
            f"{watts:.1f}W, {kwh:.3f}kWh, {current:.1f}A, {voltage:.0f}V [{status}]"
        )

        # --- 1. Feed the in-memory TelemetryStore (for SSE streaming) ---
        if self.telemetry_store:
            self.telemetry_store.update(
                plug_id=plug_id,
                power_w=watts,
                current_a=current,
                energy_kwh=kwh,
                status=telem_status,
                # cost_coins=None → auto-calculated by TelemetryStore using COINS_PER_KWH
            )

        # --- 2. Persist to the database (async, fire-and-forget) ---
        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_telemetry(plug_id, watts, kwh),
                self.event_loop,
            )

    async def _persist_telemetry(self, plug_id: int, watts: float, kwh: float):
        """
        Persist the latest telemetry snapshot to the database:
        - Update `plugs.current_power_w` so the plug list shows real-time power.
        - Update the active `charging_sessions` row with cumulative energy and
          peak power, so that even if the server crashes, the last-known values
          are saved.
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

                # Find the active session for this plug and update it
                sess_result = await session.execute(
                    select(ChargingSession).where(
                        and_(
                            ChargingSession.plug_id == plug_id,
                            ChargingSession.status == SessionStatus.ACTIVE,
                        )
                    )
                )
                active_session = sess_result.scalar_one_or_none()
                if active_session:
                    active_session.energy_kwh = kwh
                    # Track peak power — only update if this reading is higher
                    if watts > active_session.peak_power_w:
                        active_session.peak_power_w = watts

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

    # -----------------------------------------------------------------------
    # Outbound command publisher
    # -----------------------------------------------------------------------

    def send_plug_command(self, gateway_id: str, plug_id: int, action: str, max_duration: int = 14400, max_kwh: float = 30.0) -> bool:
        """
        Sends an ON/OFF command to a specific plug registered under a gateway.
        Topic: amphive/gateways/{gateway_id}/plugs/{plug_id}/commands
        Payload: {"action": "ON"/"OFF", "max_duration_seconds": X, "max_kwh": Y}
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {
            "action": action.upper(),
            "max_duration_seconds": max_duration,
            "max_kwh": max_kwh
        }
        
        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            info.wait_for_publish(timeout=3.0)
            logger.info(f"Published command to {topic}: {payload_str}")
            return info.is_published()
        except Exception as e:
            logger.error(f"Failed to publish command to {topic}: {e}")
            return False
