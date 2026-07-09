"""Simulated provider — fake plugs so the agent runs end-to-end with NO hardware.

Great for verifying the whole pipeline (discovery -> telemetry -> ON/OFF ->
session-relative kWh) against a real broker. Each plug integrates energy over
real elapsed time while it's on. Enable with AMPHIVE_PROVIDERS=sim.
"""
from __future__ import annotations

import time

from ..model import PlugState


class SimPlug:
    def __init__(self, index: int, base_watts: float = 1500.0, voltage: float = 230.0):
        self.unique_id = f"sim:{index}"
        self.model = "SIM-PLUG"
        self.alias = f"Simulated Plug {index}"
        self.capabilities = {"switch", "power", "energy"}
        self._on = False
        self._base_watts = base_watts
        self._voltage = voltage
        self._energy_wh = 0.0
        self._last = time.monotonic()

    def _integrate(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if self._on:
            self._energy_wh += self._base_watts * dt / 3600.0

    async def get_state(self) -> PlugState:
        self._integrate()
        watts = self._base_watts if self._on else 0.0
        return PlugState(
            on=self._on, watts=watts,
            energy_kwh=self._energy_wh / 1000.0,
            voltage=self._voltage,
            current=(watts / self._voltage if self._voltage else 0.0),
        )

    async def set_power(self, on: bool) -> None:
        self._integrate()
        self._on = on


class SimProvider:
    name = "sim"

    def __init__(self, count: int):
        self._plugs = [SimPlug(i + 1) for i in range(max(0, count))]

    async def discover(self) -> list:
        return list(self._plugs)

    async def close(self) -> None:
        pass
