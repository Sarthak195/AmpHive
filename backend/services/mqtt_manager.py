import json
import logging
import re
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
        db_session_factory: Optional[Callable] = None,
    ):
        # Prevent re-initialization if already initialized
        if hasattr(self, "client"):
            return
        
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password
        self.db_session_factory = db_session_factory
        
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

    def _handle_gateway_status(self, gateway_id: str, payload: Dict[str, Any]):
        status = payload.get("status", "offline")
        logger.info(f"Gateway {gateway_id} status updated to: {status}")
        
        if not self.db_session_factory:
            return
            
        # Synchronous database update helper (or run async code in thread)
        # For simplicity in the boilerplate, we log this. In production, this updates the `gateways` table status.
        # We can implement an async worker or execute the query here:
        # e.g., session.execute(update(Gateway).where(Gateway.id == gateway_id).values(status=status))

    def _handle_gateway_telemetry(self, gateway_id: str, payload: Dict[str, Any]):
        # Expected telemetry schema:
        # {
        #   "plug_id": 1,
        #   "watts": 1200.5,
        #   "kwh": 34.56,
        #   "status": "occupied" / "available"
        # }
        plug_id = payload.get("plug_id")
        watts = payload.get("watts", 0.0)
        kwh = payload.get("kwh", 0.0)
        status = payload.get("status", "offline")
        
        logger.debug(f"Telemetry from gateway {gateway_id}, plug {plug_id}: {watts} W, {kwh} kWh, Status: {status}")
        
        # Here we would update the `plugs` table and append a snapshot to the database
        # if the session is active to calculate cumulative consumption.

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
