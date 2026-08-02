"""Unit tests for SimulatedPlug — no network, no real time.sleep(). Covers
the energy-integration model, on/off state, JSON-RPC dispatch shapes, and
the --reset-counter-at scheduled reset.
"""

import time

import pytest
from plug import PlugConfig, SimulatedPlug


def _plug(*, initial=None, **overrides) -> SimulatedPlug:
    defaults = dict(plug_id=1, label="p1", host="127.0.0.1", port=9999, watts=1000.0, jitter=0.0)
    defaults.update(overrides)
    cfg = PlugConfig(**defaults)
    return SimulatedPlug(cfg, auth_hash=b"\x00" * 32, initial=initial)


def test_starts_off_with_configured_starting_energy():
    p = _plug(start_kwh=2.5)
    info = p.get_device_info()
    assert info["device_on"] is False
    energy = p.get_energy_usage()
    assert energy["current_power"] == 0
    assert energy["today_energy"] == pytest.approx(2500, abs=1)  # kWh -> Wh


def test_set_device_on_toggles_and_fires_callback():
    p = _plug()
    seen = []
    p.on_state_changed = lambda plug: seen.append(plug.get_device_info()["device_on"])

    p.set_device_on(True)
    assert p.get_device_info()["device_on"] is True
    assert seen == [True]

    p.set_device_on(True)  # no-op, no change -> no extra callback
    assert seen == [True]

    p.set_device_on(False)
    assert seen == [True, False]


def test_dispatch_set_device_info_toggles_relay_and_shapes_response():
    p = _plug()
    result = p.dispatch("set_device_info", {"device_on": True})
    assert result == {"error_code": 0, "result": {"response": ""}}
    assert p.get_device_info()["device_on"] is True


def test_dispatch_unknown_method_returns_nonzero_error_code():
    p = _plug()
    result = p.dispatch("some_unsupported_method", {})
    assert result["error_code"] != 0


def test_energy_accumulates_while_on_and_not_while_off(monkeypatch):
    t = {"v": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["v"])
    p = _plug(watts=3600.0)  # 3600 W = exactly 1 Wh per second

    p.set_device_on(True)
    p.tick()  # seeds _last_tick at t=1000
    t["v"] += 10.0  # 10 s elapsed while ON
    p.tick()

    energy_on = p.get_energy_usage()["today_energy"]
    assert energy_on == pytest.approx(10, abs=0.5)  # ~10 Wh accrued

    p.set_device_on(False)
    t["v"] += 10.0  # 10 s elapsed while OFF
    p.tick()
    energy_off = p.get_energy_usage()["today_energy"]
    assert energy_off == energy_on  # no further accrual once off


def test_current_uses_power_factor_not_naive_power_over_voltage(monkeypatch):
    p = _plug(watts=2300.0, voltage=230.0, power_factor=0.9)
    p.set_device_on(True)
    energy = p.get_energy_usage()
    naive_ma = round(2300.0 / 230.0 * 1000.0)  # 10 A if PF were 1.0
    assert energy["current_ma"] != naive_ma
    assert energy["current_ma"] == pytest.approx(naive_ma / 0.9, rel=0.05)


def test_overheat_and_overcurrent_report_normal_by_default():
    p = _plug()
    info = p.get_device_info()
    assert info["overheat_status"] == "normal"
    assert info["overcurrent_status"] == "normal"


def test_scheduled_reset_fires_once_and_invokes_callback(monkeypatch):
    real_time = {"v": 1000.0}
    monkeypatch.setattr(time, "time", lambda: real_time["v"])
    monkeypatch.setattr(time, "monotonic", lambda: real_time["v"])

    p = _plug(start_kwh=5.0, reset_after_s=10.0, reset_value_kwh=0.0)
    fired = []
    p.on_counter_reset = lambda plug: fired.append(True)

    real_time["v"] += 5.0
    p.tick()
    assert fired == []  # deadline not reached yet
    assert p.get_energy_usage()["today_energy"] == pytest.approx(5000, abs=1)

    real_time["v"] += 10.0  # now past the 10s deadline
    p.tick()
    assert fired == [True]
    assert p.get_energy_usage()["today_energy"] == 0

    real_time["v"] += 10.0
    p.tick()
    assert fired == [True]  # fires only once


def test_state_survives_restart_via_snapshot():
    p = _plug(start_kwh=1.0)
    p.set_device_on(True)
    snap = p.snapshot()

    p2 = _plug(initial=snap)
    assert p2.get_device_info()["device_on"] is True
    assert p2.get_energy_usage()["today_energy"] == pytest.approx(1000, abs=1)
