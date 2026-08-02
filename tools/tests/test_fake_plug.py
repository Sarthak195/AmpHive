"""Unit tests for tools/fake_plug.py (v2) — payload shapes and command
handling, DB-free and network-free. The MQTT client is a real `paho.mqtt`
`Client` instance (construction never touches the network), but every test
here either exercises pure `PlugSim` logic directly or mocks
`FakeGateway.client.publish`/`.subscribe` so no socket is ever opened and no
real broker (prod or otherwise) is ever contacted.

Mirrors tools/p110_sim/tests' style: no real time.sleep(), clocks passed
explicitly or monkeypatched.
"""

import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import fake_plug
import pytest
from fake_plug import FakeGateway, GatewayConfig, PlugSim

GW_ID = "test-gw"
COMMANDS_TMPL = f"amphive/gateways/{GW_ID}/plugs/{{plug_id}}/commands"
CONFIG_TOPIC = f"amphive/gateways/{GW_ID}/config"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _plug(**overrides) -> PlugSim:
    defaults = dict(plug_id=1, local_ip="10.0.0.1", watts=1000.0, voltage=230.0,
                     jitter=0.0, power_factor=0.95, ramp_seconds=0.0)
    defaults.update(overrides)
    return PlugSim(**defaults)


def _msg(topic: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, payload=json.dumps(payload).encode())


def _make_gateway(**cfg_overrides) -> FakeGateway:
    defaults = dict(gateway_id=GW_ID, broker_host="127.0.0.1", broker_port=1883,
                     username=GW_ID, password="pw",
                     default_watts=1000.0, jitter=0.0, ramp_seconds=0.0)
    defaults.update(cfg_overrides)
    gw = FakeGateway(GatewayConfig(**defaults))
    gw.client.publish = MagicMock()
    gw.client.subscribe = MagicMock()
    gw._connected = True
    return gw


@pytest.fixture
def gateway() -> FakeGateway:
    return _make_gateway()


def _publishes_to(gw: FakeGateway, topic: str) -> list:
    return [c for c in gw.client.publish.call_args_list if c.args[0] == topic]


# ---------------------------------------------------------------------------
# PlugSim — telemetry payload shape
# ---------------------------------------------------------------------------

def test_telemetry_payload_has_exact_contract_fields():
    plug = _plug()
    payload, _ = plug.tick_and_build_telemetry(time.monotonic(), time.time())
    assert set(payload.keys()) == {
        "plug_id", "watts", "kwh", "voltage", "current", "relay", "status", "session_id",
    }


def test_idle_telemetry_shape_matches_contract():
    plug = _plug(plug_id=7)
    now = time.monotonic()
    payload, log_line = plug.tick_and_build_telemetry(now, time.time())
    assert payload == {
        "plug_id": 7, "watts": 0.0, "kwh": 0.0, "voltage": 230.0, "current": 0.0,
        "relay": False, "status": "available", "session_id": "",
    }
    assert log_line is None


def test_charging_telemetry_reports_occupied_with_session_id():
    plug = _plug(ramp_seconds=0.0, jitter=0.0)
    plug.handle_on({"session_id": "42", "max_kwh": 30.0, "max_duration_seconds": 14400})
    payload, _ = plug.tick_and_build_telemetry(plug._session_start, time.time())
    assert payload["status"] == "occupied"
    assert payload["relay"] is True
    assert payload["session_id"] == "42"
    assert payload["watts"] == pytest.approx(1000.0)


def test_measured_current_uses_power_factor_not_naive_power_over_voltage():
    plug = _plug(watts=2300.0, voltage=230.0, power_factor=0.9, ramp_seconds=0.0, jitter=0.0)
    plug.handle_on({})
    payload, _ = plug.tick_and_build_telemetry(plug._session_start, time.time())
    naive = 2300.0 / 230.0
    assert payload["current"] != pytest.approx(naive)
    assert payload["current"] == pytest.approx(naive / 0.9, rel=0.01)


def test_kwh_is_always_zero_while_idle_even_after_a_prior_session():
    """Regression check: the old (v1) fake_plug froze `kwh` at its last
    session's value while idle instead of reporting 0 (docs/MQTT_CONTRACT.md:
    'Idle (no active session) reports 0')."""
    t = {"v": 1000.0}
    plug = _plug(watts=3600.0, ramp_seconds=0.0, jitter=0.0)  # 3600 W = 1 Wh/s
    plug._session_start = t["v"]
    plug._last_tick = t["v"]
    plug.handle_on({})
    plug._session_start = t["v"]
    plug._last_tick = t["v"]

    t["v"] += 10.0
    payload, _ = plug.tick_and_build_telemetry(t["v"], t["v"])
    assert payload["kwh"] > 0

    plug.handle_off()
    payload, _ = plug.tick_and_build_telemetry(t["v"] + 5.0, t["v"] + 5.0)
    assert payload["kwh"] == 0.0
    assert payload["status"] == "available"
    assert payload["session_id"] == ""


def test_ramp_reaches_half_target_at_half_ramp_time():
    plug = _plug(watts=3600.0, jitter=0.0, ramp_seconds=10.0)
    plug._session_start = 1000.0
    plug._last_tick = 1000.0
    plug.session_active = True
    payload, _ = plug.tick_and_build_telemetry(1005.0, 0.0)  # 5s into a 10s ramp
    assert payload["watts"] == pytest.approx(1800.0, rel=0.02)


def test_ramp_reaches_full_target_after_ramp_completes():
    plug = _plug(watts=3600.0, jitter=0.0, ramp_seconds=10.0)
    plug._session_start = 1000.0
    plug._last_tick = 1000.0
    plug.session_active = True
    payload, _ = plug.tick_and_build_telemetry(1015.0, 0.0)  # past the 10s ramp
    assert payload["watts"] == pytest.approx(3600.0)


# ---------------------------------------------------------------------------
# PlugSim — ON / OFF / SET_LIMITS
# ---------------------------------------------------------------------------

def test_handle_on_rebaselines_session():
    plug = _plug()
    plug.session_kwh = 5.0  # pretend leftover from a previous session
    plug.handle_on({"session_id": "99", "max_kwh": 12.5, "max_duration_seconds": 1800,
                     "max_current_a": 10.0})
    assert plug.session_active is True
    assert plug.session_id == "99"
    assert plug.session_kwh == 0.0
    assert plug.max_kwh == 12.5
    assert plug.max_duration_s == 1800
    assert plug.max_current_a == 10.0


def test_handle_on_defaults_current_cap_when_omitted():
    plug = _plug()
    plug.handle_on({"session_id": "1"})
    assert plug.max_current_a == fake_plug.DEFAULT_PLUG_CAP_A


def test_handle_off_deactivates_and_returns_final_kwh():
    plug = _plug()
    plug.handle_on({})
    plug.session_kwh = 1.234
    final = plug.handle_off()
    assert final == pytest.approx(1.234)
    assert plug.session_active is False


def test_set_limits_noop_without_active_session():
    plug = _plug()
    applied = plug.handle_set_limits({"max_kwh": 5.0})
    assert applied is False
    assert plug.max_kwh == 30.0  # untouched default


def test_set_limits_updates_thresholds_without_rebaselining():
    plug = _plug()
    plug.handle_on({"session_id": "1"})
    plug.session_kwh = 2.5
    start = plug._session_start
    sid = plug.session_id

    applied = plug.handle_set_limits({"max_kwh": 20.0, "max_duration_seconds": 7200})
    assert applied is True
    assert plug.max_kwh == 20.0
    assert plug.max_duration_s == 7200
    # NOT re-baselined: session_id/start time/accumulated kwh all untouched.
    assert plug.session_id == sid
    assert plug._session_start == start
    assert plug.session_kwh == pytest.approx(2.5)


def test_set_limits_defaults_omitted_fields_matching_firmware_quirk():
    """firmware/main/main.c's SET_LIMITS handler defaults an OMITTED
    max_duration_seconds/max_kwh to 14400/30.0 rather than leaving the
    current value alone -- this mirrors that exactly (the backend always
    sends both today, so it's rarely hit in practice, but the fake plug
    should still be faithful to the wire contract)."""
    plug = _plug()
    plug.handle_on({"max_kwh": 5.0, "max_duration_seconds": 100})
    plug.handle_set_limits({})  # both fields omitted
    assert plug.max_duration_s == 14400
    assert plug.max_kwh == 30.0


def test_set_limits_leaves_current_cap_intact_when_omitted():
    plug = _plug()
    plug.handle_on({"max_current_a": 10.0})
    plug.handle_set_limits({"max_kwh": 5.0, "max_duration_seconds": 100})
    assert plug.max_current_a == 10.0  # not reset to DEFAULT_PLUG_CAP_A


def test_set_limits_rearms_current_cap_when_provided():
    plug = _plug()
    plug.handle_on({"max_current_a": 10.0})
    plug.handle_set_limits({"max_kwh": 5.0, "max_duration_seconds": 100, "max_current_a": 6.0})
    assert plug.max_current_a == 6.0


# ---------------------------------------------------------------------------
# PlugSim — watchdog trip
# ---------------------------------------------------------------------------

def test_watchdog_trips_on_duration_and_reports_pre_trip_frame():
    plug = _plug(watts=1000.0, jitter=0.0, ramp_seconds=0.0)
    plug._session_start = 1000.0
    plug._last_tick = 1000.0
    plug.handle_on({"max_duration_seconds": 60, "max_kwh": 999.0})
    plug._session_start = 1000.0
    plug._last_tick = 1000.0

    payload, log_line = plug.tick_and_build_telemetry(1061.0, 0.0)  # 61s > 60s cap
    assert plug.session_active is False
    assert "max duration" in log_line
    # The SAME frame that trips still reports the pre-trip occupied reading —
    # matches firmware capturing sess_active before running the watchdog.
    assert payload["status"] == "occupied"
    assert payload["relay"] is True

    # The NEXT tick is the first to report idle.
    payload2, log_line2 = plug.tick_and_build_telemetry(1062.0, 0.0)
    assert payload2["status"] == "available"
    assert log_line2 is None


def test_watchdog_trips_on_energy_limit():
    plug = _plug(watts=3600.0, jitter=0.0, ramp_seconds=0.0)  # 1 kWh/hour->fast accrual
    plug._session_start = 1000.0
    plug._last_tick = 1000.0
    plug.handle_on({"max_duration_seconds": 999999, "max_kwh": 0.01})
    plug._session_start = 1000.0
    plug._last_tick = 1000.0

    # 3600 W for 60s = 0.06 kWh, comfortably over the 0.01 kWh cap.
    payload, log_line = plug.tick_and_build_telemetry(1060.0, 0.0)
    assert plug.session_active is False
    assert "energy limit" in log_line


# ---------------------------------------------------------------------------
# PlugSim — scheduled --reset-counter
# ---------------------------------------------------------------------------

def test_scheduled_reset_fires_once_mid_session():
    plug = _plug(watts=1000.0, jitter=0.0, ramp_seconds=0.0)
    plug._session_start = 1000.0
    plug._last_tick = 1000.0
    plug.handle_on({"max_duration_seconds": 999999, "max_kwh": 999.0})
    plug._session_start = 1000.0
    plug._last_tick = 1000.0
    plug.schedule_reset(after_s=10.0, value_kwh=0.5, start_wall=1000.0)

    # Before the deadline: no reset.
    payload, log_line = plug.tick_and_build_telemetry(1005.0, 1005.0)
    assert log_line is None
    assert payload["kwh"] != pytest.approx(0.5)

    # Past the deadline: counter forced to value_kwh, WARN line returned.
    payload2, log_line2 = plug.tick_and_build_telemetry(1006.0, 1011.0)
    assert payload2["kwh"] == pytest.approx(0.5, abs=1e-6)
    assert "reset-counter fired" in log_line2
    assert "ENERGY_COUNTER_RESET_DROP_KWH" in log_line2

    # Fires only once.
    payload3, log_line3 = plug.tick_and_build_telemetry(1007.0, 1020.0)
    assert log_line3 is None


# ---------------------------------------------------------------------------
# FakeGateway — connect / status
# ---------------------------------------------------------------------------

def test_on_connect_publishes_online_status_with_fw_and_subscribes(gateway):
    gateway._on_connect(gateway.client, None, {}, 0)

    status_calls = _publishes_to(gateway, gateway.status_topic)
    assert len(status_calls) == 1
    assert json.loads(status_calls[0].args[1]) == {"status": "online", "fw": fake_plug.DEFAULT_FW}
    assert status_calls[0].kwargs.get("retain") == 1

    sub_topics = [c.args[0] for c in gateway.client.subscribe.call_args_list]
    assert gateway.command_topic in sub_topics
    assert gateway.config_topic in sub_topics


def test_on_connect_failure_does_not_publish(gateway):
    gateway._on_connect(gateway.client, None, {}, 5)  # non-zero reason code = failure
    assert gateway.client.publish.call_args_list == []


# ---------------------------------------------------------------------------
# FakeGateway — command dispatch via _on_message
# ---------------------------------------------------------------------------

def test_on_command_creates_plug_and_starts_session(gateway):
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2),
        {"action": "ON", "session_id": "42", "max_kwh": 10.0,
         "max_duration_seconds": 3600, "local_ip": "10.0.0.5"},
    ))
    plug = gateway.plugs[2]
    assert plug.session_active is True
    assert plug.session_id == "42"
    assert plug.local_ip == "10.0.0.5"
    assert plug.max_kwh == 10.0
    assert plug.max_duration_s == 3600


def test_on_command_without_ip_and_no_prior_slot_is_dropped(gateway):
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=9), {"action": "ON", "session_id": "1"},
    ))
    assert 9 not in gateway.plugs


def test_off_command_deactivates_session(gateway):
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2),
        {"action": "ON", "session_id": "1", "local_ip": "10.0.0.5"},
    ))
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2), {"action": "OFF"},
    ))
    assert gateway.plugs[2].session_active is False


def test_set_limits_command_applies_to_active_session(gateway):
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2),
        {"action": "ON", "session_id": "1", "local_ip": "10.0.0.5", "max_kwh": 5.0},
    ))
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2),
        {"action": "SET_LIMITS", "max_kwh": 20.0, "max_duration_seconds": 7200},
    ))
    plug = gateway.plugs[2]
    assert plug.max_kwh == 20.0
    assert plug.max_duration_s == 7200
    assert plug.session_active is True


def test_set_limits_command_never_creates_a_plug(gateway):
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2), {"action": "SET_LIMITS", "max_kwh": 20.0},
    ))
    assert 2 not in gateway.plugs


def test_set_interval_clamps_to_firmware_bounds(gateway):
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2), {"action": "SET_INTERVAL", "interval_ms": 100},
    ))
    assert gateway.interval == pytest.approx(fake_plug.MIN_INTERVAL_MS / 1000.0)

    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2), {"action": "SET_INTERVAL", "interval_ms": 999999},
    ))
    assert gateway.interval == pytest.approx(fake_plug.MAX_INTERVAL_MS / 1000.0)


def test_ota_refused_while_session_active_publishes_alarm_and_log(gateway):
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2),
        {"action": "ON", "session_id": "1", "local_ip": "10.0.0.5"},
    ))
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2),
        {"action": "OTA", "url": "https://example.test/fw.bin"},
    ))
    alarm_calls = _publishes_to(gateway, gateway.alarms_topic)
    assert len(alarm_calls) == 1
    assert json.loads(alarm_calls[0].args[1]) == {"event": "OTA_REFUSED_SESSION_ACTIVE"}

    log_calls = _publishes_to(gateway, gateway.logs_topic)
    assert any("OTA refused" in c.args[1] for c in log_calls)


def test_ota_noop_when_no_active_session(gateway):
    gateway._on_message(gateway.client, None, _msg(
        COMMANDS_TMPL.format(plug_id=2),
        {"action": "OTA", "url": "https://example.test/fw.bin"},
    ))
    assert _publishes_to(gateway, gateway.alarms_topic) == []


def test_malformed_json_command_publishes_warn_log_line(gateway):
    msg = SimpleNamespace(topic=COMMANDS_TMPL.format(plug_id=2), payload=b"{not json")
    gateway._on_message(gateway.client, None, msg)
    log_calls = _publishes_to(gateway, gateway.logs_topic)
    assert len(log_calls) == 1
    line = log_calls[0].args[1]
    assert line.startswith("W (")
    assert "amphive_gateway:" in line
    assert "Command JSON parse failed" in line


def test_parse_plug_id_from_topic():
    parse = FakeGateway._parse_plug_id_from_topic
    assert parse("amphive/gateways/gw1/plugs/7/commands") == 7
    assert parse("amphive/gateways/gw1/config") is None


# ---------------------------------------------------------------------------
# FakeGateway — retained multi-plug roster
# ---------------------------------------------------------------------------

def test_roster_creates_multiple_plugs():
    gw = _make_gateway()
    gw._apply_roster({"v": 1, "plugs": [
        {"plug_id": 1, "local_ip": "10.0.0.1", "max_current_a": 16.0},
        {"plug_id": 2, "local_ip": "10.0.0.2", "max_current_a": 32.0},
    ]})
    assert set(gw.plugs) == {1, 2}
    assert gw.plugs[1].max_current_a == 16.0
    assert gw.plugs[2].max_current_a == 32.0


def test_roster_respects_max_plugs_and_logs_overflow(gateway):
    entries = [{"plug_id": i, "local_ip": f"10.0.0.{i}"} for i in range(1, fake_plug.MAX_PLUGS + 2)]
    gateway._apply_roster({"v": 1, "plugs": entries})
    assert len(gateway.plugs) == fake_plug.MAX_PLUGS
    assert (fake_plug.MAX_PLUGS + 1) not in gateway.plugs
    log_calls = _publishes_to(gateway, gateway.logs_topic)
    assert any("slot table full" in c.args[1] for c in log_calls)


def test_roster_flags_dropped_idle_plug_and_reaps_it():
    gw = _make_gateway()
    gw._apply_roster({"v": 1, "plugs": [{"plug_id": 2, "local_ip": "10.0.0.2"}]})
    assert 2 in gw.plugs

    gw._apply_roster({"v": 1, "plugs": []})  # plug 2 no longer in the roster
    assert gw.plugs[2].pending_remove is True

    gw._reap_pending()
    assert 2 not in gw.plugs


def test_roster_never_drops_an_active_session():
    """Matches firmware's handle_plug_roster exactly: it doesn't even FLAG an
    active-session slot for removal (`if (plugs[i].session_active) continue;`)
    -- so a roster drop that lands mid-session is silently ignored, and only
    a LATER roster application (once the plug has gone idle) flags it."""
    gw = _make_gateway()
    gw._apply_roster({"v": 1, "plugs": [{"plug_id": 2, "local_ip": "10.0.0.2"}]})
    gw.plugs[2].handle_on({"session_id": "1"})

    gw._apply_roster({"v": 1, "plugs": []})  # dropped while active -> not flagged yet
    assert gw.plugs[2].pending_remove is False
    gw._reap_pending()
    assert 2 in gw.plugs  # still tracked, session in progress

    gw.plugs[2].handle_off()
    gw._apply_roster({"v": 1, "plugs": []})  # re-applied now that it's idle -> flags it
    assert gw.plugs[2].pending_remove is True
    gw._reap_pending()
    assert 2 not in gw.plugs


def test_roster_ignores_malformed_entries():
    gw = _make_gateway()
    gw._apply_roster({"v": 1, "plugs": [
        {"plug_id": 1},                       # missing local_ip
        {"local_ip": "10.0.0.2"},              # missing plug_id
        "not-a-dict",
        {"plug_id": 3, "local_ip": "10.0.0.3"},  # the only valid one
    ]})
    assert set(gw.plugs) == {3}


def test_roster_missing_plugs_array_is_ignored_not_fatal(gateway):
    gateway._apply_roster({"v": 1})
    assert gateway.plugs == {}


# ---------------------------------------------------------------------------
# GatewayConfig wiring — bootstrap plug, watts-map, reset-specs
# ---------------------------------------------------------------------------

def test_bootstrap_plug_id_creates_a_slot_immediately():
    gw = _make_gateway(bootstrap_plug_id=2, bootstrap_local_ip="10.0.0.99", default_watts=500.0)
    assert 2 in gw.plugs
    assert gw.plugs[2].local_ip == "10.0.0.99"
    assert gw.plugs[2].watts == 500.0


def test_watts_map_overrides_default_for_specific_plug():
    gw = _make_gateway(default_watts=1000.0, watts_map={2: 5000.0})
    gw._apply_roster({"v": 1, "plugs": [
        {"plug_id": 1, "local_ip": "10.0.0.1"},
        {"plug_id": 2, "local_ip": "10.0.0.2"},
    ]})
    assert gw.plugs[1].watts == 1000.0
    assert gw.plugs[2].watts == 5000.0


def test_reset_specs_scheduled_on_plug_creation():
    gw = _make_gateway(reset_specs={2: (120.0, 0.5)})
    gw._apply_roster({"v": 1, "plugs": [{"plug_id": 2, "local_ip": "10.0.0.2"}]})
    plug = gw.plugs[2]
    assert plug._reset_deadline == pytest.approx(gw._start_wall + 120.0)
    assert plug._reset_value_kwh == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# CLI arg-parsing helpers
# ---------------------------------------------------------------------------

def test_parse_watts_map_repeatable_and_csv():
    assert fake_plug._parse_watts_map(["2=5000", "3=3300,4=0"]) == {2: 5000.0, 3: 3300.0, 4: 0.0}


def test_parse_watts_map_rejects_bad_shape():
    with pytest.raises(SystemExit):
        fake_plug._parse_watts_map(["not-a-pair"])


def test_parse_reset_specs_optional_value_defaults_to_zero():
    out = fake_plug._parse_reset_specs(["2@120", "3@60@0.75"])
    assert out == {2: (120.0, 0.0), 3: (60.0, 0.75)}


def test_parse_reset_specs_rejects_bad_shape():
    with pytest.raises(SystemExit):
        fake_plug._parse_reset_specs(["2@120@0.5@extra"])


# ---------------------------------------------------------------------------
# CLI end-to-end (--self-test only — no network)
# ---------------------------------------------------------------------------

def test_self_test_cli_runs_without_network(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["fake_plug.py", "--self-test", "--watts", "5000"])
    fake_plug.main()
    out = capsys.readouterr().out
    assert '"status": "online"' in out
    assert '"relay": true' in out
    assert "SET_LIMITS" in out
    assert "config    <-" in out
