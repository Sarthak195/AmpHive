"""
Plugs routes — moved verbatim from main.py (2026-07-07, TD#7 split).
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
from backend.services.pricing import resolve_rate_for_plug
from backend.services.rbac import require_role
from backend.services.session_lifecycle import (
    check_and_speed_up_active_session, finalize_charging_session,
    gateway_is_live, set_plug_telemetry_interval,
)
from backend.services.telemetry import COINS_PER_KWH

logger = logging.getLogger("amphive.api")
router = APIRouter()

# ===========================================================================
# Plug Endpoints
# ===========================================================================

@router.get("/api/plugs/available", response_model=List[PlugResponse])
async def get_available_plugs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all plugs the current user can access:
    - Plugs in public groups
    - Plugs in private groups the user has joined
    - Ungrouped plugs (group_id = NULL, treated as public/legacy)
    """
    # One round-trip: accessibility filter via subqueries, group name and
    # gateway coords (the fallback location) via joins. Previously this was
    # 3 + N queries — one ChargerGroup.name lookup per plug.
    joined_group_ids = (
        select(GroupMembership.group_id)
        .where(GroupMembership.user_id == user.id)
        .scalar_subquery()
    )
    public_group_ids = (
        select(ChargerGroup.id)
        .where(ChargerGroup.is_public == True)
        .scalar_subquery()
    )

    rows = await db.execute(
        select(Plug, ChargerGroup.name, Gateway)
        .join(Gateway, Gateway.id == Plug.gateway_id)
        .outerjoin(ChargerGroup, ChargerGroup.id == Plug.group_id)
        .where(
            or_(
                Plug.group_id.is_(None),  # ungrouped/legacy: public to all
                Plug.group_id.in_(joined_group_ids),
                Plug.group_id.in_(public_group_ids),
            )
        )
    )

    now = datetime.now(timezone.utc)
    responses = []
    for plug, group_name, gateway in rows.all():
        # Resolved effective rate (plug tariff -> group tariff -> tenant
        # default -> env fallback; services/pricing.py). One lookup per plug
        # — tolerable N+1 for typical per-site plug counts; batch this if the
        # cross-tenant available-plugs list ever grows large enough to matter.
        price_per_kwh = await resolve_rate_for_plug(db, plug)
        responses.append(
            PlugResponse(
                id=plug.id,
                name=plug.name,
                status=plug.status.value,
                current_power_w=plug.current_power_w,
                plug_model=plug.plug_model,
                group_name=group_name,
                latitude=plug.latitude if plug.latitude is not None else gateway.latitude,
                longitude=plug.longitude if plug.longitude is not None else gateway.longitude,
                gateway_online=gateway_is_live(gateway, now),
                price_per_kwh=float(price_per_kwh),
            )
        )
    return responses


@router.get("/api/plugs/{plug_id}", response_model=PlugResponse)
async def get_plug(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Look up a single plug by ID. Verifies the user has access to it.
    This is called when a driver manually enters a Plug ID in the app.
    """
    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()

    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    # Access check: verify user can see this plug
    if plug.group_id:
        group_result = await db.execute(
            select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
        )
        group = group_result.scalar_one_or_none()

        if group and not group.is_public:
            # Private group — check membership
            membership_result = await db.execute(
                select(GroupMembership).where(
                    and_(
                        GroupMembership.user_id == user.id,
                        GroupMembership.group_id == group.id,
                    )
                )
            )
            if not membership_result.scalar_one_or_none():
                raise HTTPException(
                    status_code=403,
                    detail="This plug belongs to a private group. Join the group first using an access code.",
                )

    # Get group name
    group_name = None
    if plug.group_id:
        gn_result = await db.execute(
            select(ChargerGroup.name).where(ChargerGroup.id == plug.group_id)
        )
        row = gn_result.first()
        group_name = row[0] if row else None

    # Effective coords: the plug's own, else its gateway's site location.
    # Also load the gateway to report live reachability to the driver.
    gw_row = await db.execute(
        select(Gateway).where(Gateway.id == plug.gateway_id)
    )
    gateway = gw_row.scalar_one_or_none()
    gw_lat = gateway.latitude if gateway else None
    gw_lng = gateway.longitude if gateway else None

    # Resolved effective rate: plug tariff -> group tariff -> tenant default
    # -> env fallback (services/pricing.py resolve_rate_for_plug).
    price_per_kwh = await resolve_rate_for_plug(db, plug)

    return PlugResponse(
        id=plug.id,
        name=plug.name,
        status=plug.status.value,
        current_power_w=plug.current_power_w,
        plug_model=plug.plug_model,
        group_name=group_name,
        latitude=plug.latitude if plug.latitude is not None else gw_lat,
        longitude=plug.longitude if plug.longitude is not None else gw_lng,
        gateway_online=gateway_is_live(gateway) if gateway else False,
        price_per_kwh=float(price_per_kwh),
    )



