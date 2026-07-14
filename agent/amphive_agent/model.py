"""Vendor-agnostic data model + provider protocols.

The core knows nothing about vendors — only these shapes. Each ecosystem
(Kasa/Tapo, Shelly, Tuya, ...) is a plugin that maps its devices into a
``PlugDevice`` and exposes them via a ``PlugProvider``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Nominal mains voltage, used ONLY to derive current as a last-resort fallback
# for a device that omits a measured current reading (see
# PlugState.effective_current). Real measured voltage always wins when present.
_NOMINAL_VOLTAGE = 230.0


@dataclass
class PlugState:
    """A normalized snapshot of one plug at a moment in time."""

    on: bool
    watts: float = 0.0
    energy_kwh: float = 0.0  # cumulative/lifetime kWh; 0.0 if unsupported
    voltage: float = 0.0     # measured volts; 0.0 if unsupported
    current: float = 0.0     # measured amps; 0.0 if unsupported

    def effective_current(self) -> float:
        """The current to report in telemetry (amps).

        Prefer the device's MEASURED current — a Tapo P110 / Shelly exposes
        real amps (~2 dp), and because active power factor is < 1 that measured
        current is NOT power/voltage (apparent power != active power). Only when
        the device omits a current reading (returns 0) do we fall back to a
        value DERIVED from power / voltage, which assumes power factor 1 and so
        slightly understates the true current — hence measured is preferred.
        """
        if self.current > 0.0:
            return self.current
        volts = self.voltage if self.voltage > 0.0 else _NOMINAL_VOLTAGE
        return self.watts / volts if volts else 0.0


@runtime_checkable
class PlugDevice(Protocol):
    """One controllable plug. Providers wrap their SDK objects in this shape.

    ``unique_id`` MUST be stable across reboots and IP changes (use the MAC),
    so a plug keeps its assigned ``plug_id``. Never key off the IP address.
    """

    unique_id: str            # e.g. "kasa:AA:BB:CC:DD:EE:FF"
    model: str
    alias: str
    capabilities: set[str]    # subset of {"switch", "power", "energy"}

    async def get_state(self) -> PlugState: ...
    async def set_power(self, on: bool) -> None: ...


@runtime_checkable
class PlugProvider(Protocol):
    """One ecosystem integration. Discovers devices on the LAN.

    Providers are fail-isolated by the core: one raising must not stop others.
    """

    name: str                 # "kasa" | "shelly" | "sim" | ...

    async def discover(self) -> list[PlugDevice]: ...
    async def close(self) -> None: ...
