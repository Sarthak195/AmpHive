#!/usr/bin/env python3
"""Self-contained test for the agent's LOCAL kWh/duration watchdog.

Exercises AmpHiveAgent._watchdog_and_publish end-to-end with a stub device and
a stub MQTT client, including the critical offline case: the broker publish
fails/queues, but the plug is still cut OFF locally (set_power is LAN-local).

Run:  python agent/test_local_limits.py       (no broker, no network, no deps
beyond paho-mqtt being importable — it is stubbed out before core imports it)
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Stub paho before importing core so the test needs no broker (and works even
# if paho-mqtt is not installed).
paho_pkg = types.ModuleType("paho")
paho_mqtt = types.ModuleType("paho.mqtt")
paho_client = types.ModuleType("paho.mqtt.client")


class _StubMqtt:
    class CallbackAPIVersion:
        VERSION2 = 2

    class Client:
        def __init__(self, *a, **k):
            self.published: list[tuple[str, str, int]] = []
            self.connected = True  # flip False to simulate broker outage
            self.on_connect = None
            self.on_message = None

        def username_pw_set(self, *a): ...
        def tls_set(self, *a, **k): ...
        def will_set(self, *a, **k): ...
        def reconnect_delay_set(self, **k): ...

        def publish(self, topic, payload, qos=0, retain=False):
            # Record everything; QoS1 while "offline" models paho's client-side
            # queue (delivered on reconnect), QoS0 would be dropped.
            self.published.append((topic, payload, qos))


paho_client.Client = _StubMqtt.Client
paho_client.CallbackAPIVersion = _StubMqtt.CallbackAPIVersion
paho_pkg.mqtt = paho_mqtt
paho_mqtt.client = paho_client
sys.modules.setdefault("paho", paho_pkg)
sys.modules["paho.mqtt"] = paho_mqtt
sys.modules["paho.mqtt.client"] = paho_client

from amphive_agent.config import Config  # noqa: E402
from amphive_agent.core import AmpHiveAgent, limit_exceeded  # noqa: E402
from amphive_agent.model import PlugState  # noqa: E402


class StubDevice:
    """A metered plug whose cumulative energy we script per poll."""

    unique_id = "sim:TEST"
    model = "stub"
    alias = "stub"
    capabilities = {"switch", "power", "energy"}

    def __init__(self):
        self.on = False
        self.energy_kwh = 100.0
        self.watts = 10000.0

    async def get_state(self) -> PlugState:
        return PlugState(on=self.on, watts=self.watts if self.on else 0.0,
                         energy_kwh=self.energy_kwh, voltage=230.0, current=45.0)

    async def set_power(self, on: bool) -> None:
        self.on = on


def make_agent(tmpdir: str) -> AmpHiveAgent:
    cfg = Config(
        gateway_id="test-gw", broker_host="localhost", broker_port=1883,
        mqtt_user="", mqtt_pass="", use_tls=False, ca_file=None,
        providers=["sim"], poll_s=5.0, state_path=Path(tmpdir) / "state.json",
        tplink_user=None, tplink_pass=None, shelly_hosts=[], sim_count=0,
    )
    return AmpHiveAgent(cfg)


async def run_tests():
    tmp = tempfile.mkdtemp()
    agent = make_agent(tmp)
    dev = StubDevice()
    agent.devices[7] = dev

    # --- ON stores the limits from the command payload (firmware contract) ---
    await agent._handle_command(7, {
        "action": "ON", "session_id": "42", "max_kwh": 2.0,
        "max_duration_seconds": 3600, "max_current_a": 16.0,
    })
    s = agent.store.get_session(7)
    assert dev.on and s["on"] and s["max_kwh"] == 2.0 and s["max_duration_s"] == 3600, s
    assert s["baseline_kwh"] == 100.0, s

    # --- Broker goes DOWN; energy climbs past the cap; local OFF anyway ---
    agent.mqtt.connected = False
    dev.energy_kwh = 101.0  # 1.0 kWh into the session: under cap
    await agent._watchdog_and_publish(7, dev, await dev.get_state())
    assert dev.on, "must stay on under the cap"

    dev.energy_kwh = 102.5  # 2.5 kWh >= 2.0 cap: trip
    await agent._watchdog_and_publish(7, dev, await dev.get_state())
    assert not dev.on, "watchdog must cut the plug OFF with the broker down"
    assert agent.store.get_session(7) is None, "session cleared after trip"

    # Trip frame was published pre-watchdog: occupied + final session kwh.
    tele = [json.loads(p) for t, p, q in agent.mqtt.published
            if t.endswith("/telemetry")]
    assert tele[-1]["status"] == "occupied" and tele[-1]["kwh"] == 2.5, tele[-1]
    # QoS-1 alarm queued for delivery on reconnect.
    alarms = [json.loads(p) for t, p, q in agent.mqtt.published
              if t.endswith("/alarms") and q == 1]
    assert alarms[-1] == {"event": "LOCAL_LIMIT_CUTOFF",
                          "reason": "ENERGY_LIMIT", "plug_id": 7}, alarms

    # --- Duration limit, and SET_LIMITS re-cap without re-baselining ---
    await agent._handle_command(7, {"action": "ON", "session_id": "43",
                                    "max_kwh": 30.0, "max_duration_seconds": 7200})
    s = agent.store.get_session(7)
    baseline = s["baseline_kwh"]
    await agent._handle_command(7, {"action": "SET_LIMITS", "max_kwh": 25.0,
                                    "max_duration_seconds": 60})
    s = agent.store.get_session(7)
    assert s["max_kwh"] == 25.0 and s["max_duration_s"] == 60, s
    assert s["baseline_kwh"] == baseline, "SET_LIMITS must not re-baseline"
    s["start_ts"] = time.time() - 61  # fake 61 s elapsed
    agent.store.set_session(7, s)
    dev.energy_kwh += 0.1
    await agent._watchdog_and_publish(7, dev, await dev.get_state())
    assert not dev.on, "duration watchdog must cut off"
    alarms = [json.loads(p) for t, p, q in agent.mqtt.published
              if t.endswith("/alarms")]
    assert alarms[-1]["reason"] == "DURATION_LIMIT", alarms[-1]

    # SET_LIMITS with no active session is a logged no-op (firmware parity).
    await agent._handle_command(7, {"action": "SET_LIMITS", "max_kwh": 1.0})
    assert agent.store.get_session(7) is None

    # --- Meterless plug: energy integrated locally from watts * dt ---
    class MeterlessDevice(StubDevice):
        async def get_state(self):
            return PlugState(on=self.on, watts=10000.0 if self.on else 0.0,
                             energy_kwh=0.0, voltage=230.0, current=45.0)

    dev2 = MeterlessDevice()
    agent.devices[8] = dev2
    await agent._handle_command(8, {"action": "ON", "session_id": "44",
                                    "max_kwh": 1.0, "max_duration_seconds": 7200})
    s = agent.store.get_session(8)
    s["last_poll_ts"] = time.time() - 3600  # pretend one hour at 10 kW elapsed
    agent.store.set_session(8, s)
    await agent._watchdog_and_publish(8, dev2, await dev2.get_state())
    assert not dev2.on, "meterless integration (10 kWh >> 1 kWh cap) must trip"

    # Pure-function sanity (also covered by `python -m amphive_agent.core`).
    assert limit_exceeded({"max_kwh": 1.0}, 0.5, 0) is None
    assert limit_exceeded({"max_kwh": 1.0}, 1.0, 0) == "ENERGY_LIMIT"

    print("local kWh/duration watchdog tests: OK")


if __name__ == "__main__":
    asyncio.run(run_tests())
