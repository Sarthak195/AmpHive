"""
Direct routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import async_session_factory, get_db
from backend.database.models import (
    ChargerGroup, ChargingSession, Gateway, GatewayStatus, GroupMembership,
    LedgerTransaction, Plug, PlugStatus, SessionStatus, TelemetryReading,
    Tenant, TransactionType, User, UserRole,
)
from backend.schemas import (
    AuthResponse, CpoGatewayCreateRequest, CpoGroupCreateRequest,
    CpoGroupUpdateRequest, CpoPlugCreateRequest, CpoPlugUpdateRequest,
    CpoSetupRequest, CreateOrderRequest, CreateOrderResponse,
    DirectPlugRequest, GatewayRegisterRequest, GroupResponse,
    JoinGroupRequest, LoginRequest, PlugRegisterRequest, PlugResponse,
    RegisterRequest, SessionStartRequest, SessionStopRequest, UserResponse,
    VerifyPaymentRequest,
)
from backend.services import payments as payment_service
from backend.services.auth import (
    create_access_token, decode_access_token, get_current_user,
    hash_password, verify_password,
)
from backend.services.money import ZERO_MONEY, to_money
from backend.services.rbac import require_role
from backend.services.session_lifecycle import (
    check_and_speed_up_active_session, finalize_charging_session,
    gateway_is_live, set_plug_telemetry_interval,
)
from backend.services.telemetry import COINS_PER_KWH

logger = logging.getLogger("amphive.api")
router = APIRouter()

# [Direct Mode] env config (dotenv is loaded before routers are imported,
# but load again defensively — it is idempotent).
from dotenv import load_dotenv
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


