"""
Tests for the inbound MQTT payload-size / hostile-parse guards in
services/mqtt/router.py (MQTTRouterMixin._on_message).

The device edge is untrusted: an authenticated-but-compromised gateway can
publish anything on its own topics. These tests lock in that a single frame
can neither memory-DoS the backend (oversized payload) nor escape into the
paho callback thread as an unhandled exception (deeply-nested / malformed
JSON). Mirrors the fake-msg / mocked-handler style of test_mqtt_manager.py.

DB-free: the downstream _handle_gateway_* dispatch is mocked, so no event
loop or DB is needed.
"""

from unittest.mock import MagicMock

from backend.services.mqtt.router import MAX_MQTT_PAYLOAD_BYTES
from backend.services.mqtt_manager import MQTTManager


class _FakeMsg:
    """Stands in for a paho MQTTMessage: a topic and a raw bytes payload."""

    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


def _mgr():
    """A DB-free MQTTManager with every downstream handler mocked out, so
    _on_message's routing/guarding can be exercised in isolation."""
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr._handle_gateway_telemetry = MagicMock()
    mgr._handle_gateway_status = MagicMock()
    mgr._handle_gateway_plug_discovery = MagicMock()
    mgr._handle_gateway_alarm = MagicMock()
    mgr._handle_gateway_log = MagicMock()
    return mgr


def _no_handler_called(mgr):
    mgr._handle_gateway_telemetry.assert_not_called()
    mgr._handle_gateway_status.assert_not_called()
    mgr._handle_gateway_plug_discovery.assert_not_called()
    mgr._handle_gateway_alarm.assert_not_called()
    mgr._handle_gateway_log.assert_not_called()


def test_oversized_payload_dropped_before_dispatch():
    """A frame larger than MAX_MQTT_PAYLOAD_BYTES is dropped without decoding
    or dispatching to any handler."""
    mgr = _mgr()
    oversized = b"x" * (MAX_MQTT_PAYLOAD_BYTES + 1)
    msg = _FakeMsg("amphive/gateways/gw-1/telemetry", oversized)

    mgr._on_message(None, None, msg)  # must not raise

    _no_handler_called(mgr)
    MQTTManager._instance = None


def test_oversized_valid_json_still_dropped():
    """Even well-formed JSON is dropped once it exceeds the cap — the size gate
    runs before the parse, so a huge-but-valid telemetry frame can't get in."""
    mgr = _mgr()
    # A valid JSON array padded past the ceiling.
    big = b"[" + b"0," * MAX_MQTT_PAYLOAD_BYTES + b"0]"
    assert len(big) > MAX_MQTT_PAYLOAD_BYTES
    msg = _FakeMsg("amphive/gateways/gw-1/telemetry", big)

    mgr._on_message(None, None, msg)

    _no_handler_called(mgr)
    MQTTManager._instance = None


def test_deeply_nested_json_caught_and_dropped():
    """Deeply-nested JSON makes json.loads exceed the recursion limit; the
    RecursionError must be caught (not propagate into the paho thread) and the
    frame dropped without dispatch."""
    mgr = _mgr()
    # ~10 KB (under the size cap) but ~5000 levels of nesting -> RecursionError.
    nested = (b"[" * 5000) + (b"]" * 5000)
    assert len(nested) <= MAX_MQTT_PAYLOAD_BYTES
    msg = _FakeMsg("amphive/gateways/gw-1/telemetry", nested)

    mgr._on_message(None, None, msg)  # must not raise

    _no_handler_called(mgr)
    MQTTManager._instance = None


def test_malformed_json_caught_and_dropped():
    """Ordinary malformed JSON is caught and dropped, no exception, no dispatch."""
    mgr = _mgr()
    msg = _FakeMsg("amphive/gateways/gw-1/telemetry", b"{not valid json")

    mgr._on_message(None, None, msg)  # must not raise

    _no_handler_called(mgr)
    MQTTManager._instance = None


def test_normal_small_valid_frame_still_dispatches():
    """A normal small, valid telemetry frame is decoded and dispatched to the
    telemetry handler unchanged."""
    mgr = _mgr()
    payload = {"plug_id": 1, "watts": 10.0, "kwh": 0.1, "status": "occupied"}
    msg = _FakeMsg("amphive/gateways/gw-1/telemetry", json_bytes(payload))

    mgr._on_message(None, None, msg)

    mgr._handle_gateway_telemetry.assert_called_once_with("gw-1", payload)
    mgr._handle_gateway_status.assert_not_called()
    mgr._handle_gateway_alarm.assert_not_called()
    MQTTManager._instance = None


def test_at_the_size_ceiling_is_not_dropped():
    """The cap is a strict >: a frame exactly at MAX_MQTT_PAYLOAD_BYTES is
    still accepted and parsed (proves the guard doesn't reject legitimate
    edge-sized frames)."""
    mgr = _mgr()
    # Build a valid JSON object padded to exactly the ceiling.
    base = {"plug_id": 1, "pad": ""}
    filler = MAX_MQTT_PAYLOAD_BYTES - len(json_bytes(base))
    base["pad"] = "a" * filler
    payload = json_bytes(base)
    assert len(payload) == MAX_MQTT_PAYLOAD_BYTES
    msg = _FakeMsg("amphive/gateways/gw-1/telemetry", payload)

    mgr._on_message(None, None, msg)

    mgr._handle_gateway_telemetry.assert_called_once()
    MQTTManager._instance = None


def json_bytes(obj) -> bytes:
    import json

    return json.dumps(obj).encode("utf-8")
