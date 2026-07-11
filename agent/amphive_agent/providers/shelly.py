"""Shelly Gen2+ provider via the local HTTP RPC API (no cloud, no extra SDK).

A genuinely different ecosystem from Kasa — proves the multi-brand plugin model.
Devices are given explicitly via AMPHIVE_SHELLY_HOSTS (comma-separated hosts/IPs);
mDNS auto-discovery could be added with zeroconf. Uses the documented Gen2 RPC:

    GET /rpc/Shelly.GetDeviceInfo
    GET /rpc/Switch.GetStatus?id=0
    GET /rpc/Switch.Set?id=0&on=true|false

Requires: aiohttp>=3.9
"""
from __future__ import annotations

import logging

import aiohttp

from ..model import PlugState

log = logging.getLogger(__name__)


class ShellyPlug:
    def __init__(self, session: aiohttp.ClientSession, host: str, info: dict):
        self._s = session
        self._base = f"http://{host}/rpc"
        self.unique_id = f"shelly:{info.get('mac', host)}"
        self.model = info.get("model") or info.get("app") or "Shelly"
        self.alias = info.get("name") or info.get("id") or self.model
        self.capabilities = {"switch", "power", "energy"}

    async def get_state(self) -> PlugState:
        async with self._s.get(f"{self._base}/Switch.GetStatus", params={"id": 0}) as r:
            d = await r.json()
        aenergy = d.get("aenergy") or {}
        return PlugState(
            on=bool(d.get("output", False)),
            watts=float(d.get("apower", 0.0) or 0.0),
            energy_kwh=float(aenergy.get("total", 0.0) or 0.0) / 1000.0,  # Wh -> kWh
            voltage=float(d.get("voltage", 0.0) or 0.0),
            current=float(d.get("current", 0.0) or 0.0),
        )

    async def set_power(self, on: bool) -> None:
        params = {"id": 0, "on": "true" if on else "false"}
        async with self._s.get(f"{self._base}/Switch.Set", params=params) as r:
            await r.read()


class ShellyProvider:
    name = "shelly"

    def __init__(self, hosts: list[str]):
        self._hosts = hosts
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        return self._session

    async def discover(self) -> list:
        session = await self._sess()
        plugs = []
        for host in self._hosts:
            try:
                async with session.get(f"http://{host}/rpc/Shelly.GetDeviceInfo") as r:
                    info = await r.json()
                plugs.append(ShellyPlug(session, host, info))
            except Exception:
                log.warning("shelly: %s unreachable", host)
        return plugs

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
