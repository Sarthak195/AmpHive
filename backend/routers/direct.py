"""
Direct routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend import state
from backend.database.models import (
    User,
)
from backend.schemas import (
    DirectPlugRequest,
)
from backend.services.auth import (
    get_current_user,
)
from backend.services.rbac import require_role

logger = logging.getLogger("amphive.api")
router = APIRouter()

# [Direct Mode] env config (dotenv is loaded before routers are imported,
# but load again defensively — it is idempotent).
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
DIRECT_MODE = os.getenv("DIRECT_MODE", "false").lower() == "true"
TAPO_PLUG_IP = os.getenv("TAPO_PLUG_IP", "")

# ===========================================================================
# [Direct Mode] Tapo P110 Direct Control Endpoints
# ===========================================================================
# These endpoints bypass the ESP32 gateway and MQTT broker, controlling the
# Tapo P110 smart plug directly via the `tapo` Python library through a
# WireGuard tunnel to the developer's home network.
#
# Architecture:
#   Cloud Backend (GCP VM) → WireGuard Tunnel → Dev PC → Home LAN → Tapo P110
#
# These are development/testing-only endpoints. In production, the normal
# ESP32/MQTT session flow is used instead.
#
# All endpoints require JWT authentication.
# ===========================================================================


def _get_plug_ip(req_ip: Optional[str] = None) -> str:
    """
    Resolve the target plug IP address.
    Priority: request body plug_ip > TAPO_PLUG_IP env var.
    Raises 400 if neither is set.
    """
    ip = req_ip or TAPO_PLUG_IP
    if not ip:
        raise HTTPException(
            status_code=400,
            detail="No plug IP specified. Set TAPO_PLUG_IP env var or pass plug_ip in the request body.",
        )
    return ip


@router.post("/api/direct/plug/on")
async def direct_plug_on(
    req: DirectPlugRequest = DirectPlugRequest(),
    user: User = Depends(require_role("admin")),
):
    """
    [Direct Mode] Turn the Tapo P110 plug ON.
    Bypasses ESP32/MQTT and sends the command directly to the plug via
    the WireGuard tunnel. Requires DIRECT_MODE=true in environment.
    """
    if not DIRECT_MODE or not state.tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    plug_ip = _get_plug_ip(req.plug_ip)
    success = await state.tapo_driver.turn_on(plug_ip)

    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to turn ON plug at {plug_ip}. Check WireGuard tunnel and plug connectivity.",
        )

    return {
        "status": "on",
        "plug_ip": plug_ip,
        "message": f"Plug at {plug_ip} turned ON via direct mode.",
        "mode": "direct",
    }


@router.post("/api/direct/plug/off")
async def direct_plug_off(
    req: DirectPlugRequest = DirectPlugRequest(),
    user: User = Depends(require_role("admin")),
):
    """
    [Direct Mode] Turn the Tapo P110 plug OFF.
    Bypasses ESP32/MQTT and sends the command directly to the plug via
    the WireGuard tunnel. Requires DIRECT_MODE=true in environment.
    """
    if not DIRECT_MODE or not state.tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    plug_ip = _get_plug_ip(req.plug_ip)
    success = await state.tapo_driver.turn_off(plug_ip)

    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to turn OFF plug at {plug_ip}. Check WireGuard tunnel and plug connectivity.",
        )

    return {
        "status": "off",
        "plug_ip": plug_ip,
        "message": f"Plug at {plug_ip} turned OFF via direct mode.",
        "mode": "direct",
    }


@router.get("/api/direct/plug/info")
async def direct_plug_info(
    plug_ip: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    """
    [Direct Mode] Get device information from the Tapo P110 plug.
    Returns power state, model, nickname, firmware version, etc.
    """
    if not DIRECT_MODE or not state.tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    target_ip = _get_plug_ip(plug_ip)
    info = await state.tapo_driver.get_device_info(target_ip)

    if info is None:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to get device info from {target_ip}. Check WireGuard tunnel and plug connectivity.",
        )

    return {
        "plug_ip": target_ip,
        "device_info": info,
        "mode": "direct",
    }


@router.get("/api/direct/plug/energy")
async def direct_plug_energy(
    plug_ip: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    """
    [Direct Mode] Get energy usage data from the Tapo P110 plug.
    Returns current power draw, today's energy consumption, monthly stats.
    """
    if not DIRECT_MODE or not state.tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    target_ip = _get_plug_ip(plug_ip)
    usage = await state.tapo_driver.get_energy_usage(target_ip)

    if usage is None:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to get energy data from {target_ip}. Check WireGuard tunnel and plug connectivity.",
        )

    return {
        "plug_ip": target_ip,
        "energy_usage": usage,
        "mode": "direct",
    }


@router.get("/api/direct/plug/health")
async def direct_plug_health(
    plug_ip: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    """
    [Direct Mode] Health check — verify the plug is reachable through
    the WireGuard tunnel and responding to commands.
    """
    if not DIRECT_MODE or not state.tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    target_ip = _get_plug_ip(plug_ip)
    health = await state.tapo_driver.health_check(target_ip)

    return {
        "plug_ip": target_ip,
        "health": health,
        "mode": "direct",
    }


