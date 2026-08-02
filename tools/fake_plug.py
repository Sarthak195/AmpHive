#!/usr/bin/env python3
"""
AmpHive — Fake Plug Simulator (v2)
===================================
A software stand-in for a **whole ESP32-C3 gateway** (not just one plug), so
the full stack (session start/stop, billing, live Socket.io telemetry, the
driver/CPO UI) can be exercised **without physical hardware**.

It speaks the exact same MQTT contract as `firmware/main/main.c` (see
`docs/MQTT_CONTRACT.md`), current as of fw 2.3.x:

  * publishes a retained `online` status (with an `fw` version string) + an
    `offline` LWT on `amphive/gateways/{gw}/status`
  * subscribes to the retained **multi-plug roster** on
    `amphive/gateways/{gw}/config` and builds/tears down a simulated plug
    per roster entry — up to `MAX_PLUGS` (4, matching
    `SESSION_NVS_MAX_PLUGS`) — exactly like real firmware (fw >=
    2.0.0-direct); a plug dropped from the roster is reaped once idle, an
    active session never is
  * subscribes to `amphive/gateways/{gw}/plugs/+/commands` and honours
    ON / OFF / SET_INTERVAL / SET_LIMITS (OTA is acknowledged — refusing with
    an `OTA_REFUSED_SESSION_ACTIVE` alarm while any plug is mid-session, like
    firmware — but otherwise no-op'd: a fake plug has no firmware to flash)
  * publishes telemetry on `amphive/gateways/{gw}/telemetry` every
    `--interval` seconds (or whatever `SET_INTERVAL` last set), one frame per
    plug, all fields firmware emits: `plug_id`, `watts`, `kwh`, `voltage`,
    `current`, `relay`, `status`, `session_id`
  * forwards a few realistic WARN/ERROR lines to `amphive/gateways/{gw}/logs`
    for the same conditions firmware's log-forwarder would catch (a local
    watchdog trip, a malformed command payload, a roster overflow, an OTA
    refusal) — real firmware only forwards WARN+ lines, so this does too

When a plug's session is ON its load **ramps** from 0 to the configured watts
over `--ramp-seconds` (soft-start realism, not an instant jump) with
`--jitter` fractional noise on top, and integrates session-relative energy
exactly like the firmware does (kwh resets to 0 at ON, reports 0 while idle),
so the wallet debits and the live cost tick up predictably. At the default
10 kW that's ~0.167 kWh/min (~0.83 coins/min at the default COINS_PER_KWH=5).

Registration (creating the gateway + plug rows) goes through the public CPO
API, not the DB, so this never touches the database directly.

Modes
-----
  (default)        register the fake gateway/plug via the API (idempotent),
                   then run the MQTT simulator.
  --register-only  just provision the DB rows via the API and print the plug id,
                   then exit. Run this from anywhere with API access.
  --run-only       skip provisioning and just simulate. `--plug-id` is now
                   optional (a bootstrap seed before any roster arrives) —
                   without it, the gateway just waits for the backend's
                   retained multi-plug roster on `.../config`, like real
                   firmware does on a fresh/unprovisioned unit.
  --self-test      print the telemetry/status/command/roster/log payloads and
                   exit. No network at all — a quick contract sanity check.

Where to run it
----------------
The broker is `mqtt.amphive.app:8883` (public, direct-MQTT, TLS + per-gateway
credentials — see docs/MQTT_CONTRACT.md); the legacy WireGuard/Tailscale
overlay this docstring used to describe is retired.
  * from anywhere:        --broker-host mqtt.amphive.app --broker-port 8883 --tls
  * inside the relay VM's compose network (as a service): --broker-host mqtt
    --broker-port 1883 (plaintext, internal-only — see
    deploy/docker/docker-compose.fakeplug.yml)
The registration part uses the public API (`--api-base`, default
https://amphive.app) and works from anywhere.

Examples
--------
  # One-time: create the fake gateway + plug (run from your workstation)
  python tools/fake_plug.py --register-only \
      --cpo-email cpo@amphive.test --cpo-password '<pw>'

  # Keep the fake plug live, single bootstrap plug (matches the prod service):
  python tools/fake_plug.py --run-only --plug-id 2 \
      --gateway-id fakeplug-gw-01 --broker-host mqtt.amphive.app --tls \
      --broker-user fakeplug-gw-01 --broker-pass '<pw>'

  # Multiple plugs, distinct loads, one dropping its counter mid-session:
  python tools/fake_plug.py --run-only --gateway-id bench-gw-01 \
      --broker-host mqtt.amphive.app --tls --broker-user bench-gw-01 --broker-pass '<pw>' \
      --watts 7400 --watts-map 2=11000,3=3300 --ramp-seconds 15 --jitter 0.03 \
      --reset-counter 2@300@0.5

Dependencies: paho-mqtt (`pip install paho-mqtt`). Everything else is stdlib.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fake_plug")

# Defaults chosen to line up with the seeded test fixture (see
# TEST_ACCOUNTS.local.txt / docs/TESTING.md) and the existing prod
# registration (gateway fakeplug-gw-01 / plug_id 2) — changing any of these
# is a breaking change for that live registration, not just a cosmetic tweak.
DEFAULT_API_BASE = os.getenv("AMPHIVE_API_BASE", "https://amphive.app")
DEFAULT_GATEWAY_ID = os.getenv("FAKE_PLUG_GATEWAY_ID", "fakeplug-gw-01")
# Legacy/overlay-only field on gateway registration (backend/schemas.py's
# CpoGatewayCreateRequest); direct-MQTT gateways don't use it. Empty is fine —
# routers/cpo/_gateways.py falls back to the gateway_id to satisfy the
# NOT NULL + UNIQUE column.
DEFAULT_VPN_IP = os.getenv("FAKE_PLUG_VPN_IP", "")
# NOTE: the "(10kW)" in this name is legacy cosmetic text, not a live
# indicator of configured load — register()'s idempotent re-run matches an
# existing plug BY THIS NAME, so changing it would orphan the real prod row
# (gateway fakeplug-gw-01 / plug_id 2) and create a duplicate instead of
# reusing it. Actual wattage always comes from --watts / --watts-map / the
# roster's own config, independent of this string.
DEFAULT_PLUG_NAME = os.getenv("FAKE_PLUG_NAME", "Fake Test Plug (10kW)")
DEFAULT_LOCAL_IP = os.getenv("FAKE_PLUG_LOCAL_IP", "10.0.0.99")
DEFAULT_GROUP_ID = int(os.getenv("FAKE_PLUG_GROUP_ID", "1"))  # public group
DEFAULT_WATTS = float(os.getenv("FAKE_PLUG_WATTS", "10000"))  # 10 kW, matches the original single-plug default
DEFAULT_VOLTAGE = float(os.getenv("FAKE_PLUG_VOLTAGE", "230"))
DEFAULT_INTERVAL = float(os.getenv("FAKE_PLUG_INTERVAL", "5"))
# Soft-start realism: watts ramps 0 -> target linearly over this many seconds
# after ON, instead of jumping instantly (a real EV's onboard charger/OBC
# ramps its draw, and a P110 wired to it would show the same shape). 0 = the
# old instant-jump behavior.
DEFAULT_RAMP_SECONDS = float(os.getenv("FAKE_PLUG_RAMP_SECONDS", "8"))
# Fractional load noise applied on top of the ramped watts (0.02 = +/-2%).
DEFAULT_JITTER = float(os.getenv("FAKE_PLUG_JITTER", "0.02"))
# Power factor of the simulated load. A real Tapo P110 reports MEASURED current
# and voltage, and active power = V x A x PF with PF < 1 — so the measured
# current is NOT power/voltage (apparent). Model that by deriving the reported
# current as power / (voltage * PF): with PF 0.95 the measured amps run a few
# percent ABOVE the naive power/voltage figure, exercising the
# measured-vs-derived path end-to-end on the fake rig.
POWER_FACTOR = float(os.getenv("FAKE_PLUG_POWER_FACTOR", "0.95"))
# Reported in the retained online status payload. Prefixed "fake-" so it can
# never be confused with a real device in the backend/UI; the numeric part
# tracks the firmware generation this simulator mirrors (firmware/CMakeLists.txt
# PROJECT_VER, 2.4.0-direct as of this writing).
DEFAULT_FW = os.getenv("FAKE_PLUG_FW", "fake-2.4.0-direct")
# Matches the firmware's per-plug current-cap default (REC-03) — see
# firmware/main/main.c's DEFAULT_PLUG_CAP_A / backend/services/caps.py's
# DEFAULT_PLUG_CAP_A. Used when an ON/SET_LIMITS payload omits max_current_a.
DEFAULT_PLUG_CAP_A = 16.0

# Match the firmware's SET_INTERVAL clamp (ms).
MIN_INTERVAL_MS = 500
MAX_INTERVAL_MS = 60000

# Matches firmware/main/session_nvs.h's SESSION_NVS_MAX_PLUGS — a real
# gateway's slot table only ever holds this many plugs, roster or not.
MAX_PLUGS = 4

_PLUG_ID_FROM_TOPIC_RE = re.compile(r"/plugs/(\d+)/commands$")


# ---------------------------------------------------------------------------
# Registration via the public CPO API (never touches the DB directly)
# ---------------------------------------------------------------------------

def _api_request(method: str, url: str, token: Optional[str] = None,
                 body: Optional[dict] = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            pass
        return e.code, detail


def _cpo_login(api_base: str, email: str, password: str) -> str:
    status, body = _api_request(
        "POST", f"{api_base}/api/auth/login",
        body={"email": email, "password": password},
    )
    if status != 200 or not isinstance(body, dict) or "token" not in body:
        raise SystemExit(f"CPO login failed ({status}): {body}")
    role = (body.get("user") or {}).get("role")
    if role not in ("cpo", "admin"):
        raise SystemExit(
            f"Account {email} is role '{role}', not cpo/admin — it cannot "
            "register gateways/plugs. Use the CPO test account."
        )
    log.info("Logged in as %s (role=%s)", email, role)
    return body["token"]


def register(api_base: str, token: str, gateway_id: str, vpn_ip: str,
             plug_name: str, local_ip: str, group_id: Optional[int]) -> int:
    """Idempotently ensure the fake gateway + plug exist. Returns the plug id."""
    # 1. Gateway — tolerate "already exists" so re-runs are safe.
    status, body = _api_request(
        "POST", f"{api_base}/api/cpo/gateways", token,
        {"gateway_id": gateway_id, "name": f"Fake Plug Gateway ({gateway_id})",
         "vpn_ip": vpn_ip},
    )
    if status == 200:
        log.info("Registered gateway %s", gateway_id)
    elif status == 400 and "already exists" in str(body):
        log.info("Gateway %s already exists — reusing it", gateway_id)
    else:
        raise SystemExit(f"Gateway registration failed ({status}): {body}")

    # 2. Plug — reuse an existing fake plug on this gateway (matched by name) so
    #    re-runs don't pile up duplicate rows.
    status, plugs = _api_request("GET", f"{api_base}/api/cpo/plugs", token)
    if status == 200 and isinstance(plugs, list):
        for p in plugs:
            if p.get("gateway_id") == gateway_id and p.get("name") == plug_name:
                log.info("Reusing existing plug id=%s (%s)", p["id"], plug_name)
                return int(p["id"])

    status, body = _api_request(
        "POST", f"{api_base}/api/cpo/plugs", token,
        {"gateway_id": gateway_id, "name": plug_name, "local_ip": local_ip,
         "plug_model": "tapo_p110", "group_id": group_id},
    )
    if status != 200 or not isinstance(body, dict) or "plug_id" not in body:
        raise SystemExit(f"Plug registration failed ({status}): {body}")
    plug_id = int(body["plug_id"])
    log.info("Registered plug id=%s (%s)", plug_id, plug_name)

    # Cosmetic: flip the plug AVAILABLE so the UI shows it ready (a freshly
    # created plug defaults to OFFLINE; the backend doesn't need this to start a
    # session, but it reads better in the driver/CPO list).
    _api_request("PUT", f"{api_base}/api/cpo/plugs/{plug_id}", token,
                 {"status": "available"})
    return plug_id


# ---------------------------------------------------------------------------
# CLI arg parsing helpers for the repeatable per-plug flags
# ---------------------------------------------------------------------------

def _parse_watts_map(specs: list[str]) -> dict[int, float]:
    """--watts-map PLUGID=WATTS (repeatable, or comma-separated in one arg) ->
    {plug_id: watts}. A plug not in this map falls back to --watts."""
    out: dict[int, float] = {}
    for spec in specs:
        for item in spec.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" not in item:
                raise SystemExit(f"--watts-map {item!r}: expected PLUGID=WATTS")
            pid, watts = item.split("=", 1)
            out[int(pid)] = float(watts)
    return out


def _parse_reset_specs(specs: list[str]) -> dict[int, tuple[float, float]]:
    """--reset-counter PLUGID@SECONDS[@VALUE_KWH] -> {plug_id: (after_s, value_kwh)}.
    Same shape as tools/p110_sim/cli.py's --reset-counter (see its README.md),
    except SECONDS here is measured from when this simulator process started
    (a fake gateway's plugs come and go with the roster, so "since that one
    plug was created" isn't a stable anchor the way it is for p110_sim's
    static plug list)."""
    out: dict[int, tuple[float, float]] = {}
    for spec in specs:
        fields = spec.split("@")
        if len(fields) not in (2, 3):
            raise SystemExit(f"--reset-counter {spec!r}: expected PLUGID@SECONDS[@VALUE_KWH]")
        plug_id = int(fields[0])
        after_s = float(fields[1])
        value_kwh = float(fields[2]) if len(fields) == 3 else 0.0
        out[plug_id] = (after_s, value_kwh)
    return out


# ---------------------------------------------------------------------------
# PlugSim — one simulated plug slot, mirrors firmware's plug_slot_t
# ---------------------------------------------------------------------------

class PlugSim:
    """One simulated plug on the fake gateway. Mirrors the per-plug state
    `firmware/main/main.c` keeps in its `plug_slot_t` slot table: its own
    session/watchdog state and load model. The owning `FakeGateway` guards
    all mutation with its own lock (matching firmware's `plugs_mutex`) — this
    class assumes a single-threaded caller and does no locking itself.
    """

    def __init__(self, plug_id: int, local_ip: str, watts: float, voltage: float,
                 jitter: float, power_factor: float, ramp_seconds: float):
        self.plug_id = plug_id
        self.local_ip = local_ip
        self.watts = watts
        self.voltage = voltage
        self.jitter = jitter
        self.power_factor = power_factor
        self.ramp_seconds = ramp_seconds
        self.max_current_a = DEFAULT_PLUG_CAP_A

        # Session safety-watchdog state (mirrors plug_slot_t).
        self.session_active = False
        self.session_id = ""
        self.session_kwh = 0.0            # session-relative energy, resets on ON
        self.max_kwh = 30.0
        self.max_duration_s = 14400
        self.pending_remove = False       # roster dropped this plug — reaped once idle

        now = time.monotonic()
        self._session_start = now
        self._last_tick = now

        # Optional scheduled --reset-counter event (wall-clock deadline).
        self._reset_deadline: Optional[float] = None
        self._reset_value_kwh = 0.0
        self._reset_fired = False

    def schedule_reset(self, after_s: float, value_kwh: float, start_wall: float) -> None:
        self._reset_deadline = start_wall + after_s
        self._reset_value_kwh = value_kwh
        self._reset_fired = False

    def handle_on(self, payload: dict) -> None:
        """Mirrors firmware's ON handler: (re)baselines the session — a fresh
        session_kwh, start time, and ramp origin. Every ON re-baselines, same
        as firmware (SET_LIMITS is the one that must NOT)."""
        self.session_active = True
        self.session_id = str(payload.get("session_id", "") or "")
        self.max_kwh = float(payload.get("max_kwh", 30.0))
        self.max_duration_s = int(payload.get("max_duration_seconds", 14400))
        cap = payload.get("max_current_a")
        self.max_current_a = (
            float(cap) if isinstance(cap, (int, float)) and cap > 0 else DEFAULT_PLUG_CAP_A
        )
        now = time.monotonic()
        self.session_kwh = 0.0
        self._session_start = now
        self._last_tick = now

    def handle_off(self) -> float:
        """Returns the final session kwh (for logging) and marks idle. The
        stored value is left as-is (harmless) — tick_and_build_telemetry
        always reports 0 while not session_active, matching firmware (whose
        `session_kwh` is a per-sweep local that's simply never assigned when
        no session is active)."""
        final_kwh = self.session_kwh
        self.session_active = False
        return final_kwh

    def handle_set_limits(self, payload: dict) -> bool:
        """Re-caps a RUNNING session's watchdog thresholds WITHOUT
        re-baselining (start_energy/start_time/session_id untouched) — a
        no-op if no session is active. Mirrors firmware exactly, including
        its quirk of defaulting an omitted field to 14400s/30kWh rather than
        leaving it unchanged (the backend always sends both, so this rarely
        bites in practice — see docs/MQTT_CONTRACT.md's SET_LIMITS note)."""
        if not self.session_active:
            return False
        self.max_duration_s = int(payload.get("max_duration_seconds", 14400))
        self.max_kwh = float(payload.get("max_kwh", 30.0))
        cap = payload.get("max_current_a")
        if isinstance(cap, (int, float)) and cap > 0:
            self.max_current_a = float(cap)
        return True

    # ---- load model ----

    def _ramped_watts(self, now: float) -> float:
        target = self.watts
        if self.ramp_seconds > 0:
            elapsed = now - self._session_start
            frac = min(1.0, max(0.0, elapsed / self.ramp_seconds))
            target *= frac
        if self.jitter > 0:
            target *= 1.0 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, target)

    def _measured_current(self, watts: float) -> float:
        # MEASURED current, NOT power/voltage: fold in the power-factor fudge
        # so the fake rig reports the same apparent != active behaviour a
        # real P110 does (see POWER_FACTOR above).
        if self.voltage <= 0 or watts <= 0:
            return 0.0
        return watts / (self.voltage * self.power_factor)

    # ---- safety watchdog + scheduled counter reset ----

    def _maybe_trip_watchdog(self, now: float) -> Optional[str]:
        """Local safety cutoff, like the firmware — same check order
        (duration before energy). Caller has already integrated this tick's
        energy into session_kwh."""
        elapsed = now - self._session_start
        if elapsed >= self.max_duration_s:
            self.session_active = False
            return (f"WATCHDOG plug {self.plug_id}: max duration "
                    f"({self.max_duration_s} s) exceeded — local OFF.")
        if self.session_kwh >= self.max_kwh:
            self.session_active = False
            return (f"WATCHDOG plug {self.plug_id}: energy limit "
                    f"({self.max_kwh:.3f} kWh) reached — local OFF.")
        return None

    def _maybe_apply_scheduled_reset(self, now_wall: float) -> Optional[str]:
        """--reset-counter: force a mid-session drop in the session-relative
        kwh counter, simulating the firmware losing its NVS session baseline
        (e.g. a brownout/crash) — the scenario the backend's
        ENERGY_COUNTER_RESET_DROP_KWH detection (services/mqtt/telemetry.py)
        exists to catch. Fires at most once."""
        if self._reset_deadline is None or self._reset_fired or now_wall < self._reset_deadline:
            return None
        self._reset_fired = True
        old = self.session_kwh
        self.session_kwh = self._reset_value_kwh
        return (f"WARN plug {self.plug_id}: --reset-counter fired "
                f"({old:.4f} -> {self._reset_value_kwh:.4f} kWh) — simulating an "
                "energy-counter regression (see ENERGY_COUNTER_RESET_DROP_KWH).")

    def tick_and_build_telemetry(self, now_mono: float, now_wall: float) -> tuple[dict, Optional[str]]:
        """Advance this plug's state by one tick and return (telemetry payload,
        optional WARN/ERROR line for /logs). Mirrors firmware/main/main.c's
        telemetry_task: the frame that TRIPS the watchdog still reports the
        pre-trip 'occupied' reading — firmware captures `sess_active` BEFORE
        running the watchdog check and only de-energizes the relay after
        publishing, so the *next* tick is the first to report idle.
        """
        dt = now_mono - self._last_tick
        self._last_tick = now_mono
        sess_active_pre = self.session_active
        sid_pre = self.session_id
        log_line = None

        watts = 0.0
        if sess_active_pre:
            watts = self._ramped_watts(now_mono)
            if dt > 0:
                self.session_kwh += (watts / 1000.0) * (dt / 3600.0)
            log_line = self._maybe_apply_scheduled_reset(now_wall) or self._maybe_trip_watchdog(now_mono)

        current = self._measured_current(watts)
        payload = {
            "plug_id": self.plug_id,
            "watts": round(watts, 1),
            # Idle always reports 0 — session-relative, not a frozen last value
            # (matches docs/MQTT_CONTRACT.md's "Idle reports 0" note).
            "kwh": round(self.session_kwh, 4) if sess_active_pre else 0.0,
            "voltage": round(self.voltage, 1),
            "current": round(current, 2),
            "relay": sess_active_pre,
            "status": "occupied" if sess_active_pre else "available",
            "session_id": sid_pre if sess_active_pre else "",
            # TODO(feat/offline-consumption): a sibling branch is adding
            # plug-side day/month consumption counters to telemetry. Once that
            # lands (see docs/MQTT_CONTRACT.md and firmware/main/main.c for the
            # real field shape), mirror it here — e.g. persistent
            # day_kwh/month_kwh counters, modeled like tools/p110_sim's
            # today_energy/month_energy (tools/p110_sim/plug.py). Deliberately
            # NOT guessed/implemented in this change.
        }
        return payload, log_line


# ---------------------------------------------------------------------------
# FakeGateway — the simulator itself, mirrors firmware/main/main.c
# ---------------------------------------------------------------------------

@dataclass
class GatewayConfig:
    gateway_id: str
    broker_host: str
    broker_port: int
    username: str = ""
    password: str = ""
    use_tls: bool = False
    cafile: str = ""
    fw_version: str = DEFAULT_FW
    interval: float = DEFAULT_INTERVAL
    default_watts: float = DEFAULT_WATTS
    voltage: float = DEFAULT_VOLTAGE
    jitter: float = DEFAULT_JITTER
    power_factor: float = POWER_FACTOR
    ramp_seconds: float = DEFAULT_RAMP_SECONDS
    watts_map: dict[int, float] = field(default_factory=dict)
    reset_specs: dict[int, tuple[float, float]] = field(default_factory=dict)
    bootstrap_plug_id: Optional[int] = None
    bootstrap_local_ip: str = DEFAULT_LOCAL_IP


class FakeGateway:
    def __init__(self, cfg: GatewayConfig):
        import paho.mqtt.client as mqtt  # imported here so --self-test needs no dep

        self._mqtt = mqtt
        self.cfg = cfg
        self.interval = cfg.interval   # mutable — SET_INTERVAL updates this gateway-wide

        gw = cfg.gateway_id
        self.status_topic = f"amphive/gateways/{gw}/status"
        self.telemetry_topic = f"amphive/gateways/{gw}/telemetry"
        self.command_topic = f"amphive/gateways/{gw}/plugs/+/commands"
        self.config_topic = f"amphive/gateways/{gw}/config"
        self.alarms_topic = f"amphive/gateways/{gw}/alarms"
        self.logs_topic = f"amphive/gateways/{gw}/logs"

        # plugs + all mutable session state guarded by _lock (mirrors
        # firmware's plugs_mutex) — commands land on the paho network thread,
        # telemetry is built/published from the main thread.
        self._lock = threading.Lock()
        self.plugs: dict[int, PlugSim] = {}
        self._connected = False
        self._stop = threading.Event()
        self._boot_mono = time.monotonic()   # for /logs' ESP_LOG-style "(ms)" prefix
        self._start_wall = time.time()       # anchor for --reset-counter deadlines

        self.client = mqtt.Client(
            client_id=f"fake_plug_{gw}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if cfg.username:
            self.client.username_pw_set(cfg.username, cfg.password)
        if cfg.use_tls:
            self.client.tls_set(ca_certs=cfg.cafile or None)
            # Self-signed CA with an IP-SAN cert — skip hostname matching like
            # the firmware does (it validates the chain + IP SAN, not a hostname).
            self.client.tls_insecure_set(True)
        # Offline LWT, exactly like the firmware.
        self.client.will_set(self.status_topic, '{"status":"offline"}', qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # Optional bootstrap plug so telemetry flows even before any retained
        # roster arrives (or if the broker/backend never publishes one for
        # this gateway_id — e.g. a bench gateway_id with no DB row yet).
        if cfg.bootstrap_plug_id is not None:
            self._get_or_create_plug(cfg.bootstrap_plug_id, cfg.bootstrap_local_ip)

    # --- MQTT callbacks (network thread) ---

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False) or reason_code != 0:
            log.error("Connect failed: %s", reason_code)
            return
        self._connected = True
        log.info("Connected to broker %s:%s as gateway %s",
                 self.cfg.broker_host, self.cfg.broker_port, self.cfg.gateway_id)
        # Announce online (retained, with fw version) + subscribe to commands
        # and the retained multi-plug roster — re-runs on every (re)connect,
        # mirroring the firmware.
        client.publish(self.status_topic,
                       json.dumps({"status": "online", "fw": self.cfg.fw_version}),
                       qos=1, retain=True)
        client.subscribe(self.command_topic, qos=1)
        client.subscribe(self.config_topic, qos=1)
        log.info("Subscribed to %s and %s", self.command_topic, self.config_topic)

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False
        log.warning("Disconnected from broker (will auto-reconnect)")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic if isinstance(msg.topic, str) else msg.topic.decode()
        raw = msg.payload.decode("utf-8", "ignore") if isinstance(msg.payload, (bytes, bytearray)) else str(msg.payload)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            text = "Command JSON parse failed; ignoring payload."
            log.warning("%s (topic=%s)", text, topic)
            self._publish_log("W", text)
            return
        if not isinstance(payload, dict):
            return

        # Retained plug roster — its own topic, distinct from the per-plug
        # command wildcard. Handle and return.
        if topic.endswith("/config"):
            self._apply_roster(payload)
            return

        action = str(payload.get("action", "")).upper()
        cmd_plug_id = self._parse_plug_id_from_topic(topic)
        local_ip = payload.get("local_ip") or None
        local_ip = local_ip if isinstance(local_ip, str) else None

        if action == "ON" and cmd_plug_id is not None:
            self._handle_on(cmd_plug_id, local_ip, payload)
        elif action == "OFF" and cmd_plug_id is not None:
            self._handle_off(cmd_plug_id, local_ip)
        elif action == "SET_LIMITS" and cmd_plug_id is not None:
            self._handle_set_limits(cmd_plug_id, local_ip, payload)
        elif action == "SET_INTERVAL":
            self._handle_set_interval(payload)
        elif action == "OTA":
            self._handle_ota(payload)
        else:
            log.warning("Unrecognized command action %r on %s", action, topic)

    @staticmethod
    def _parse_plug_id_from_topic(topic: str) -> Optional[int]:
        m = _PLUG_ID_FROM_TOPIC_RE.search(topic)
        return int(m.group(1)) if m else None

    # --- plug slot table (caller of _get_or_create_plug must hold _lock) ---

    def _get_or_create_plug(self, plug_id: int, local_ip: Optional[str]) -> Optional[PlugSim]:
        """Find (and optionally re-IP) an existing slot, or allocate a new one
        (up to MAX_PLUGS) when an IP is known. Mirrors firmware's
        slot_get_locked. Caller holds self._lock."""
        plug = self.plugs.get(plug_id)
        if plug is not None:
            if local_ip:
                plug.local_ip = local_ip
            plug.pending_remove = False   # named again -> keep
            return plug
        if not local_ip:
            log.warning("No IP for plug %d (not in payload, no provisioned target)", plug_id)
            return None
        if len(self.plugs) >= MAX_PLUGS:
            text = f"Plug slot table full ({MAX_PLUGS}); ignoring plug {plug_id}"
            log.warning(text)
            self._publish_log("W", text)
            return None
        plug = PlugSim(
            plug_id=plug_id, local_ip=local_ip,
            watts=self.cfg.watts_map.get(plug_id, self.cfg.default_watts),
            voltage=self.cfg.voltage, jitter=self.cfg.jitter,
            power_factor=self.cfg.power_factor, ramp_seconds=self.cfg.ramp_seconds,
        )
        reset_spec = self.cfg.reset_specs.get(plug_id)
        if reset_spec is not None:
            plug.schedule_reset(reset_spec[0], reset_spec[1], self._start_wall)
        self.plugs[plug_id] = plug
        log.info("Tracking plug %d @ %s (%d/%d slots)", plug_id, local_ip, len(self.plugs), MAX_PLUGS)
        return plug

    def _apply_roster(self, payload: dict) -> None:
        """Applies a retained plug roster (amphive/gateways/{gw}/config): the
        backend's full list of this gateway's plugs
        {plug_id, local_ip, max_current_a}. Mirrors firmware's
        handle_plug_roster — proactively (re)builds the slot table so idle
        telemetry flows for every plug without waiting for a command, and
        flags (never immediately frees) plugs the roster dropped."""
        entries = payload.get("plugs")
        if not isinstance(entries, list):
            log.warning("Roster has no 'plugs' array; ignoring.")
            return

        seen: set[int] = set()
        with self._lock:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                plug_id_raw = entry.get("plug_id")
                local_ip = entry.get("local_ip")
                if not isinstance(plug_id_raw, (int, float)) or not isinstance(local_ip, str) or not local_ip:
                    continue
                plug_id = int(plug_id_raw)
                plug = self._get_or_create_plug(plug_id, local_ip)
                if plug is None:
                    continue
                cap = entry.get("max_current_a")
                if not plug.session_active and isinstance(cap, (int, float)) and cap > 0:
                    plug.max_current_a = float(cap)
                seen.add(plug_id)

            for plug_id, plug in self.plugs.items():
                if plug.session_active or plug.pending_remove:
                    continue
                if plug_id not in seen:
                    plug.pending_remove = True
                    log.info("Plug %d dropped from roster — flagged for removal", plug_id)

        log.info("Applied plug roster: %d plug(s)", len(seen))

    def _reap_pending(self) -> None:
        with self._lock:
            dead = [pid for pid, p in self.plugs.items() if p.pending_remove and not p.session_active]
            for pid in dead:
                del self.plugs[pid]
        for pid in dead:
            log.info("Reaped plug %d (dropped from roster)", pid)

    # --- command handlers ---

    def _handle_on(self, plug_id: int, local_ip: Optional[str], payload: dict) -> None:
        with self._lock:
            plug = self._get_or_create_plug(plug_id, local_ip)
            if plug is None:
                log.warning("ON for plug %d dropped: no slot/IP available.", plug_id)
                return
            plug.handle_on(payload)
            watts, ramp_s, max_kwh, max_dur, cap, sid = (
                plug.watts, plug.ramp_seconds, plug.max_kwh, plug.max_duration_s,
                plug.max_current_a, plug.session_id,
            )
        log.info("ON  -> plug %d session_id=%s, ramping to %.0f W over %.1fs "
                 "(limits: %.1f kWh / %d s, cap %.1f A)",
                 plug_id, sid or "(none)", watts, ramp_s, max_kwh, max_dur, cap)

    def _handle_off(self, plug_id: int, local_ip: Optional[str]) -> None:
        with self._lock:
            plug = self._get_or_create_plug(plug_id, local_ip)
            if plug is None:
                return
            was_on = plug.session_active
            final_kwh = plug.handle_off()
        if was_on:
            log.info("OFF -> plug %d relay de-energized (session used %.4f kWh)", plug_id, final_kwh)

    def _handle_set_limits(self, plug_id: int, local_ip: Optional[str], payload: dict) -> None:
        with self._lock:
            plug = self._get_or_create_plug(plug_id, local_ip)
            applied = bool(plug) and plug.handle_set_limits(payload)
            max_kwh = plug.max_kwh if plug else None
            max_dur = plug.max_duration_s if plug else None
        if applied:
            log.info("SET_LIMITS -> plug %d: %.1f kWh / %d s (session preserved, no re-baseline)",
                     plug_id, max_kwh, max_dur)
        else:
            log.info("SET_LIMITS for plug %d ignored: no active session.", plug_id)

    def _handle_set_interval(self, payload: dict) -> None:
        try:
            ms = int(payload.get("interval_ms", 0))
        except (TypeError, ValueError):
            return
        ms = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, ms))
        self.interval = ms / 1000.0   # gateway-wide poll cadence
        log.info("SET_INTERVAL -> telemetry every %d ms", ms)

    def _handle_ota(self, payload: dict) -> None:
        url = payload.get("url")
        with self._lock:
            any_active = any(p.session_active for p in self.plugs.values())
        if not url:
            log.warning("OTA command missing 'url'; ignoring.")
            return
        if any_active:
            text = "OTA refused: a charging session is active."
            log.warning(text)
            self._publish_log("W", text)
            self._publish_alarm_event("OTA_REFUSED_SESSION_ACTIVE")
            return
        log.info("OTA command received (url=%s) — no-op on a fake plug (no firmware to flash).", url)

    # --- outbound: /logs and /alarms (best-effort, silent when offline) ---

    def _publish_log(self, level: str, text: str) -> None:
        if not self._connected or self.client is None:
            return
        ms = int((time.monotonic() - self._boot_mono) * 1000)
        # Mirrors real firmware's ESP_LOG line shape ("<LEVEL> (<ms>) <tag>: <msg>")
        # since the real log-forwarder ships the raw line, not a JSON envelope.
        line = f"{level} ({ms}) amphive_gateway: {text}"
        self.client.publish(self.logs_topic, line, qos=0)

    def _publish_alarm_event(self, event: str) -> None:
        if not self._connected or self.client is None:
            return
        self.client.publish(self.alarms_topic, json.dumps({"event": event}), qos=1)

    # --- telemetry loop (main thread) ---

    def run(self) -> None:
        self.client.connect_async(self.cfg.broker_host, self.cfg.broker_port, keepalive=60)
        self.client.loop_start()
        log.info("Fake gateway '%s' running — publishing telemetry every %.1fs. Ctrl-C to stop.",
                 self.cfg.gateway_id, self.interval)
        try:
            while not self._stop.is_set():
                self._reap_pending()
                now_mono = time.monotonic()
                now_wall = time.time()
                with self._lock:
                    plug_ids = sorted(self.plugs.keys())
                for plug_id in plug_ids:
                    with self._lock:
                        plug = self.plugs.get(plug_id)
                        if plug is None:
                            continue
                        payload, trip_log = plug.tick_and_build_telemetry(now_mono, now_wall)
                    if self._connected:
                        self.client.publish(self.telemetry_topic, json.dumps(payload), qos=0)
                    log.info("telemetry: %s", json.dumps(payload))
                    if trip_log:
                        log.error(trip_log)
                        self._publish_log("E", trip_log)
                self._stop.wait(self.interval)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self.client.publish(self.status_topic, '{"status":"offline"}',
                                qos=1, retain=True)
            time.sleep(0.2)  # give the publish a moment to flush
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        log.info("Fake gateway stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _self_test(args) -> None:
    """Print the exact wire payloads without any network — a contract check."""
    plug_id = args.plug_id or 2

    print("Status (online, retained):")
    print("  " + json.dumps({"status": "online", "fw": args.fw_version}))
    print("Status (offline LWT):")
    print("  " + json.dumps({"status": "offline"}))

    print("\nRetained multi-plug roster the gateway subscribes to on connect")
    print("(amphive/gateways/<gw>/config — drives which plugs get simulated):")
    print("  " + json.dumps({"v": 1, "plugs": [
        {"plug_id": plug_id, "local_ip": args.local_ip, "max_current_a": DEFAULT_PLUG_CAP_A},
    ]}))

    print("\nSET_LIMITS re-caps a RUNNING session without re-baselining (a no-op if none active):")
    print("  <- " + json.dumps({"action": "SET_LIMITS", "max_kwh": 20.0,
                                 "max_duration_seconds": 7200, "max_current_a": 12.0}))

    print("\nTelemetry while charging (ramping to %.0f W over %.1fs, %.0f%% jitter):"
          % (args.watts, args.ramp_seconds, args.jitter * 100))
    # MEASURED current includes the power-factor fudge (see POWER_FACTOR), so it
    # is deliberately NOT the naive power/voltage apparent figure.
    measured_current = args.watts / (args.voltage * args.power_factor) if args.voltage else 0.0
    naive_current = args.watts / args.voltage if args.voltage else 0.0
    assert measured_current > naive_current, (
        "measured current should exceed naive power/voltage (PF < 1)"
    )
    kwh = (args.watts / 1000.0) * (args.interval / 3600.0)
    print("  " + json.dumps({
        "plug_id": plug_id, "watts": round(args.watts, 1),
        "kwh": round(kwh, 4), "voltage": round(args.voltage, 1),
        "current": round(measured_current, 2),
        "relay": True, "status": "occupied", "session_id": "42",
    }))
    print("  (measured current %.2f A vs naive P/V %.2f A — power factor %.2f)"
          % (measured_current, naive_current, args.power_factor))
    print("Telemetry while idle (kwh always 0 — session-relative, not a frozen last value):")
    print("  " + json.dumps({
        "plug_id": plug_id, "watts": 0.0, "kwh": 0.0,
        "voltage": round(args.voltage, 1), "current": 0.0,
        "relay": False, "status": "available", "session_id": "",
    }))

    print("\nA sample /logs line for a local watchdog trip (WARN/ERROR only, matching")
    print("firmware's log-forward filter — see amphive/gateways/<gw>/logs):")
    print("  E (123456) amphive_gateway: WATCHDOG plug %d: energy limit (30.000 kWh) "
          "reached — local OFF." % plug_id)

    print("\nAt %.0f W: %.4f kWh per %.1fs tick, %.3f kWh/min, %.1f kWh/hour (post-ramp)."
          % (args.watts, kwh, args.interval,
             (args.watts / 1000.0) / 60.0, args.watts / 1000.0))
    print("Topics:")
    print("  status    -> amphive/gateways/%s/status" % args.gateway_id)
    print("  telemetry -> amphive/gateways/%s/telemetry" % args.gateway_id)
    print("  logs      -> amphive/gateways/%s/logs" % args.gateway_id)
    print("  alarms    -> amphive/gateways/%s/alarms" % args.gateway_id)
    print("  commands  <- amphive/gateways/%s/plugs/+/commands" % args.gateway_id)
    print("  config    <- amphive/gateways/%s/config (retained multi-plug roster)" % args.gateway_id)


def main():
    ap = argparse.ArgumentParser(
        description="AmpHive fake plug simulator (ESP32 gateway + N P110 plugs stand-in).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--register-only", action="store_true",
                      help="Provision the gateway/plug via the API, print the plug id, exit.")
    mode.add_argument("--run-only", action="store_true",
                      help="Skip provisioning and just simulate.")
    mode.add_argument("--self-test", action="store_true",
                      help="Print the wire payloads and exit (no network).")

    # Load / cadence
    ap.add_argument("--watts", type=float, default=DEFAULT_WATTS,
                    help="Default steady-state load in watts while a plug is charging "
                         "(10000 = 10 kW). Overridden per-plug by --watts-map.")
    ap.add_argument("--watts-map", action="append", default=[], metavar="PLUGID=WATTS",
                    help="Repeatable/CSV. Per-plug wattage override for plugs learned from "
                         "the roster or a command (e.g. '2=11000,3=3300').")
    ap.add_argument("--voltage", type=float, default=DEFAULT_VOLTAGE)
    ap.add_argument("--power-factor", type=float, default=POWER_FACTOR,
                    help="Simulated load power factor (< 1 so reported current != watts/voltage).")
    ap.add_argument("--jitter", type=float, default=DEFAULT_JITTER,
                    help="Fractional load noise on top of the ramped watts (0.02 = +/-2%%).")
    ap.add_argument("--ramp-seconds", type=float, default=DEFAULT_RAMP_SECONDS,
                    help="Seconds to linearly ramp watts from 0 to target after ON "
                         "(soft-start realism; 0 = instant, the old v1 behavior).")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="Seconds between telemetry publishes (gateway-wide default; "
                         "a SET_INTERVAL command overrides it at runtime, like firmware).")
    ap.add_argument("--fw-version", default=DEFAULT_FW,
                    help="Firmware version string reported in the retained online status.")
    ap.add_argument("--reset-counter", action="append", default=[],
                    metavar="PLUGID@SECONDS[@VALUE_KWH]",
                    help="Repeatable. SECONDS after this simulator starts, force that plug's "
                         "session-relative kwh counter to drop to VALUE_KWH (default 0) mid-"
                         "session -- simulates the firmware losing its NVS session baseline, "
                         "for exercising the backend's ENERGY_COUNTER_RESET_DROP_KWH detection "
                         "(backend/services/mqtt/telemetry.py). Same shape as tools/p110_sim's "
                         "--reset-counter.")

    # Identity / provisioning
    ap.add_argument("--gateway-id", default=DEFAULT_GATEWAY_ID)
    ap.add_argument("--plug-id", type=int, default=None,
                    help="Bootstrap plug id to simulate immediately at startup, before any "
                         "retained roster arrives on .../config. Optional: once connected, the "
                         "gateway also builds its slot table from the backend's retained multi-"
                         "plug roster, exactly like real firmware (fw >= 2.0.0-direct) -- so "
                         "this is a seed/fallback now, not the only way to get a plug simulated.")
    ap.add_argument("--api-base", default=DEFAULT_API_BASE)
    ap.add_argument("--cpo-email", default=os.getenv("AMPHIVE_CPO_EMAIL", ""))
    ap.add_argument("--cpo-password", default=os.getenv("AMPHIVE_CPO_PASSWORD", ""))
    ap.add_argument("--vpn-ip", default=DEFAULT_VPN_IP,
                    help="Legacy/overlay-only field on gateway registration; direct-MQTT "
                         "gateways don't use it (optional -- empty falls back to gateway_id).")
    ap.add_argument("--plug-name", default=DEFAULT_PLUG_NAME)
    ap.add_argument("--local-ip", default=DEFAULT_LOCAL_IP,
                    help="LAN IP to register/bootstrap the plug with. Cosmetic for this "
                         "MQTT-level simulator -- no real HTTP/KLAP calls are ever made "
                         "(see tools/p110_sim for a plug-level emulator that does).")
    ap.add_argument("--group-id", type=int, default=DEFAULT_GROUP_ID,
                    help="Charger group id to place the plug in (1 = public).")

    # Broker
    ap.add_argument("--broker-host", default=os.getenv("MQTT_BROKER_HOST", "localhost"),
                    help="Public direct-MQTT broker: mqtt.amphive.app (with --tls). Inside the "
                         "relay VM's compose network: 'mqtt' (plaintext, port 1883).")
    ap.add_argument("--broker-port", type=int,
                    default=int(os.getenv("MQTT_BROKER_PORT", "1883")))
    ap.add_argument("--broker-user",
                    default=os.getenv("MQTT_GW_USERNAME") or os.getenv("MQTT_USERNAME", ""))
    ap.add_argument("--broker-pass",
                    default=os.getenv("MQTT_GW_PASSWORD") or os.getenv("MQTT_PASSWORD", ""))
    ap.add_argument("--tls", action="store_true", help="Use the TLS listener (8883).")
    ap.add_argument("--cafile", default=os.getenv("MQTT_CA_FILE", ""),
                    help="CA cert for --tls (validates the broker chain).")

    args = ap.parse_args()

    if args.self_test:
        _self_test(args)
        return

    # --- Provision (unless run-only) ---
    plug_id = args.plug_id
    if not args.run_only:
        if not args.cpo_email or not args.cpo_password:
            raise SystemExit(
                "Registration needs CPO credentials: pass --cpo-email/--cpo-password "
                "(or AMPHIVE_CPO_EMAIL/AMPHIVE_CPO_PASSWORD). See TEST_ACCOUNTS.local.txt. "
                "Or skip registration with --run-only."
            )
        token = _cpo_login(args.api_base, args.cpo_email, args.cpo_password)
        plug_id = register(args.api_base, token, args.gateway_id, args.vpn_ip,
                           args.plug_name, args.local_ip,
                           args.group_id if args.group_id > 0 else None)
        print(f"\n[OK] Fake plug ready: gateway={args.gateway_id} plug_id={plug_id} "
              f"(group {args.group_id})")

    if args.register_only:
        print("Registration done. Run the simulator with:")
        print(f"  python tools/fake_plug.py --run-only --plug-id {plug_id} "
              f"--gateway-id {args.gateway_id} --broker-host mqtt.amphive.app --tls "
              f"--broker-user {args.broker_user or args.gateway_id} --broker-pass '<pw>'")
        return

    if not args.broker_user:
        log.warning("No broker credentials given; the broker enforces auth and "
                    "will reject an anonymous client.")

    if plug_id is None and not args.watts_map:
        log.warning("No --plug-id bootstrap given; waiting for the backend's retained "
                    "roster on amphive/gateways/%s/config before any plug is simulated "
                    "(this is expected for a freshly-provisioned gateway).", args.gateway_id)

    cfg = GatewayConfig(
        gateway_id=args.gateway_id,
        broker_host=args.broker_host, broker_port=args.broker_port,
        username=args.broker_user, password=args.broker_pass,
        use_tls=args.tls, cafile=args.cafile,
        fw_version=args.fw_version, interval=args.interval,
        default_watts=args.watts, voltage=args.voltage,
        jitter=args.jitter, power_factor=args.power_factor,
        ramp_seconds=args.ramp_seconds,
        watts_map=_parse_watts_map(args.watts_map),
        reset_specs=_parse_reset_specs(args.reset_counter),
        bootstrap_plug_id=plug_id, bootstrap_local_ip=args.local_ip,
    )
    gateway = FakeGateway(cfg)
    signal.signal(signal.SIGINT, lambda *_: gateway.shutdown())
    try:
        signal.signal(signal.SIGTERM, lambda *_: gateway.shutdown())
    except (ValueError, AttributeError):
        pass  # SIGTERM not settable on some platforms/threads
    gateway.run()


if __name__ == "__main__":
    main()
