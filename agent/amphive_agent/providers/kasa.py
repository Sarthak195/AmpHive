"""Kasa/Tapo provider via python-kasa (LOCAL access).

Tapo and newer Kasa devices need the TP-Link account credentials for the local
KLAP handshake — the creds are used *locally*, the data never leaves the LAN
except as AmpHive telemetry. This is what gives us real Tapo P110 energy that
the unofficial cloud API can't (see docs/PLUG_CONNECTIVITY_OPTIONS.md).

Requires: python-kasa>=0.7
"""
from __future__ import annotations

import logging

from ..model import PlugState

log = logging.getLogger(__name__)


class KasaPlug:
    def __init__(self, dev):
        from kasa import Module
        self._dev = dev
        self.unique_id = f"kasa:{dev.mac}"
        self.model = dev.model
        self.alias = dev.alias or dev.model
        caps = {"switch"}
        if dev.modules.get(Module.Energy) is not None:
            caps |= {"power", "energy"}
        self.capabilities = caps

    async def get_state(self) -> PlugState:
        from kasa import Module
        dev = self._dev
        await dev.update()
        em = dev.modules.get(Module.Energy)
        if em is None:
            return PlugState(on=bool(dev.is_on))
        # Attribute names are stable in python-kasa 0.7+; guard anyway.
        watts = float(getattr(em, "current_consumption", None) or 0.0)
        kwh = getattr(em, "consumption_total", None)
        if kwh is None:
            kwh = getattr(em, "consumption_today", None)
        volts = float(getattr(em, "voltage", None) or 0.0)
        amps = float(getattr(em, "current", None) or 0.0)
        return PlugState(
            on=bool(dev.is_on), watts=watts,
            energy_kwh=float(kwh or 0.0), voltage=volts, current=amps,
        )

    async def set_power(self, on: bool) -> None:
        if on:
            await self._dev.turn_on()
        else:
            await self._dev.turn_off()


class KasaProvider:
    name = "kasa"

    def __init__(self, tp_user: str | None, tp_pass: str | None):
        self._user = tp_user
        self._pass = tp_pass

    async def discover(self) -> list:
        from kasa import Credentials, Discover
        creds = Credentials(self._user, self._pass) if self._user else None
        found = await Discover.discover(credentials=creds)
        plugs = []
        for ip, dev in found.items():
            try:
                await dev.update()
            except Exception:
                log.warning("kasa: update failed for %s", ip)
                continue
            plugs.append(KasaPlug(dev))
        return plugs

    async def close(self) -> None:
        pass
