#!/usr/bin/env python3
"""
AmpHive — Fake Plug Simulator
=============================
A software stand-in for an ESP32 gateway + Tapo P110 plug, so the full stack
(session start/stop, billing, live Socket.io telemetry, the driver/CPO UI) can
be exercised **without physical hardware**.

It speaks the exact same MQTT contract as `firmware/main/main.c` (see
`docs/MQTT_CONTRACT.md`):

  * publishes a retained `online` status + an `offline` LWT on
    `amphive/gateways/{gw}/status`
  * subscribes to `amphive/gateways/{gw}/plugs/+/commands` and honours
    ON / OFF / SET_INTERVAL (OTA is acknowledged but no-op'd — a fake plug has
    no firmware to flash)
  * publishes telemetry on `amphive/gateways/{gw}/telemetry` every
    `--interval` seconds

When a session is ON it draws a **constant load** (default 10 kW, `--watts`) and
integrates session-relative energy exactly like the firmware does, so the wallet
debits and the live cost tick up predictably. At 10 kW that's ~0.167 kWh/min
(≈0.83 coins/min at the default COINS_PER_KWH=5).

Registration (creating the gateway + plug rows) goes through the public CPO API,
not the DB, so this never touches the database directly.

Modes
-----
  (default)        register the fake gateway/plug via the API (idempotent),
                   then run the MQTT simulator.
  --register-only  just provision the DB rows via the API and print the plug id,
                   then exit. Run this from anywhere with API access.
  --run-only       skip provisioning and just simulate. Requires --plug-id.
                   Run this where the broker is reachable.
  --self-test      print the telemetry/status/command payloads and exit. No
                   network at all — a quick contract sanity check.

Where to run it
---------------
The broker is bound to the overlay (`MQTT_BIND_IP`, e.g. 100.87.241.70) and is
NOT public, so the MQTT part must run somewhere that can reach it:
  * on the GCP VM host:   --broker-host 100.87.241.70   (the VM's own overlay IP)
  * inside the compose network (as a service): --broker-host mqtt
The registration part uses the public API and works from anywhere.

Examples
--------
  # One-time: create the fake gateway + plug (run from your workstation)
  python tools/fake_plug.py --register-only \
      --cpo-email cpo@amphive.test --cpo-password '<pw>'

  # Keep the fake plug live (run on the VM, points at the overlay broker)
  python tools/fake_plug.py --run-only --plug-id 7 \
      --gateway-id fakeplug-gw-01 --broker-host 100.87.241.70 \
      --broker-user amphive-gateway --broker-pass '<pw>'

Dependencies: paho-mqtt (`pip install paho-mqtt`). Everything else is stdlib.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fake_plug")

# Defaults chosen to line up with the seeded test fixture (see
# TEST_ACCOUNTS.local.txt / docs/TESTING.md).
DEFAULT_API_BASE = os.getenv("AMPHIVE_API_BASE", "http://8.231.81.12:8000")
DEFAULT_GATEWAY_ID = os.getenv("FAKE_PLUG_GATEWAY_ID", "fakeplug-gw-01")
DEFAULT_VPN_IP = os.getenv("FAKE_PLUG_VPN_IP", "100.64.0.201")
DEFAULT_PLUG_NAME = os.getenv("FAKE_PLUG_NAME", "Fake Test Plug (10kW)")
DEFAULT_LOCAL_IP = os.getenv("FAKE_PLUG_LOCAL_IP", "10.0.0.99")
DEFAULT_GROUP_ID = int(os.getenv("FAKE_PLUG_GROUP_ID", "1"))  # public group
DEFAULT_WATTS = float(os.getenv("FAKE_PLUG_WATTS", "10000"))  # 10 kW constant load
DEFAULT_VOLTAGE = float(os.getenv("FAKE_PLUG_VOLTAGE", "230"))
DEFAULT_INTERVAL = float(os.getenv("FAKE_PLUG_INTERVAL", "5"))
DEFAULT_FW = "fake-1.0.0"

# Match the firmware's SET_INTERVAL clamp (ms).
MIN_INTERVAL_MS = 500
MAX_INTERVAL_MS = 60000


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
# The simulator itself — mirrors firmware/main/main.c
# ---------------------------------------------------------------------------

class FakePlug:
    def __init__(self, broker_host: str, broker_port: int, username: str,
                 password: str, gateway_id: str, plug_id: int, watts: float,
                 voltage: float, interval: float, use_tls: bool, cafile: str):
        import paho.mqtt.client as mqtt  # imported here so --self-test needs no dep

        self._mqtt = mqtt
        self.gateway_id = gateway_id
        self.plug_id = plug_id
        self.watts = watts
        self.voltage = voltage
        self.interval = interval
        self.broker_host = broker_host
        self.broker_port = broker_port

        self.status_topic = f"amphive/gateways/{gateway_id}/status"
        self.telemetry_topic = f"amphive/gateways/{gateway_id}/telemetry"
        self.command_topic = f"amphive/gateways/{gateway_id}/plugs/+/commands"

        # Session state (guarded by _lock — commands land on the network thread,
        # telemetry is published from the main thread).
        self._lock = threading.Lock()
        self.relay_on = False           # is the fake relay energized?
        self.session_id = ""            # backend session id, echoed in telemetry
        self.session_kwh = 0.0          # session-relative energy (reset on ON)
        self.max_kwh = 30.0             # local watchdog limits from the ON command
        self.max_duration_s = 14400
        self._session_start = 0.0       # monotonic
        self._last_tick = time.monotonic()
        self._stop = threading.Event()

        self.client = mqtt.Client(
            client_id=f"fake_plug_{gateway_id}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if username:
            self.client.username_pw_set(username, password)
        if use_tls:
            self.client.tls_set(ca_certs=cafile or None)
            # Self-signed CA with an IP-SAN cert — skip hostname matching like
            # the firmware does (it validates the chain + IP SAN, not a hostname).
            self.client.tls_insecure_set(True)
        # Offline LWT, exactly like the firmware.
        self.client.will_set(self.status_topic, '{"status":"offline"}', qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # --- MQTT callbacks (network thread) ---

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False) or reason_code != 0:
            log.error("Connect failed: %s", reason_code)
            return
        log.info("Connected to broker %s:%s as gateway %s",
                 self.broker_host, self.broker_port, self.gateway_id)
        # Announce online (retained, with fw version) + subscribe to commands —
        # re-runs on every (re)connect, mirroring the firmware.
        client.publish(self.status_topic,
                       json.dumps({"status": "online", "fw": DEFAULT_FW}),
                       qos=1, retain=True)
        client.subscribe(self.command_topic, qos=1)
        log.info("Subscribed to %s", self.command_topic)

    def _on_disconnect(self, client, userdata, *args):
        log.warning("Disconnected from broker (will auto-reconnect)")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", "ignore"))
        except json.JSONDecodeError:
            log.warning("Ignoring non-JSON command on %s", msg.topic)
            return
        action = str(payload.get("action", "")).upper()
        if action == "ON":
            self._handle_on(payload)
        elif action == "OFF":
            self._handle_off()
        elif action == "SET_INTERVAL":
            self._handle_set_interval(payload)
        elif action == "OTA":
            log.info("OTA command received (url=%s) — no-op on a fake plug",
                     payload.get("url"))
        else:
            log.warning("Unknown command action %r", action)

    def _handle_on(self, payload: dict):
        with self._lock:
            self.relay_on = True
            self.session_kwh = 0.0            # session-relative meter resets
            self.session_id = str(payload.get("session_id", "") or "")
            self.max_kwh = float(payload.get("max_kwh", 30.0))
            self.max_duration_s = int(payload.get("max_duration_seconds", 14400))
            self._session_start = time.monotonic()
            self._last_tick = time.monotonic()
        log.info("ON  -> session_id=%s, drawing %.0f W (limits: %.1f kWh / %d s)",
                 self.session_id or "(none)", self.watts, self.max_kwh,
                 self.max_duration_s)

    def _handle_off(self):
        with self._lock:
            was_on = self.relay_on
            self.relay_on = False
            final = self.session_kwh
        if was_on:
            log.info("OFF -> relay de-energized (session used %.4f kWh)", final)

    def _handle_set_interval(self, payload: dict):
        try:
            ms = int(payload.get("interval_ms", 0))
        except (TypeError, ValueError):
            return
        ms = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, ms))
        self.interval = ms / 1000.0
        log.info("SET_INTERVAL -> telemetry every %.0f ms", ms * 1.0)

    # --- Telemetry loop (main thread) ---

    def _build_telemetry(self) -> dict:
        """Integrate energy since the last tick and return the payload dict."""
        now = time.monotonic()
        with self._lock:
            dt = now - self._last_tick
            self._last_tick = now
            if self.relay_on and dt > 0:
                # kWh += kW * hours
                self.session_kwh += (self.watts / 1000.0) * (dt / 3600.0)
                self._maybe_trip_watchdog(now)
            watts = self.watts if self.relay_on else 0.0
            current = watts / self.voltage if self.voltage else 0.0
            return {
                "plug_id": self.plug_id,
                "watts": round(watts, 1),
                "kwh": round(self.session_kwh, 4),
                "voltage": round(self.voltage, 1),
                "current": round(current, 2),
                "status": "occupied" if self.relay_on else "available",
                "session_id": self.session_id if self.relay_on else "",
            }

    def _maybe_trip_watchdog(self, now: float):
        """Local safety cutoff, like the firmware. Caller holds _lock. The
        accrued energy is kept (frozen) so the backend still bills it — we only
        de-energize the relay."""
        elapsed = now - self._session_start
        if self.session_kwh >= self.max_kwh:
            log.error("WATCHDOG: %.4f kWh >= limit %.1f -- cutting off",
                      self.session_kwh, self.max_kwh)
            self.relay_on = False
        elif elapsed >= self.max_duration_s:
            log.error("WATCHDOG: %.0f s >= limit %d -- cutting off",
                      elapsed, self.max_duration_s)
            self.relay_on = False

    def run(self):
        self.client.connect_async(self.broker_host, self.broker_port, keepalive=60)
        self.client.loop_start()
        log.info("Fake plug running — publishing telemetry every %.1fs. Ctrl-C to stop.",
                 self.interval)
        try:
            while not self._stop.is_set():
                payload = self._build_telemetry()
                self.client.publish(self.telemetry_topic, json.dumps(payload), qos=0)
                log.info("telemetry: %s", json.dumps(payload))
                self._stop.wait(self.interval)
        finally:
            self.shutdown()

    def shutdown(self):
        self._stop.set()
        try:
            self.client.publish(self.status_topic, '{"status":"offline"}',
                                qos=1, retain=True)
            time.sleep(0.2)  # give the publish a moment to flush
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        log.info("Fake plug stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _self_test(args) -> None:
    """Print the exact wire payloads without any network — a contract check."""
    print("Status (online, retained):")
    print("  " + json.dumps({"status": "online", "fw": DEFAULT_FW}))
    print("Status (offline LWT):")
    print("  " + json.dumps({"status": "offline"}))
    print("\nTelemetry while charging (constant %.0f W load):" % args.watts)
    kwh = (args.watts / 1000.0) * (args.interval / 3600.0)
    print("  " + json.dumps({
        "plug_id": args.plug_id or 1, "watts": round(args.watts, 1),
        "kwh": round(kwh, 4), "voltage": round(args.voltage, 1),
        "current": round(args.watts / args.voltage, 2),
        "status": "occupied", "session_id": "42",
    }))
    print("Telemetry while idle:")
    print("  " + json.dumps({
        "plug_id": args.plug_id or 1, "watts": 0.0, "kwh": 0.0,
        "voltage": round(args.voltage, 1), "current": 0.0,
        "status": "available", "session_id": "",
    }))
    print("\nAt %.0f W: %.4f kWh per %.1fs tick, %.3f kWh/min, %.1f kWh/hour."
          % (args.watts, kwh, args.interval,
             (args.watts / 1000.0) / 60.0, args.watts / 1000.0))
    print("Topics:")
    print("  status    -> amphive/gateways/%s/status" % args.gateway_id)
    print("  telemetry -> amphive/gateways/%s/telemetry" % args.gateway_id)
    print("  commands  <- amphive/gateways/%s/plugs/+/commands" % args.gateway_id)


def main():
    ap = argparse.ArgumentParser(
        description="AmpHive fake plug simulator (ESP32 + P110 stand-in).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--register-only", action="store_true",
                      help="Provision the gateway/plug via the API, print the plug id, exit.")
    mode.add_argument("--run-only", action="store_true",
                      help="Skip provisioning and just simulate (needs --plug-id).")
    mode.add_argument("--self-test", action="store_true",
                      help="Print the wire payloads and exit (no network).")

    # Load / cadence
    ap.add_argument("--watts", type=float, default=DEFAULT_WATTS,
                    help="Constant load in watts while charging (10000 = 10 kW).")
    ap.add_argument("--voltage", type=float, default=DEFAULT_VOLTAGE)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="Seconds between telemetry publishes.")

    # Identity / provisioning
    ap.add_argument("--gateway-id", default=DEFAULT_GATEWAY_ID)
    ap.add_argument("--plug-id", type=int, default=None,
                    help="DB plug id. Required for --run-only; auto-filled otherwise.")
    ap.add_argument("--api-base", default=DEFAULT_API_BASE)
    ap.add_argument("--cpo-email", default=os.getenv("AMPHIVE_CPO_EMAIL", ""))
    ap.add_argument("--cpo-password", default=os.getenv("AMPHIVE_CPO_PASSWORD", ""))
    ap.add_argument("--vpn-ip", default=DEFAULT_VPN_IP)
    ap.add_argument("--plug-name", default=DEFAULT_PLUG_NAME)
    ap.add_argument("--local-ip", default=DEFAULT_LOCAL_IP)
    ap.add_argument("--group-id", type=int, default=DEFAULT_GROUP_ID,
                    help="Charger group id to place the plug in (1 = public).")

    # Broker
    ap.add_argument("--broker-host", default=os.getenv("MQTT_BROKER_HOST", "localhost"),
                    help="On the VM host use the overlay IP (e.g. 100.87.241.70); "
                         "inside compose use 'mqtt'.")
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
                "Or skip registration with --run-only --plug-id N."
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
              f"--gateway-id {args.gateway_id} --broker-host <overlay-ip> "
              f"--broker-user {args.broker_user or 'amphive-gateway'} --broker-pass '<pw>'")
        return

    if plug_id is None:
        raise SystemExit("--run-only requires --plug-id.")
    if not args.broker_user:
        log.warning("No broker credentials given; the broker enforces auth and "
                    "will reject an anonymous client.")

    plug = FakePlug(
        broker_host=args.broker_host, broker_port=args.broker_port,
        username=args.broker_user, password=args.broker_pass,
        gateway_id=args.gateway_id, plug_id=plug_id, watts=args.watts,
        voltage=args.voltage, interval=args.interval,
        use_tls=args.tls, cafile=args.cafile,
    )
    signal.signal(signal.SIGINT, lambda *_: plug.shutdown())
    try:
        signal.signal(signal.SIGTERM, lambda *_: plug.shutdown())
    except (ValueError, AttributeError):
        pass  # SIGTERM not settable on some platforms/threads
    plug.run()


if __name__ == "__main__":
    main()
