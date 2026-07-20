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
import time

import paho.mqtt.client as mqtt

from .config import Config
from .model import PlugDevice, PlugProvider, PlugState
from .providers import build_providers
from .store import Store

log = logging.getLogger(__name__)

# Meter-reset / rounding tolerance (kWh). Telemetry ``kwh`` is published at 4
# decimals, so a drop smaller than this is noise, not a power-cycle reset.
_KWH_EPSILON = 1e-4

# Local watchdog defaults, mirroring the firmware / backend ON contract
# (send_plug_command defaults: max_kwh=30.0, max_duration=14400).
_DEFAULT_MAX_KWH = 30.0
_DEFAULT_MAX_DURATION_S = 14400


def monotonic_session_kwh(session: dict, energy_kwh: float) -> tuple[float, bool]:
    """Session-relative kWh that never regresses (mirrors the backend guard).

    ``energy_kwh`` is the plug's cumulative/lifetime reading. If it drops below
    the captured baseline (a power-cycle that reset the plug's meter), re-anchor
    the baseline to the new lower reading so subsequent deltas stay correct, but
    never emit a value below the last one reported for this session.

    Mutates ``session`` in place (``baseline_kwh`` / ``last_kwh``) and returns
    ``(session_kwh, changed)`` where ``changed`` is True if ``session`` needs to
    be persisted.
    """
    baseline = float(session.get("baseline_kwh", 0.0))
    last = float(session.get("last_kwh", 0.0))
    changed = False

    # Meter reset: cumulative reading fell below our baseline (power-cycle).
    if energy_kwh < baseline - _KWH_EPSILON:
        baseline = energy_kwh
        session["baseline_kwh"] = baseline
        changed = True

    raw = energy_kwh - baseline
    session_kwh = raw if raw > last else last  # never jump downward
    if session_kwh != last:
        session["last_kwh"] = session_kwh
        changed = True
    return session_kwh, changed


def limit_exceeded(session: dict, session_kwh: float, now_ts: float) -> str | None:
    """Local watchdog check, mirroring the firmware's per-poll test.

    Returns ``"ENERGY_LIMIT"`` / ``"DURATION_LIMIT"`` when the session has hit
    its cap, else ``None``. Pure function so it is trivially testable; the
    caller (the poll loop) does the actual cutoff.
    """
    max_kwh = session.get("max_kwh")
    if max_kwh is not None and session_kwh >= float(max_kwh):
        return "ENERGY_LIMIT"
    max_dur = session.get("max_duration_s")
    start = session.get("start_ts")
    if max_dur is not None and start is not None and now_ts - float(start) >= float(max_dur):
        return "DURATION_LIMIT"
    return None


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
                now = time.time()
                self.store.set_session(plug_id, {
                    "on": True,
                    "baseline_kwh": state.energy_kwh,
                    "session_id": str(cmd.get("session_id", "")),
                    # Local watchdog limits (mirrors the firmware): enforced by
                    # the poll loop even when the broker is unreachable, and
                    # persisted so a restart mid-session keeps them.
                    "max_kwh": float(cmd.get("max_kwh", _DEFAULT_MAX_KWH)),
                    "max_duration_s": int(cmd.get("max_duration_seconds",
                                                  _DEFAULT_MAX_DURATION_S)),
                    "start_ts": now,
                    "last_poll_ts": now,
                    "integrated_kwh": 0.0,  # meterless fallback (watts * dt)
                })
                log.info("plug %s ON (session=%s, limits: %.1f kWh / %s s)",
                         plug_id, cmd.get("session_id", ""),
                         float(cmd.get("max_kwh", _DEFAULT_MAX_KWH)),
                         cmd.get("max_duration_seconds", _DEFAULT_MAX_DURATION_S))
            elif action == "OFF":
                await dev.set_power(False)
                self.store.clear_session(plug_id)
                log.info("plug %s OFF", plug_id)
            elif action == "SET_LIMITS":
                # Re-cap a RUNNING session without re-baselining, exactly like
                # the firmware (docs/MQTT_CONTRACT.md): only max_kwh /
                # max_duration_s change; baseline/session_id/start stay put.
                session = self.store.get_session(plug_id)
                if not session or not session.get("on"):
                    log.info("SET_LIMITS for plug %s ignored: no active session", plug_id)
                    return
                if cmd.get("max_kwh") is not None:
                    session["max_kwh"] = float(cmd["max_kwh"])
                if cmd.get("max_duration_seconds") is not None:
                    session["max_duration_s"] = int(cmd["max_duration_seconds"])
                self.store.set_session(plug_id, session)
                log.info("plug %s SET_LIMITS -> %.1f kWh / %s s", plug_id,
                         session.get("max_kwh"), session.get("max_duration_s"))
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
                await self._watchdog_and_publish(plug_id, dev, state)
            await self._sleep_or_stop(self._poll_s)

    async def _watchdog_and_publish(self, plug_id: int, dev: PlugDevice, state: PlugState):
        """Local safety watchdog + telemetry, mirroring the firmware loop.

        Cuts the plug OFF when the session hits its energy/duration limit —
        ``set_power`` is LAN-local, so this works with the broker unreachable
        (the offline-tail gap). Like the firmware, the trip frame is published
        *pre-watchdog* (still ``occupied``, carrying the final kwh), then the
        session ends and a QoS-1 alarm is queued (paho delivers it on
        reconnect if the broker is down).
        """
        session = self.store.get_session(plug_id)
        reason = None
        if session and session.get("on"):
            now = time.time()
            session_kwh, _ = monotonic_session_kwh(session, state.energy_kwh)
            # Meterless fallback: a plug without a cumulative energy reading
            # (energy_kwh stuck at 0) still gets a limit by integrating
            # watts over the poll gap, like the fake-plug simulator.
            last = float(session.get("last_poll_ts") or now)
            if state.energy_kwh <= 0.0 and state.watts > 0.0 and now > last:
                session["integrated_kwh"] = (
                    float(session.get("integrated_kwh", 0.0))
                    + (state.watts / 1000.0) * ((now - last) / 3600.0)
                )
            session["last_poll_ts"] = now
            effective_kwh = max(session_kwh, float(session.get("integrated_kwh", 0.0)))
            reason = limit_exceeded(session, effective_kwh, now)
            self.store.set_session(plug_id, session)
            if reason:
                log.error("WATCHDOG plug %s: %s (%.4f kWh, %.0f s) — local OFF",
                          plug_id, reason, effective_kwh,
                          now - float(session.get("start_ts") or now))
                try:
                    await dev.set_power(False)
                except Exception:
                    log.exception("watchdog OFF failed for plug %s", plug_id)
        # Trip frame (if any) goes out pre-watchdog: occupied + final kwh.
        self._publish_telemetry(plug_id, state)
        if reason:
            self.store.clear_session(plug_id)
            self.mqtt.publish(
                f"{self.base}/alarms",
                json.dumps({"event": "LOCAL_LIMIT_CUTOFF", "reason": reason,
                            "plug_id": plug_id}),
                qos=1,
            )

    def _publish_telemetry(self, plug_id: int, state: PlugState):
        session = self.store.get_session(plug_id)
        if session and session.get("on"):
            session_kwh, changed = monotonic_session_kwh(session, state.energy_kwh)
            if changed:
                self.store.set_session(plug_id, session)
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
                "current": round(state.effective_current(), 2),
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


if __name__ == "__main__":
    # Self-check for the monotonic / re-anchor telemetry logic (no framework):
    #   python -m amphive_agent.core
    s = {"on": True, "baseline_kwh": 100.0, "session_id": "x"}

    # Normal accumulation: session_kwh == cumulative - baseline, monotonic up.
    v, changed = monotonic_session_kwh(s, 100.0); assert v == 0.0, v
    v, changed = monotonic_session_kwh(s, 103.0); assert v == 3.0 and changed, v
    v, changed = monotonic_session_kwh(s, 104.5); assert v == 4.5 and changed, v

    # Power-cycle meter reset: cumulative drops far below baseline. Baseline
    # re-anchors to the new low, but the reported value must NOT jump down.
    v, changed = monotonic_session_kwh(s, 2.0)
    assert v == 4.5, v                      # held at last, no downward jump
    assert s["baseline_kwh"] == 2.0, s      # baseline re-anchored to new low

    # After the reset, deltas resume from the new baseline and only surface
    # once they exceed the held value.
    v, _ = monotonic_session_kwh(s, 5.0); assert v == 4.5, v   # raw 3.0 < 4.5, still held
    v, _ = monotonic_session_kwh(s, 8.0); assert v == 6.0, v   # raw 6.0 > 4.5, resumes

    # Sub-epsilon dip is noise, not a reset: no re-anchor, value holds.
    b = s["baseline_kwh"]
    v, _ = monotonic_session_kwh(s, 8.0 - 5e-5)
    assert v == 6.0 and s["baseline_kwh"] == b, (v, s["baseline_kwh"])

    print("monotonic_session_kwh self-check: OK")

    # PlugState.effective_current: measured amps win; derive only when omitted.
    # Measured current is reported as-is even when it differs from power/voltage
    # (power factor < 1 -> measured != P/V).
    st = PlugState(on=True, watts=2200.0, voltage=230.0, current=10.5)
    assert st.effective_current() == 10.5, st.effective_current()
    # Device omits current (0.0): derive from measured voltage.
    st = PlugState(on=True, watts=2300.0, voltage=230.0, current=0.0)
    assert st.effective_current() == 10.0, st.effective_current()
    # No current AND no voltage: fall back to nominal 230 V.
    st = PlugState(on=True, watts=2300.0, voltage=0.0, current=0.0)
    assert st.effective_current() == 10.0, st.effective_current()
    # Idle plug draws nothing.
    assert PlugState(on=False).effective_current() == 0.0
    print("PlugState.effective_current self-check: OK")

    # limit_exceeded: the local watchdog predicate (mirrors the firmware).
    sess = {"max_kwh": 5.0, "max_duration_s": 3600, "start_ts": 1000.0}
    assert limit_exceeded(sess, 4.99, 1500.0) is None          # under both caps
    assert limit_exceeded(sess, 5.0, 1500.0) == "ENERGY_LIMIT" # kWh cap (>=)
    assert limit_exceeded(sess, 0.0, 4600.0) == "DURATION_LIMIT"  # time cap (>=)
    # Energy trips first when both are exceeded (matches firmware ordering).
    assert limit_exceeded(sess, 9.0, 9999.0) == "ENERGY_LIMIT"
    # SET_LIMITS mid-session: a raised cap un-trips, a lowered cap trips.
    sess["max_kwh"] = 10.0
    assert limit_exceeded(sess, 5.0, 1500.0) is None
    sess["max_kwh"] = 2.0
    assert limit_exceeded(sess, 5.0, 1500.0) == "ENERGY_LIMIT"
    # Legacy session dict without limit keys never trips.
    assert limit_exceeded({}, 999.0, 1e12) is None
    print("limit_exceeded self-check: OK")
