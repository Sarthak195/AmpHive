"""
Tests for the inbound MQTT payload-size / hostile-parse guards in
services/mqtt/router.py (MQTTRouterMixin._on_message).

The device edge is untrusted: an authenticated-but-compromised gateway can
publish anything on its own topics. These tests lock in that a single frame
can neither memory-DoS the backend (oversized payload) nor escape into the
paho callback thread as an unhandled exception (deeply-nested / malformed
JSON, a valid-but-non-object JSON body, or a raising handler). Mirrors the
fake-msg / mocked-handler style of test_mqtt_manager.py.

Why non-object bodies matter: every JSON topic handler calls payload.get(),
so a bare 5/[]/"x"/true/null parses fine, then raises AttributeError on the
paho network-loop thread. paho 2.1.0 has suppress_exceptions=False, so an
escaped exception kills that thread and freezes ALL ingestion platform-wide
until restart. The guard + catch-all must make that impossible.

DB-free: the downstream _handle_gateway_* dispatch is mocked, so no event
loop or DB is needed.
"""

from unittest.mock import MagicMock

import pytest

from backend.services.mqtt.router import MAX_MQTT_PAYLOAD_BYTES
from backend.services.mqtt_manager import MQTTManager

# Every JSON topic type and the handler _on_message routes it to. The topic
# strings match the gateway telemetry/status/discovery/alarm regexes compiled
# in MQTTManager.__init__ (gateway id "gw-1").
_JSON_TOPICS = [
    ("amphive/gateways/gw-1/status", "_handle_gateway_status"),
    ("amphive/gateways/gw-1/telemetry", "_handle_gateway_telemetry"),
    ("amphive/gateways/gw-1/discovery", "_handle_gateway_plug_discovery"),
    ("amphive/gateways/gw-1/alarms", "_handle_gateway_alarm"),
]

# Valid JSON values that are NOT objects (dicts). Each parses successfully but
# has no .get(), so it must be rejected before dispatch.
_NON_OBJECT_JSON = [b"5", b"[]", b'"x"', b"true", b"null", b"[1, 2, 3]"]


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


# ---------------------------------------------------------------------------
# Non-object JSON body guard (the live-exploitable availability bug): a valid
# but non-dict JSON value reaches a handler's .get() and raises AttributeError
# on the paho network-loop thread. _on_message must reject it before dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("topic,handler_name", _JSON_TOPICS)
@pytest.mark.parametrize("body", _NON_OBJECT_JSON)
def test_non_object_json_body_dropped_without_dispatch(topic, handler_name, body):
    """A valid-but-non-object JSON body (5/[]/"x"/true/null/[1,2,3]) on ANY
    JSON topic must be rejected before dispatch: no handler is invoked, and
    _on_message does not raise (so nothing escapes into the paho thread)."""
    mgr = _mgr()
    msg = _FakeMsg(topic, body)

    mgr._on_message(None, None, msg)  # must not raise

    # The targeted handler (and every other) was skipped — the non-dict never
    # reached .get().
    _no_handler_called(mgr)
    MQTTManager._instance = None


def test_non_object_json_does_not_wedge_following_normal_frame():
    """A dropped non-object frame must not stop the next well-formed dict frame
    from dispatching normally (proves the guard drops just the one frame)."""
    mgr = _mgr()

    mgr._on_message(None, None, _FakeMsg("amphive/gateways/gw-1/telemetry", b"5"))

    payload = {"plug_id": 2, "watts": 5.0, "kwh": 0.2, "status": "occupied"}
    mgr._on_message(None, None,
                    _FakeMsg("amphive/gateways/gw-1/telemetry", json_bytes(payload)))

    mgr._handle_gateway_telemetry.assert_called_once_with("gw-1", payload)
    MQTTManager._instance = None


# ---------------------------------------------------------------------------
# Catch-all backstop: an unexpected exception raised INSIDE a handler (a bug in
# current or future handler code) must be swallowed, not propagated into the
# paho callback thread where it would kill the network loop.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("topic,handler_name", _JSON_TOPICS)
def test_raising_handler_does_not_propagate(topic, handler_name):
    """If a topic handler raises an unexpected exception on a well-formed dict
    payload, _on_message must catch it (the catch-all backstop) and NOT
    re-raise, so ingestion for other frames/tenants keeps running."""
    mgr = _mgr()
    boom = getattr(mgr, handler_name)
    boom.side_effect = RuntimeError("simulated handler bug")

    # A well-formed dict body so routing reaches the (now-raising) handler.
    body = json_bytes({"plug_id": 1, "status": "online", "unique_id": "kasa:AA",
                       "error": "THERMAL_CUTOFF"})
    msg = _FakeMsg(topic, body)

    mgr._on_message(None, None, msg)  # must not raise — backstop swallows it

    # The handler WAS reached (dict dispatched) but its exception did not escape.
    boom.assert_called_once()
    MQTTManager._instance = None


def test_raising_handler_does_not_wedge_following_frame():
    """A frame whose handler blows up must not stop a subsequent good frame
    (routed to a healthy handler) from dispatching."""
    mgr = _mgr()
    mgr._handle_gateway_status.side_effect = RuntimeError("simulated handler bug")

    mgr._on_message(None, None,
                    _FakeMsg("amphive/gateways/gw-1/status", json_bytes({"status": "online"})))

    payload = {"plug_id": 9, "watts": 1.0, "kwh": 0.01, "status": "available"}
    mgr._on_message(None, None,
                    _FakeMsg("amphive/gateways/gw-1/telemetry", json_bytes(payload)))

    mgr._handle_gateway_telemetry.assert_called_once_with("gw-1", payload)
    MQTTManager._instance = None


def test_non_json_log_line_still_dispatches_and_is_not_dict_guarded():
    """The logs topic carries plain-text (non-JSON) lines and is handled before
    the JSON parse / isinstance guard — a raw log line must still reach
    _handle_gateway_log unchanged (the dict guard must not swallow it)."""
    mgr = _mgr()
    line = b"E (1234) wifi: disconnected, reason=201"
    msg = _FakeMsg("amphive/gateways/gw-1/logs", line)

    mgr._on_message(None, None, msg)

    mgr._handle_gateway_log.assert_called_once_with("gw-1", line.decode("utf-8"))
    mgr._handle_gateway_telemetry.assert_not_called()
    MQTTManager._instance = None


def json_bytes(obj) -> bytes:
    import json

    return json.dumps(obj).encode("utf-8")
