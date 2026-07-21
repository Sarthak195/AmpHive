"""
Tests for the session-start gateway liveness gate.

Sessions used to start "successfully" against dead or mock gateways:
send_plug_command only confirms the *broker* accepted the publish (QoS 1
PUBACK), so nothing checked that a gateway was actually there. Those sessions
sat ACTIVE forever with 0 energy and the plug pinned OCCUPIED (prod sessions
72-76). The gate requires status == ONLINE *and* a fresh last_seen_at —
the status flag alone lies for seeded rows and after a missed LWT.

Telemetry refreshes last_seen_at (throttled once per gateway per minute in
MQTTManager) so long-connected gateways stay live without reconnecting.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend.database.models import GatewayStatus
from backend.services import mqtt_manager as mqtt_module
from backend.services.mqtt import telemetry as mqtt_telemetry_module
from backend.services.mqtt_manager import MQTTManager
from backend.services.session_lifecycle import (
    GATEWAY_LIVENESS_WINDOW_SEC,
    gateway_is_live,
)

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def _gateway(status, last_seen_at):
    gw = MagicMock()
    gw.status = status
    gw.last_seen_at = last_seen_at
    return gw


# --- gateway_is_live -------------------------------------------------------

def test_online_and_fresh_is_live():
    gw = _gateway(GatewayStatus.ONLINE, NOW - timedelta(seconds=10))
    assert gateway_is_live(gw, now=NOW) is True


def test_offline_is_dead_even_when_fresh():
    gw = _gateway(GatewayStatus.OFFLINE, NOW - timedelta(seconds=1))
    assert gateway_is_live(gw, now=NOW) is False


def test_online_but_stale_is_dead():
    """The mock-gateway case: DB says ONLINE but nothing reported for ages."""
    gw = _gateway(
        GatewayStatus.ONLINE,
        NOW - timedelta(seconds=GATEWAY_LIVENESS_WINDOW_SEC + 1),
    )
    assert gateway_is_live(gw, now=NOW) is False


def test_online_with_naive_fresh_timestamp_is_live():
    """Legacy rows written by the old naive-datetime onupdate hook."""
    gw = _gateway(
        GatewayStatus.ONLINE,
        (NOW - timedelta(seconds=10)).replace(tzinfo=None),
    )
    assert gateway_is_live(gw, now=NOW) is True


def test_missing_last_seen_is_dead():
    gw = _gateway(GatewayStatus.ONLINE, None)
    assert gateway_is_live(gw, now=NOW) is False


# --- MQTTManager last_seen_at refresh --------------------------------------

def test_bump_throttle_allows_first_then_blocks_within_window(monkeypatch):
    MQTTManager._instance = None
    mgr = MQTTManager()

    t = [1000.0]
    # _should_bump_gateway_seen's time.monotonic() call now lives in
    # services/mqtt/telemetry.py (post mixin-split); patch it there.
    monkeypatch.setattr(mqtt_telemetry_module.time, "monotonic", lambda: t[0])

    assert mgr._should_bump_gateway_seen("gw-1") is True
    assert mgr._should_bump_gateway_seen("gw-1") is False       # same instant
    t[0] += mqtt_module.GATEWAY_SEEN_BUMP_INTERVAL_SEC - 1
    assert mgr._should_bump_gateway_seen("gw-1") is False       # inside window
    t[0] += 2
    assert mgr._should_bump_gateway_seen("gw-1") is True        # window elapsed
    assert mgr._should_bump_gateway_seen("gw-2") is True        # per-gateway

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_telemetry_schedules_gateway_seen_refresh_throttled():
    """
    A telemetry message must schedule _persist_gateway_seen for its gateway
    (that's what keeps a long-connected gateway 'live' for the session-start
    gate), and an immediate second message must not schedule it again.
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    persisted = []

    async def fake_persist_telemetry(gateway_id, plug_id, watts, kwh, session_id=None,
                                     sample=None, relay_on=False, is_offline=False):
        pass

    async def fake_persist_gateway_seen(gateway_id):
        persisted.append(gateway_id)

    mgr._persist_telemetry = fake_persist_telemetry
    mgr._persist_gateway_seen = fake_persist_gateway_seen

    payload = {"plug_id": 1, "watts": 5.0, "kwh": 0.0, "status": "available"}
    mgr._handle_gateway_telemetry("gw-live", payload)
    mgr._handle_gateway_telemetry("gw-live", payload)  # throttled
    await asyncio.sleep(0.05)

    assert persisted == ["gw-live"]

    MQTTManager._instance = None
