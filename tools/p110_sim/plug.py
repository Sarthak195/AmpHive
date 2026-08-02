"""A single simulated Tapo P110 plug: state machine + JSON-RPC method
dispatch (get_device_info / get_energy_usage / get_current_power /
set_device_info), shaped to match a real P110's response fields.

Field provenance, matching firmware/main/tapo_protocol.c's own documented
REAL-vs-derived split (see its header comment and FIRMWARE.md §4):
  * current_power (mW), current_ma, voltage_mv — the P110's own energy
    monitor; the firmware reads current_power + current_ma + voltage_mv from
    get_energy_usage and treats them as REAL measurements (falling back to
    nominal voltage / derived current only when a field is absent).
  * device_on, overheat_status, overcurrent_status — from get_device_info.
  * energy_kwh on the firmware side is a DRIVER-SIDE monotonic integrator
    computed from current_power samples — the firmware never reads a
    cumulative energy field from the plug for BILLING.
  * today_energy/month_energy (2026-08, offline-consumption bench work):
    the plug's OWN calendar counters, tracked as independent accumulators
    (not aliases of the lifetime meter) so a bench test can exercise
    firmware/main/tapo_protocol.c's tapo_plug_reconcile_idle_baseline() day/
    month cross-check: --reset-counter (PlugConfig.reset_after_s) simulates
    a full power-cycle and clears all three counters together; the new
    --daily-reset-counter (PlugConfig.daily_reset_after_s) simulates the
    P110's own nightly midnight rollover and clears ONLY today_energy,
    leaving month_energy (and the lifetime meter) climbing -- exactly the
    "today rolled over mid-gap but month kept counting" case the firmware's
    reconciliation logic falls back on. Because SimulatedPlug.tick() runs on
    every inbound HTTP request (server.py), an independent KLAP client (e.g.
    tools/klap_probe.py, or a human with the real Tapo app) toggling the
    relay while the firmware/gateway simply isn't polling is enough to
    reproduce the owner-reported incident on the bench -- no sim code needed
    beyond keeping these counters advancing in real wall-clock time
    regardless of who last called get_energy_usage.
"""

from __future__ import annotations

import base64
import random
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional


def _mac_from_id(plug_id: int) -> str:
    # Stable, obviously-fake MAC derived from the plug id (cosmetic only).
    tail = f"{plug_id:06X}"
    return f"AA:BB:CC:{tail[0:2]}:{tail[2:4]}:{tail[4:6]}"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


@dataclass
class PlugConfig:
    plug_id: int
    label: str
    host: str
    port: int
    watts: float = 1500.0
    jitter: float = 0.02          # fractional noise, e.g. 0.02 = +/-2%
    voltage: float = 230.0
    power_factor: float = 0.95
    start_kwh: float = 0.0
    drop_rate: float = 0.0        # probability [0,1) a request is dropped
    reset_after_s: Optional[float] = None    # seconds after plug creation
    reset_value_kwh: float = 0.0
    # Independent "midnight rollover" simulation (2026-08 offline-consumption
    # bench work): after this many seconds of uptime, reset ONLY
    # today_energy to 0, leaving month_energy/energy_wh untouched -- a real
    # P110 resets its calendar-day counter nightly on its own clock, distinct
    # from reset_after_s's full power-cycle reset (which clears all three).
    # See the module docstring.
    daily_reset_after_s: Optional[float] = None


@dataclass
class _PersistedState:
    energy_wh: float = 0.0
    device_on: bool = False
    on_time_s: float = 0.0
    today_energy_wh: float = 0.0
    month_energy_wh: float = 0.0


class SimulatedPlug:
    """Owns one plug's mutable state. Thread-safe: the HTTP server calls
    into this from its own request-handling thread, and with several plugs
    running concurrently each has its own lock."""

    def __init__(self, cfg: PlugConfig, auth_hash: bytes, *, initial: Optional[dict] = None):
        self.cfg = cfg
        self.auth_hash = auth_hash
        self._lock = threading.Lock()

        if initial is not None:
            st = _PersistedState(**initial)
            self._energy_wh = st.energy_wh
            self._device_on = st.device_on
            self._on_time_s = st.on_time_s
            # Old state files predate today/month tracking (both default to
            # 0.0 via the dataclass) -- fall back to the lifetime meter so a
            # resumed run doesn't look like a bogus reset happened.
            self._today_energy_wh = initial.get("today_energy_wh", self._energy_wh)
            self._month_energy_wh = initial.get("month_energy_wh", self._energy_wh)
        else:
            self._energy_wh = cfg.start_kwh * 1000.0
            self._device_on = False
            self._on_time_s = 0.0
            self._today_energy_wh = self._energy_wh
            self._month_energy_wh = self._energy_wh
        self._last_tick = time.monotonic()
        self._created_at = time.time()
        self._reset_deadline = (
            self._created_at + cfg.reset_after_s if cfg.reset_after_s is not None else None
        )
        self._reset_fired = False
        self._daily_reset_deadline = (
            self._created_at + cfg.daily_reset_after_s
            if cfg.daily_reset_after_s is not None
            else None
        )
        self._daily_reset_fired = False

        self.device_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"p110sim-{cfg.plug_id}").hex.upper()
        self.mac = _mac_from_id(cfg.plug_id)
        self.on_state_changed = None  # optional callback(plug) -> None
        self.on_counter_reset = None  # optional callback(plug) -> None, fires once at reset
        self.on_daily_reset = None    # optional callback(plug) -> None, fires once at daily rollover

    # ---- persistence snapshot -------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "energy_wh": self._energy_wh,
                "device_on": self._device_on,
                "on_time_s": self._on_time_s,
                "today_energy_wh": self._today_energy_wh,
                "month_energy_wh": self._month_energy_wh,
            }

    # ---- physics: called on every inbound request to advance the clock --------

    def tick(self) -> None:
        """Integrate energy since the last tick (wall-clock, so gaps between
        polls — including ones dropped by --drop-rate — still account for
        real elapsed time) and apply a scheduled counter reset if its
        deadline has passed."""
        now_mono = time.monotonic()
        with self._lock:
            dt_h = (now_mono - self._last_tick) / 3600.0
            self._last_tick = now_mono
            if self._device_on and dt_h > 0:
                watts = self._jittered_watts()
                wh = watts * dt_h
                self._energy_wh += wh
                self._today_energy_wh += wh
                self._month_energy_wh += wh
                self._on_time_s += dt_h * 3600.0

            if (
                not self._reset_fired
                and self._reset_deadline is not None
                and time.time() >= self._reset_deadline
            ):
                self._reset_fired = True
                self._energy_wh = self.cfg.reset_value_kwh * 1000.0
                self._today_energy_wh = self._energy_wh
                self._month_energy_wh = self._energy_wh
                cb = self.on_counter_reset
            else:
                cb = None

            if (
                not self._daily_reset_fired
                and self._daily_reset_deadline is not None
                and time.time() >= self._daily_reset_deadline
            ):
                self._daily_reset_fired = True
                self._today_energy_wh = 0.0
                daily_cb = self.on_daily_reset
            else:
                daily_cb = None
        if cb:
            cb(self)
        if daily_cb:
            daily_cb(self)

    # ---- load model -------------------------------------------------------

    def _jittered_watts(self) -> float:
        base = self.cfg.watts
        if self.cfg.jitter <= 0:
            return base
        return base * (1.0 + random.uniform(-self.cfg.jitter, self.cfg.jitter))

    def _current_power_mw(self) -> int:
        watts = self._jittered_watts() if self._device_on else 0.0
        return max(0, round(watts * 1000.0))

    def _voltage_mv(self) -> int:
        # Small mains-noise jitter around nominal, independent of load.
        v = self.cfg.voltage * (1.0 + random.uniform(-0.005, 0.005))
        return max(0, round(v * 1000.0))

    def _current_ma(self, power_mw: int, voltage_mv: int) -> int:
        # Real P110 current is MEASURED, not power/voltage: apparent power
        # (V x A) exceeds active power (W) because power factor < 1, so
        # derive the reported amps as P / (V x PF) rather than P / V. See
        # docs/MQTT_CONTRACT.md's note on `current` and tools/fake_plug.py's
        # POWER_FACTOR for the same modeling choice on the MQTT-fake side.
        if voltage_mv <= 0 or not self._device_on:
            return 0
        power_w = power_mw / 1000.0
        voltage_v = voltage_mv / 1000.0
        current_a = power_w / (voltage_v * self.cfg.power_factor)
        return max(0, round(current_a * 1000.0))

    # ---- JSON-RPC methods (mirrors the 3 the firmware calls, + 1 bonus) ------

    def get_device_info(self) -> dict:
        with self._lock:
            device_on = self._device_on
            on_time = int(self._on_time_s)
        return {
            "device_id": self.device_id,
            "fw_ver": "1.2.5 Build 240328 Rel.170053",
            "hw_ver": "1.0",
            "type": "SMART.TAPOPLUG",
            "model": "P110",
            "mac": self.mac,
            "hw_id": self.device_id[:16],
            "fw_id": "0" * 16,
            "oem_id": self.device_id[16:32],
            "specs": "",
            "device_on": device_on,
            "on_time": on_time,
            "overheated": False,
            "nickname": _b64(f"AmpHive Sim Plug {self.cfg.plug_id}"),
            "location": "",
            "avatar": "plug",
            "longitude": 0,
            "latitude": 0,
            "has_set_location_info": False,
            "ip": self.cfg.host,
            "ssid": _b64("AmpHive-Bench"),
            "signal_level": 3,
            "rssi": -45,
            "region": "Asia/Kolkata",
            "time_diff": 330,
            "lang": "en_US",
            # The two status fields tapo_protocol.c's telemetry_safety loop
            # actually watchdogs on (json_status_abnormal treats anything
            # other than the literal string "normal" as a fault).
            "overheat_status": "normal",
            "overcurrent_status": "normal",
            "power_protection_status": "normal",
            "charging_status": "normal",
            "default_states": {"type": "last_states", "state": {}},
        }

    def get_energy_usage(self) -> dict:
        power_mw = self._current_power_mw()
        voltage_mv = self._voltage_mv()
        current_ma = self._current_ma(power_mw, voltage_mv)
        with self._lock:
            today_energy_wh = self._today_energy_wh
            month_energy_wh = self._month_energy_wh
        return {
            "today_runtime": int(self._on_time_s // 60),
            "month_runtime": int(self._on_time_s // 60),
            # Independent calendar counters (2026-08) -- see the module
            # docstring for why these aren't just the lifetime meter.
            "today_energy": round(today_energy_wh),
            "month_energy": round(month_energy_wh),
            "local_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "electricity_charge": [0, 0, 0],
            "current_power": power_mw,
            # REAL measured fields (fw reads these directly — see module docstring).
            "current_ma": current_ma,
            "voltage_mv": voltage_mv,
        }

    def get_current_power(self) -> dict:
        return {"current_power": self._current_power_mw()}

    def set_device_info(self, params: dict) -> dict:
        if "device_on" in params:
            self.set_device_on(bool(params["device_on"]))
        # Real Tapo "write" responses wrap an empty confirmation string under
        # "response" (e.g. the `tapo` client's TapoResult{ response: String }) —
        # not an empty object.
        return {"response": ""}

    def set_device_on(self, on: bool) -> None:
        with self._lock:
            changed = on != self._device_on
            self._device_on = on
        if changed and self.on_state_changed:
            self.on_state_changed(self)

    def dispatch(self, method: str, params: dict) -> dict[str, Any]:
        if method == "get_device_info":
            return {"error_code": 0, "result": self.get_device_info()}
        if method == "get_energy_usage":
            return {"error_code": 0, "result": self.get_energy_usage()}
        if method == "get_current_power":
            return {"error_code": 0, "result": self.get_current_power()}
        if method == "set_device_info":
            return {"error_code": 0, "result": self.set_device_info(params)}
        return {"error_code": -1001}  # Tapo's generic "unsupported method"
