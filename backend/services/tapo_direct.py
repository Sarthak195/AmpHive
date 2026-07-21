"""
TapoDirectDriver — Direct Tapo P110 Smart Plug Control Service
================================================================
Temporary bypass service that controls a TP-Link Tapo P110 smart plug
directly from the cloud backend using the `tapo` Python library, without
requiring an ESP32 gateway or MQTT broker.

This service is used during development/testing when the ESP32 board is
not yet available. The backend reaches the plug through a WireGuard
tunnel from the GCP VM to the developer's home PC, which port-forwards
to the plug's local IP on the home LAN.

Architecture (Development / ESP32 Bypass):
    Cloud Backend (GCP VM)
        → WireGuard Tunnel (UDP 51820)
            → Developer's PC (WireGuard client + port proxy)
                → Home LAN
                    → Tapo P110 Smart Plug (HTTP port 80)

Production Architecture (ESP32 Gateway):
    Cloud Backend (GCP VM)
        → WireGuard/Tailscale Tunnel
            → ESP32 Edge Gateway
                → VLAN 20 Local Network
                    → Tapo P110 Smart Plug

Prerequisites:
    - The `tapo` Python package must be installed (pip install tapo)
    - The Tapo plug must have "Third-Party Compatibility" enabled in the
      Tapo mobile app (Device Settings → Third-Party Compatibility → ON)
    - The backend must be able to reach the plug's IP on port 80, either
      directly (same LAN) or via the WireGuard tunnel + port forwarding

Environment Variables:
    TAPO_USERNAME:  TP-Link/Tapo account email address
    TAPO_PASSWORD:  TP-Link/Tapo account password
    TAPO_PLUG_IP:   IP address of the plug (WireGuard peer IP if tunneled)
    DIRECT_MODE:    Set to 'true' to enable direct control endpoints
"""

import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

from tapo import ApiClient

logger = logging.getLogger("amphive.tapo_direct")

TAPO_RELAY_URL = os.getenv("TAPO_RELAY_URL")


class TapoDirectDriver:
    """
    Manages direct control of a Tapo P110 smart plug via the `tapo` library,
    or via a local HTTP relay server if TAPO_RELAY_URL is set.
    """

    def __init__(self, tapo_email: str, tapo_password: str):
        self.tapo_email = tapo_email
        self.tapo_password = tapo_password
        self._client: Optional[ApiClient] = None
        if TAPO_RELAY_URL:
            logger.info(f"TapoDirectDriver initialized with RELAY_URL: {TAPO_RELAY_URL}")
        else:
            logger.info("TapoDirectDriver initialized (credentials set, awaiting first connection)")

    async def _get_client(self) -> ApiClient:
        if self._client is None:
            self._client = ApiClient(self.tapo_email, self.tapo_password)
            logger.info("Tapo ApiClient created successfully")
        return self._client

    def _call_relay(self, endpoint: str) -> Dict[str, Any]:
        url = f"{TAPO_RELAY_URL.rstrip('/')}/{endpoint.lstrip('/')}"
        logger.info(f"Calling relay: {url}")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())

    async def turn_on(self, plug_ip: str) -> bool:
        if TAPO_RELAY_URL:
            try:
                self._call_relay("/on")
                logger.info("✅ Plug turned ON successfully via relay")
                return True
            except Exception as e:
                logger.error(f"❌ Failed to turn ON plug via relay: {e}")
                return False

        try:
            client = await self._get_client()
            device = await client.p110(plug_ip)
            await device.on()
            logger.info(f"✅ Plug at {plug_ip} turned ON successfully")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to turn ON plug at {plug_ip}: {e}")
            return False

    async def turn_off(self, plug_ip: str) -> bool:
        if TAPO_RELAY_URL:
            try:
                self._call_relay("/off")
                logger.info("✅ Plug turned OFF successfully via relay")
                return True
            except Exception as e:
                logger.error(f"❌ Failed to turn OFF plug via relay: {e}")
                return False

        try:
            client = await self._get_client()
            device = await client.p110(plug_ip)
            await device.off()
            logger.info(f"✅ Plug at {plug_ip} turned OFF successfully")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to turn OFF plug at {plug_ip}: {e}")
            return False

    async def get_device_info(self, plug_ip: str) -> Optional[Dict[str, Any]]:
        if TAPO_RELAY_URL:
            try:
                info_dict = self._call_relay("/info")
                logger.info(f"📋 Retrieved device info via relay: on={info_dict.get('device_on', 'unknown')}")
                return info_dict
            except Exception as e:
                logger.error(f"❌ Failed to get device info via relay: {e}")
                return None

        try:
            client = await self._get_client()
            device = await client.p110(plug_ip)
            info = await device.get_device_info()
            info_dict = info.to_dict()
            logger.info(f"📋 Retrieved device info from {plug_ip}: on={info_dict.get('device_on', 'unknown')}")
            return info_dict
        except Exception as e:
            logger.error(f"❌ Failed to get device info from {plug_ip}: {e}")
            return None

    async def get_energy_usage(self, plug_ip: str) -> Optional[Dict[str, Any]]:
        if TAPO_RELAY_URL:
            return {"current_power": 0, "today_energy": 0}

        try:
            client = await self._get_client()
            device = await client.p110(plug_ip)
            usage = await device.get_energy_usage()
            usage_dict = usage.to_dict()
            logger.info(
                f"⚡ Energy usage from {plug_ip}: "
                f"current_power={usage_dict.get('current_power', 0)}mW, "
                f"today_energy={usage_dict.get('today_energy', 0)}Wh"
            )
            return usage_dict
        except Exception as e:
            logger.error(f"❌ Failed to get energy usage from {plug_ip}: {e}")
            return None

    async def health_check(self, plug_ip: str) -> Dict[str, Any]:
        if TAPO_RELAY_URL:
            try:
                self._call_relay("/health")
                info = await self.get_device_info(plug_ip)
                if info:
                    return {
                        "reachable": True,
                        "device_on": info.get("device_on", False),
                        "model": info.get("model", "unknown"),
                        "nickname": info.get("nickname", "unknown"),
                        "ip": "relay",
                    }
                return {"reachable": False, "ip": "relay", "error": "No device info returned"}
            except Exception as e:
                return {"reachable": False, "ip": "relay", "error": str(e)}

        try:
            info = await self.get_device_info(plug_ip)
            if info:
                return {
                    "reachable": True,
                    "device_on": info.get("device_on", False),
                    "model": info.get("model", "unknown"),
                    "nickname": info.get("nickname", "unknown"),
                    "ip": plug_ip,
                }
            return {"reachable": False, "ip": plug_ip, "error": "No device info returned"}
        except Exception as e:
            return {"reachable": False, "ip": plug_ip, "error": str(e)}
