"""
C10 live Wi-Fi signal (fw 2.10.0-direct): telemetry "wifi_rssi" parsing, the
in-memory store on the MQTT manager, the selftest live vs boot fallback, and
the version ordering that lets the OTA picker offer 2.10.0 over 2.9.0.
All DB-free.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.services import gateway_selftest as st
from backend.services.mqtt.telemetry import parse_wifi_rssi
from backend.services.mqtt_manager import MQTTManager
from backend.services.versioning import is_newer_version, version_sort_key

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (-61, -61), (-127, -127), (0, 0), (-61.0, -61),
    (None, None), (-128, None), (1, None), (False, None), (True, None),
    ("-61", None), (-61.5, None), (float("nan"), None), (float("-inf"), None),
    ([], None), ({}, None),
])
def test_parse_wifi_rssi(raw, expected):
    assert parse_wifi_rssi(raw) == expected


def test_handler_stores_live_rssi_and_a_frame_without_it_keeps_the_last():
    MQTTManager._instance = None
    mgr = MQTTManager()
    assert mgr.live_wifi_rssi("gw-1") is None

    mgr._handle_gateway_telemetry("gw-1", {"plug_id": 1, "watts": 5.0, "kwh": 0.0,
                                           "status": "available", "wifi_rssi": -64})
    rssi, at = mgr.live_wifi_rssi("gw-1")
    assert rssi == -64 and at.tzinfo is not None

    # Missing, garbage and out of range all leave the stored value alone.
    for extra in ({}, {"wifi_rssi": "weak"}, {"wifi_rssi": 5}, {"wifi_rssi": None}):
        mgr._handle_gateway_telemetry("gw-1", {"plug_id": 1, "watts": 5.0, "kwh": 0.0,
                                               "status": "available", **extra})
    assert mgr.live_wifi_rssi("gw-1") == (-64, at)

    # Gateway-wide: captured even when the plug part of the frame is rejected.
    mgr._handle_gateway_telemetry("gw-2", {"wifi_rssi": -77})
    assert mgr.live_wifi_rssi("gw-2")[0] == -77
    MQTTManager._instance = None


# --- selftest live vs boot -----------------------------------------------

def _boot(rssi, ago):
    from backend.database.models import GatewayEvent
    return GatewayEvent(
        tenant_id=1, gateway_id="aa11bb22cc33", plug_id=None, event_type="GATEWAY_BOOT",
        detail=f"Gateway booted (fw 2.9.0-direct): boot #3, Wi-Fi RSSI {rssi} dBm after 2 attempts.",
        created_at=NOW - timedelta(seconds=ago),
    )


def test_wifi_uses_fresh_live_value():
    c = st.check_wifi(_boot(-85, 3600), NOW, (-62, NOW - timedelta(seconds=120)))
    assert c["label"] == "Wi-Fi signal (live)"
    assert c["status"] == "pass" and c["detail"].startswith("-62 dBm live")
    assert c["observed_at"] == (NOW - timedelta(seconds=120)).isoformat()


def test_wifi_live_value_drives_thresholds():
    assert st.check_wifi(None, NOW, (-75, NOW))["status"] == "warn"
    assert st.check_wifi(None, NOW, (-90, NOW))["status"] == "fail"


def test_wifi_stale_live_falls_back_to_boot():
    stale = (-62, NOW - timedelta(seconds=st.LIVE_RSSI_FRESH_SEC + 1))
    c = st.check_wifi(_boot(-85, 3600), NOW, stale)
    assert c["label"] == "Wi-Fi signal (at boot)" and c["status"] == "fail"
    assert "-85 dBm at boot" in c["detail"]


def test_wifi_live_older_than_boot_falls_back_to_boot():
    c = st.check_wifi(_boot(-65, 60), NOW, (-90, NOW - timedelta(seconds=120)))
    assert c["label"] == "Wi-Fi signal (at boot)" and c["status"] == "pass"


def test_wifi_without_live_keeps_boot_and_unknown_behaviour():
    assert st.check_wifi(_boot(-65, 60), NOW)["label"] == "Wi-Fi signal (at boot)"
    assert st.check_wifi(None, NOW)["status"] == "unknown"
    assert st.check_wifi(None, NOW, (-60, NOW - timedelta(hours=1)))["status"] == "unknown"


def test_build_selftest_passes_live_rssi_through():
    from backend.database.models import Gateway, GatewayStatus
    gw = Gateway(id="aa11bb22cc33", name="gw", vpn_ip="x", status=GatewayStatus.ONLINE,
                 last_seen_at=NOW, firmware_version="2.10.0-direct")
    out = st.build_selftest(gw, [], None, [], [], NOW, (-58, NOW))
    wifi = next(c for c in out["checks"] if c["key"] == "wifi")
    assert wifi["label"] == "Wi-Fi signal (live)" and wifi["status"] == "pass"


# --- version ordering -----------------------------------------------------

def test_2_10_0_direct_is_newer_than_2_9_0_direct():
    assert is_newer_version("2.10.0-direct", "2.9.0-direct") is True
    assert is_newer_version("2.9.0-direct", "2.10.0-direct") is False
    ordered = sorted(["2.9.0-direct", "2.10.0-direct", "2.8.0-direct"],
                     key=version_sort_key, reverse=True)
    assert ordered[0] == "2.10.0-direct"
    assert st.check_firmware("2.9.0-direct", ["2.10.0-direct"])["status"] == "warn"
