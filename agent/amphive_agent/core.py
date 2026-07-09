"""AmpHiveAgent core: MQTT + discovery/poll/command loops.

Registers as a *software gateway* and speaks the AmpHive MQTT contract
(docs/MQTT_CONTRACT.md). Telemetry ``kwh`` is **session-relative** (baseline
captured at ON), matching the firmware so the backend bills correctly.

plug_id is backend-authoritative (it equals the DB ``plugs.id``). The agent
announces discovered devices by ``unique_id`` on ``.../discovery``, the backend
upserts a plug and publishes a retained ``.../assign`` map, and the agent adopts
the assigned ids before it starts publishing telemetry for a device.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import ssl

import paho.mqtt.client as mqtt

from .config import Config
from .model import PlugDevice, PlugProvider, PlugState
from .providers import build_providers
from .store import Store

log = logging.getLogger(__name__)


class AmpHiveAgent:
    def __init__(self, config: Config):
        self.cfg = config
        self.base = f"amphive/gateways/{config.gateway_id}"
        self.store = Store(config.state_path)
        self.providers: list[PlugProvider] = build_providers(config)
        self.devices: dict[int, PlugDevice] = {}   # assigned plug_id -> device
        self.pending: dict[str, PlugDevice] = {}    # unique_id -> device awaiting assignment
        self._poll_s = config.poll_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()

        self.mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"amphive-agent-{config.gateway_id}",
        )
        if config.mqtt_user:
            self.mqtt.username_pw_set(config.mqtt_user, config.mqtt_pass)
        if config.use_tls:
            # Validate the broker cert — against the AmpHive CA when configured
            # (self-signed broker, the normal case), else the system store.
            self.mqtt.tls_set(
                ca_certs=str(config.ca_file) if config.ca_file else None,
                cert_reqs=ssl.CERT_REQUIRED,
            )
        self.mqtt.will_set(
            f"{self.base}/status", json.dumps({"status": "offline"}), qos=1, retain=True
        )
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=60)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

    # ---- MQTT callbacks (run on paho's network thread) ----
    def _on_connect(self, client, _userdata, _flags, reason, _props=None):
        log.info("mqtt connected (%s)", reason)
        client.publish(
            f"{self.base}/status",
            json.dumps({"status": "online", "fw": "agent-0.1"}),
            qos=1, retain=True,
        )
        client.subscribe(f"{self.base}/plugs/+/commands", qos=1)
        client.subscribe(f"{self.base}/assign", qos=1)  # retained plug_id assignments

    def _on_message(self, _client, _userdata, msg):
        if self._loop is None:
            return
        if msg.topic == f"{self.base}/assign":
            try:
                assignments = json.loads(msg.payload)
            except Exception:
                log.warning("bad assign payload")
                return
            asyncio.run_coroutine_threadsafe(self._handle_assignment(assignments), self._loop)
            return
        try:
            plug_id = int(msg.topic.split("/")[4])
            cmd = json.loads(msg.payload)
        except Exception:
            log.warning("ignoring malformed command on %s", msg.topic)
            return
        asyncio.run_coroutine_threadsafe(self._handle_command(plug_id, cmd), self._loop)

    # ---- assignment adoption (async) ----
    async def _handle_assignment(self, assignments: dict):
        """Adopt backend-assigned plug_ids; move pending devices to active."""
        for unique_id, plug_id in assignments.items():
            try:
                plug_id = int(plug_id)
            except (ValueError, TypeError):
                continue
            self.store.set_assignment(unique_id, plug_id)
            dev = self.pending.pop(unique_id, None)
            if dev is not None and plug_id not in self.devices:
                self.devices[plug_id] = dev
                log.info("adopted %s -> plug_id %s", unique_id, plug_id)

    # ---- command handling (async) ----
    async def _handle_command(self, plug_id: int, cmd: dict):
        action = str(cmd.get("action", "")).upper()

        if action == "SET_INTERVAL":
            ms = int(cmd.get("interval_ms", self._poll_s * 1000))
            self._poll_s = min(60.0, max(0.5, ms / 1000.0))
            log.info("poll interval -> %.1fs", self._poll_s)
            return

        dev = self.devices.get(plug_id)
        if dev is None:
            log.warning("command for unknown plug_id %s", plug_id)
            return

        try:
            if action == "ON":
                await dev.set_power(True)
                state = await dev.get_state()
                self.store.set_session(plug_id, {
                    "on": True,
                    "baseline_kwh": state.energy_kwh,
                    "session_id": str(cmd.get("session_id", "")),
                })
                log.info("plug %s ON (session=%s)", plug_id, cmd.get("session_id", ""))
            elif action == "OFF":
                await dev.set_power(False)
                self.store.clear_session(plug_id)
                log.info("plug %s OFF", plug_id)
            elif action == "OTA":
                # The agent self-updates via its package channel; OTA is n/a.
                self.mqtt.publish(
                    f"{self.base}/alarms",
                    json.dumps({"event": "OTA_REFUSED_NOT_APPLICABLE"}), qos=1,
                )
            else:
                log.warning("unknown action %r for plug %s", action, plug_id)
        except Exception:
            log.exception("command %s failed for plug %s", action, plug_id)

    # ---- loops ----
    async def _discover_loop(self):
        while not self._stop.is_set():
            for prov in self.providers:
                try:
                    found = await prov.discover()
                except Exception:
                    log.exception("discover failed for provider %s", prov.name)
                    continue
                for dev in found:
                    uid = dev.unique_id
                    plug_id = self.store.get_assignment(uid)
                    if plug_id is not None:
                        # Already assigned (persisted) — adopt immediately.
                        if plug_id not in self.devices:
                            self.devices[plug_id] = dev
                            log.info("adopted %s -> plug_id %s (persisted)", uid, plug_id)
                        self.pending.pop(uid, None)
                    else:
                        # Awaiting assignment — (re)announce for the backend.
                        self.pending[uid] = dev
                        self._announce(prov.name, dev)
            await self._sleep_or_stop(60)

    def _announce(self, provider: str, dev: PlugDevice):
        """Announce a discovered device so the backend can assign a plug_id."""
        self.mqtt.publish(
            f"{self.base}/discovery",
            json.dumps({
                "unique_id": dev.unique_id,
                "provider": provider,
                "model": dev.model,
                "alias": dev.alias,
                "capabilities": sorted(dev.capabilities),
            }),
            qos=1,
        )

    async def _poll_loop(self):
        while not self._stop.is_set():
            for plug_id, dev in list(self.devices.items()):
                try:
                    state = await dev.get_state()
                except Exception:
                    log.warning("poll failed for plug %s", plug_id)
                    continue
                self._publish_telemetry(plug_id, state)
            await self._sleep_or_stop(self._poll_s)

    def _publish_telemetry(self, plug_id: int, state: PlugState):
        session = self.store.get_session(plug_id)
        if session and session.get("on"):
            session_kwh = max(0.0, state.energy_kwh - float(session.get("baseline_kwh", 0.0)))
            status = "occupied"
            session_id = str(session.get("session_id", ""))
        else:
            session_kwh = 0.0
            status = "available"
            session_id = ""

        self.mqtt.publish(
            f"{self.base}/telemetry",
            json.dumps({
                "plug_id": plug_id,
                "watts": round(state.watts, 1),
                "kwh": round(session_kwh, 4),
                "voltage": round(state.voltage, 1),
                "current": round(state.current, 2),
                "status": status,
                "session_id": session_id,
            }),
            qos=0,
        )

    async def _sleep_or_stop(self, seconds: float):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ---- lifecycle ----
    async def run(self):
        self._loop = asyncio.get_running_loop()
        if not self.providers:
            raise SystemExit("no providers enabled (set AMPHIVE_PROVIDERS)")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows: rely on KeyboardInterrupt

        self.mqtt.connect_async(self.cfg.broker_host, self.cfg.broker_port, keepalive=60)
        self.mqtt.loop_start()
        log.info("agent running as gateway '%s' -> %s:%d (%d provider(s))",
                 self.cfg.gateway_id, self.cfg.broker_host, self.cfg.broker_port,
                 len(self.providers))
        try:
            await asyncio.gather(self._discover_loop(), self._poll_loop())
        finally:
            await self.shutdown()

    async def shutdown(self):
        self._stop.set()
        for prov in self.providers:
            try:
                await prov.close()
            except Exception:
                pass
        try:
            self.mqtt.publish(
                f"{self.base}/status", json.dumps({"status": "offline"}), qos=1, retain=True
            )
            self.mqtt.loop_stop()
            self.mqtt.disconnect()
        except Exception:
            pass
