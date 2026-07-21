"""
MQTTManager collaborator: broker connection lifecycle.

Extracted verbatim from services/mqtt_manager.py (god-object split) — start/
stop the paho client loop and the on_connect/on_disconnect callbacks that
(re)establish the gateway subscriptions. Mixed into MQTTManager; see
services/mqtt/__init__.py for why this is a mixin rather than a delegating
collaborator object.
"""
import logging

logger = logging.getLogger("amphive.mqtt")


class MQTTConnectionMixin:
    """Paho client connect/subscribe/disconnect lifecycle."""

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
