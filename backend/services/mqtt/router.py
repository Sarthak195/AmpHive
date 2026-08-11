"""
MQTTManager collaborator: inbound topic parsing/routing.

Extracted verbatim from services/mqtt_manager.py (god-object split). Matches
the raw paho message topic against the gateway telemetry/status/discovery/
alarm regexes (compiled in MQTTManager.__init__ and read here off `self`) and
dispatches the decoded JSON payload to the matching handler. Mixed into
MQTTManager; see services/mqtt/__init__.py for why this is a mixin rather than
a delegating collaborator object.
"""
import json
import logging

logger = logging.getLogger("amphive.mqtt")

# Hard ceiling on the size of a single inbound MQTT payload we will decode/parse.
# Set slightly ABOVE the broker's own message_size_limit (8 KB in
# deploy/config/mosquitto.conf) so the broker is the primary gate and this is
# defense-in-depth: real telemetry/status/alarm frames are ~200 bytes, so any
# frame anywhere near this is hostile. An authenticated-but-compromised gateway
# could otherwise publish a multi-megabyte payload and memory-DoS the shared
# backend (every frame is decoded and JSON-parsed on the paho callback thread).
MAX_MQTT_PAYLOAD_BYTES = 16384


class MQTTRouterMixin:
    """Topic-pattern dispatch for inbound MQTT messages."""

    def _on_message(self, client, userdata, msg):
        # Untrusted device edge: drop oversized frames BEFORE decoding/parsing so
        # a hostile gateway can't force a large allocation. len() on the raw
        # bytes is O(1) and doesn't copy.
        if len(msg.payload) > MAX_MQTT_PAYLOAD_BYTES:
            logger.warning(
                "Oversized MQTT payload dropped",
                extra={"topic": msg.topic, "size": len(msg.payload)},
            )
            return

        # WHY the outer catch-all below: this method IS the paho on_message
        # callback and runs on the network-loop thread. In paho 2.1.0
        # `suppress_exceptions` defaults to False and the backend never sets it,
        # so ANY exception that escapes here re-raises up through
        # loop_read -> _loop -> loop_forever -> _thread_main and KILLS the
        # network-loop thread. The process keeps serving HTTP, but ALL MQTT
        # ingestion (telemetry, billing accrual, auto-stop, gateway status,
        # alarms/safety-finalize, discovery) freezes for EVERY tenant until the
        # backend is restarted. A single gateway credential publishing one
        # malformed frame to its own topic could otherwise take the whole
        # platform's ingestion down. So no parse or handler exception — current
        # or future — may ever escape this callback: we log it and drop just
        # that one frame. Do NOT narrow this into a specific-exceptions catch;
        # the broad backstop is deliberate and load-bearing.
        try:
            payload_str = msg.payload.decode("utf-8", errors="ignore")
            logger.debug(
                "Received MQTT message",
                extra={"topic": msg.topic, "payload": payload_str},
            )

            # Match the logs topic BEFORE the JSON parse below: firmware
            # publishes plain-text log lines there (not JSON), so json.loads
            # would always fail and the line would be dropped as "invalid JSON".
            # This is the one non-JSON handler branch; the isinstance guard
            # below deliberately does not apply to it.
            logs_match = self.logs_pattern.match(msg.topic)
            if logs_match:
                gateway_id = logs_match.group(1)
                self._handle_gateway_log(gateway_id, payload_str)
                return

            try:
                payload = json.loads(payload_str)
            except (json.JSONDecodeError, ValueError, RecursionError, MemoryError):
                # Beyond ordinary malformed JSON (JSONDecodeError/ValueError): a
                # deeply-nested payload makes json.loads recurse until it raises
                # RecursionError, and a pathological one can raise MemoryError.
                # (The outer catch-all would also swallow these, but keeping the
                # specific guard lets us log them as the expected "invalid JSON"
                # case rather than an unexpected internal error.)
                logger.warning(
                    "Invalid JSON payload on MQTT topic",
                    extra={"topic": msg.topic, "payload": payload_str},
                )
                return

            # Every JSON topic handler below calls payload.get(...) and so
            # REQUIRES a JSON object (dict). A valid but non-object JSON value —
            # 5, [], "x", true, null, or a bare/nested array — parses fine, then
            # hits .get() and raises AttributeError ("'int' object has no
            # attribute 'get'") on this very thread. Reject it before dispatch
            # (O(1) isinstance check) so it degrades to a logged drop instead of
            # riding the outer catch-all.
            if not isinstance(payload, dict):
                logger.warning(
                    "Non-object JSON payload on MQTT topic dropped",
                    extra={"topic": msg.topic, "payload_type": type(payload).__name__},
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
        except Exception:
            # Durable backstop: a bug in ANY current or future handler (or the
            # routing/parse above) drops just this one frame instead of killing
            # the network-loop thread and freezing platform-wide ingestion (see
            # the block comment above). BaseException is intentionally NOT caught
            # so KeyboardInterrupt/SystemExit still propagate normally.
            logger.exception(
                "Unhandled error dispatching MQTT message; frame dropped",
                extra={"topic": getattr(msg, "topic", "?")},
            )
