#!/usr/bin/env python3
"""Host-runnable test: SET_INTERVAL wakes the agent's poll loop at once.

Mirrors firmware 2.9.0: the poll loop must not finish its current (long) sleep
before the new cadence starts, a repeated interval must not wake it, stop must
still end the sleep, and the meterless integrator must not drop the first gap
after the interval shrinks (that gap is up to the OLD interval).

Run:  python agent/test_poll_wake.py   (no broker, no network; reuses the paho
stub and fixtures from test_local_limits.py)
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_local_limits import StubDevice, make_agent  # noqa: E402  (stubs paho)


class CountingDevice(StubDevice):
    def __init__(self):
        super().__init__()
        self.polls = 0

    async def get_state(self):
        self.polls += 1
        return await super().get_state()


async def wait_for(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def run_tests():
    agent = make_agent(tempfile.mkdtemp())
    dev = CountingDevice()
    agent.devices[7] = dev
    agent._poll_s = agent._slept_s = 30.0  # a long sleep we must not wait out

    task = asyncio.ensure_future(agent._poll_loop())
    assert await wait_for(lambda: dev.polls == 1, 1.0), dev.polls

    # A changed interval wakes the loop now, not after the 30 s sleep.
    await agent._handle_command(7, {"action": "SET_INTERVAL", "interval_ms": 60000})
    assert await wait_for(lambda: dev.polls == 2, 1.0), "SET_INTERVAL did not wake the loop"
    assert agent._poll_s == 60.0

    # The same interval again is not a wake.
    await agent._handle_command(7, {"action": "SET_INTERVAL", "interval_ms": 60000})
    await asyncio.sleep(0.3)
    assert dev.polls == 2, "an unchanged interval must not wake the loop"

    # Stop still ends the sleep promptly.
    agent._stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    # Meterless integrator: interval 10 s -> 1 s, the woken poll comes 8 s after
    # the last one. Clamped against the new 1 s interval (3 s) it was dropped.
    agent = make_agent(tempfile.mkdtemp())
    dev = StubDevice()
    dev.energy_kwh = 0.0  # no cumulative meter: the integrator is the energy
    agent.devices[8] = dev
    await agent._handle_command(8, {"action": "ON", "session_id": "50",
                                    "max_kwh": 30.0, "max_duration_seconds": 7200})
    agent._slept_s = 10.0  # the sleep that SET_INTERVAL cut short
    await agent._handle_command(8, {"action": "SET_INTERVAL", "interval_ms": 1000})
    s = agent.store.get_session(8)
    s["last_poll_ts"] = time.time() - 8.0
    agent.store.set_session(8, s)
    await agent._watchdog_and_publish(8, dev, await dev.get_state())
    kwh = agent.store.get_session(8)["integrated_kwh"]
    assert abs(kwh - 10.0 * 8.0 / 3600.0) < 1e-3, kwh  # 10 kW for 8 s

    # A gap beyond 3x the larger interval is still "resume now" (no phantom energy).
    s = agent.store.get_session(8)
    s["last_poll_ts"] = time.time() - 40.0
    agent.store.set_session(8, s)
    await agent._watchdog_and_publish(8, dev, await dev.get_state())
    assert agent.store.get_session(8)["integrated_kwh"] == kwh

    print("poll-loop wake tests: OK")


if __name__ == "__main__":
    asyncio.run(run_tests())
