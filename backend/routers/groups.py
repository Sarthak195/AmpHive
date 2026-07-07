"""
Groups routes — moved verbatim from main.py (2026-07-07, TD#7 split).
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

# ===========================================================================
# Charger Group Endpoints
# ===========================================================================

@router.post("/api/groups/join")
async def join_group(
    req: JoinGroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Join a private charger group using an access code.
    Once joined, the user can see and use all plugs in that group.
    """
    # Find the group by access code
    result = await db.execute(
        select(ChargerGroup).where(ChargerGroup.access_code == req.access_code)
    )
    group = result.scalar_one_or_none()

    if not group:
        raise HTTPException(status_code=404, detail="Invalid access code. No group found.")

    if group.is_public:
        raise HTTPException(status_code=400, detail="This group is public. No access code needed.")

    # Check if already a member
    result = await db.execute(
        select(GroupMembership).where(
            and_(GroupMembership.user_id == user.id, GroupMembership.group_id == group.id)
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="You are already a member of this group.")

    # Create membership
    membership = GroupMembership(user_id=user.id, group_id=group.id)
    db.add(membership)
    await db.commit()
    logger.info(f"User {user.email} joined group '{group.name}' (id={group.id})")

    return {"status": "joined", "group_id": group.id, "group_name": group.name}


@router.get("/api/groups/my", response_model=List[GroupResponse])
async def get_my_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List all charger groups the current user has access to:
    - All public groups
    - All private groups the user has joined
    """
    # One round-trip: public groups ∪ joined groups, with plug counts
    # aggregated in SQL (previously one COUNT query per group — N+1).
    # DISTINCT on the plug count because a (theoretical) duplicate membership
    # row would otherwise multiply it.
    result = await db.execute(
        select(ChargerGroup, func.count(func.distinct(Plug.id)))
        .outerjoin(
            GroupMembership,
            and_(
                GroupMembership.group_id == ChargerGroup.id,
                GroupMembership.user_id == user.id,
            ),
        )
        .outerjoin(Plug, Plug.group_id == ChargerGroup.id)
        .where(or_(ChargerGroup.is_public == True, GroupMembership.id.is_not(None)))
        .group_by(ChargerGroup.id)
    )

    return [
        GroupResponse(
            id=group.id,
            name=group.name,
            is_public=group.is_public,
            plug_count=plug_count,
        )
        for group, plug_count in result.all()
    ]


