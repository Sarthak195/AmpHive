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


# ---------------------------------------------------------------------------
# 2026-08 offline-consumption bench work: today_energy/month_energy are now
# independent accumulators (not aliases of the lifetime meter), so a bench
# test can exercise firmware/main/tapo_protocol.c's
# tapo_plug_reconcile_idle_baseline() day/month cross-check.
# ---------------------------------------------------------------------------


def test_today_and_month_energy_track_independently_of_lifetime_meter(monkeypatch):
    """Both calendar counters accrue in lockstep with the lifetime meter
    under normal operation (no reset fired) -- basic fidelity check."""
    t = {"v": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["v"])
    p = _plug(watts=3600.0)  # 3600 W = exactly 1 Wh per second

    p.set_device_on(True)
    p.tick()
    t["v"] += 20.0
    p.tick()

    energy = p.get_energy_usage()
    assert energy["today_energy"] == pytest.approx(20, abs=0.5)
    assert energy["month_energy"] == pytest.approx(20, abs=0.5)


def test_scheduled_reset_clears_today_and_month_together(monkeypatch):
    """--reset-counter simulates a full power-cycle: today_energy AND
    month_energy (and the lifetime meter) all clear together."""
    real_time = {"v": 1000.0}
    monkeypatch.setattr(time, "time", lambda: real_time["v"])
    monkeypatch.setattr(time, "monotonic", lambda: real_time["v"])

    p = _plug(start_kwh=5.0, reset_after_s=10.0, reset_value_kwh=0.5)
    real_time["v"] += 15.0  # past the 10s deadline
    p.tick()

    energy = p.get_energy_usage()
    assert energy["today_energy"] == pytest.approx(500, abs=1)   # 0.5 kWh -> 500 Wh
    assert energy["month_energy"] == pytest.approx(500, abs=1)


def test_daily_reset_clears_only_today_leaves_month_climbing(monkeypatch):
    """--daily-reset-counter simulates the P110's own nightly midnight
    rollover: ONLY today_energy clears. month_energy (and the lifetime
    meter) must keep climbing right through it -- this is exactly the
    "today rolled over mid-gap but month kept counting" case
    tapo_plug_reconcile_idle_baseline() falls back on."""
    real_time = {"v": 1000.0}
    monkeypatch.setattr(time, "time", lambda: real_time["v"])
    monkeypatch.setattr(time, "monotonic", lambda: real_time["v"])

    p = _plug(start_kwh=2.0, watts=3600.0, daily_reset_after_s=10.0)
    fired = []
    p.on_daily_reset = lambda plug: fired.append(True)

    p.set_device_on(True)
    p.tick()  # seeds _last_tick

    real_time["v"] += 5.0  # 5 Wh accrued, before the deadline
    p.tick()
    assert fired == []
    energy = p.get_energy_usage()
    assert energy["today_energy"] == pytest.approx(2005, abs=1)
    assert energy["month_energy"] == pytest.approx(2005, abs=1)

    real_time["v"] += 10.0  # now past the 10s daily-reset deadline (+10 more Wh accrued first)
    p.tick()
    assert fired == [True]
    energy = p.get_energy_usage()
    assert energy["today_energy"] == 0            # rolled over
    assert energy["month_energy"] == pytest.approx(2015, abs=1)  # untouched, kept climbing

    real_time["v"] += 5.0  # continues accruing normally after the rollover
    p.tick()
    energy = p.get_energy_usage()
    assert energy["today_energy"] == pytest.approx(5, abs=1)
    assert energy["month_energy"] == pytest.approx(2020, abs=1)
    assert fired == [True]  # fires only once


def test_manual_toggle_during_a_polling_gap_is_reflected_on_reconnect(monkeypatch):
    """Bench-fidelity check for the owner-reported incident this emulator
    extension exists to reproduce: energy consumed while nobody is calling
    get_energy_usage (the "gateway was offline" window) must still show up
    in the very next reading once polling resumes -- exactly what a real
    P110's onboard MCU does (it measures continuously, independent of who's
    asking). SimulatedPlug.tick() runs off wall-clock time, so directly
    driving set_device_on() (standing in for an independent KLAP client --
    tools/klap_probe.py, or a human with the real Tapo app -- toggling the
    plug while the firmware/gateway simply isn't polling) is sufficient; no
    HTTP server or firmware involvement is needed to prove this at the
    simulator layer."""
    t = {"v": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["v"])
    p = _plug(watts=3600.0)  # 3600 W = exactly 1 Wh per second

    # Establish an idle baseline (mirrors the firmware polling while nothing
    # is happening, before "the gateway goes offline").
    p.tick()
    baseline = p.get_energy_usage()
    assert baseline["today_energy"] == 0
    assert baseline["month_energy"] == 0

    # "Gateway offline": a manual session happens with nobody polling --
    # simulate it by driving the plug object directly (no get_energy_usage
    # call in between), then advancing wall-clock time.
    p.set_device_on(True)
    t["v"] += 30.0  # 30 s manual charge
    p.tick()
    p.set_device_on(False)

    # "Gateway reconnects": the very next read must show the full gap, even
    # though get_energy_usage was never called during it.
    after = p.get_energy_usage()
    assert after["today_energy"] == pytest.approx(30, abs=1)
    assert after["month_energy"] == pytest.approx(30, abs=1)
    assert after["current_power"] == 0  # relay is off again by the time we look
