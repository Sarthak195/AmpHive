"""
Cpo routes — moved verbatim from main.py (2026-07-07, TD#7 split).
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
# CPO (Charge Point Operator) Admin Endpoints
# ===========================================================================
# These endpoints power the CPO admin dashboard. All endpoints (except setup)
# require the user to have the 'cpo' role, enforced via the require_role()
# RBAC dependency.
#
# The CPO dashboard allows property owners/managers to:
# - Register and manage ESP32 gateways and smart plugs
# - Create and manage charger groups (public/private with access codes)
# - View analytics: session history, revenue, energy consumption
# ===========================================================================


# --- CPO Setup & Profile ---

@router.post("/api/cpo/setup")
async def cpo_setup(
    req: CpoSetupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    CPO onboarding: create a new tenant (organization) and promote the
    current user to the 'cpo' role. This is a one-time operation — users
    who already have a tenant_id cannot call this again.

    This is the entry point for any driver who wants to become a Charge
    Point Operator and start managing their own plugs and groups.
    """
    # Prevent double-setup: if user already has a tenant, reject
    if user.tenant_id is not None:
        raise HTTPException(
            status_code=400,
            detail="You are already associated with a tenant. Cannot create another.",
        )

    # Check tenant name uniqueness
    existing = await db.execute(
        select(Tenant).where(Tenant.name == req.tenant_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail=f"A tenant with the name '{req.tenant_name}' already exists.",
        )

    # Create the tenant
    tenant = Tenant(name=req.tenant_name)
    db.add(tenant)
    try:
        await db.flush()  # Get the tenant.id before committing

        # Promote user to CPO role and link to the new tenant
        user.role = UserRole.CPO
        user.tenant_id = tenant.id
        await db.commit()
    except IntegrityError:
        # Lost the race to a concurrent setup with the same tenant name (the
        # name-uniqueness SELECT above can't see an uncommitted twin).
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"A tenant with the name '{req.tenant_name}' already exists.",
        )
    await db.refresh(user)
    await db.refresh(tenant)

    logger.info(f"CPO setup complete: user={user.email} → tenant='{tenant.name}' (id={tenant.id})")

    return {
        "status": "success",
        "tenant_id": tenant.id,
        "tenant_name": tenant.name,
        "user_role": user.role.value,
        "message": f"Welcome, CPO! Your organization '{tenant.name}' has been created.",
    }


@router.get("/api/cpo/profile")
async def cpo_profile(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Get the CPO's profile including tenant information and summary stats.
    """
    # Load tenant
    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == user.tenant_id)
    )
    tenant = tenant_result.scalar_one_or_none()

    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    # Count gateways and plugs
    gw_result = await db.execute(
        select(func.count(Gateway.id)).where(Gateway.tenant_id == tenant.id)
    )
    gateway_count = gw_result.scalar() or 0

    # Count plugs across all gateways owned by this tenant
    plug_result = await db.execute(
        select(func.count(Plug.id))
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(Gateway.tenant_id == tenant.id)
    )
    plug_count = plug_result.scalar() or 0

    # Count groups
    group_result = await db.execute(
        select(func.count(ChargerGroup.id)).where(ChargerGroup.tenant_id == tenant.id)
    )
    group_count = group_result.scalar() or 0

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role.value,
        },
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        },
        "stats": {
            "gateway_count": gateway_count,
            "plug_count": plug_count,
            "group_count": group_count,
        },
    }


# --- CPO Gateway Management ---

@router.get("/api/cpo/gateways")
async def cpo_list_gateways(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all gateways owned by the CPO's tenant."""
    result = await db.execute(
        select(Gateway).where(Gateway.tenant_id == user.tenant_id)
    )
    gateways = list(result.scalars().all())

    response = []
    for gw in gateways:
        # Count plugs on this gateway
        plug_count_result = await db.execute(
            select(func.count(Plug.id)).where(Plug.gateway_id == gw.id)
        )
        plug_count = plug_count_result.scalar() or 0

        response.append({
            "id": gw.id,
            "name": gw.name,
            "vpn_ip": gw.vpn_ip,
            "status": gw.status.value,
            "last_seen_at": gw.last_seen_at.isoformat() if gw.last_seen_at else None,
            "created_at": gw.created_at.isoformat() if gw.created_at else None,
            "plug_count": plug_count,
        })

    return response


@router.post("/api/cpo/gateways")
async def cpo_create_gateway(
    req: CpoGatewayCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new ESP32 gateway under the CPO's tenant.
    This is the authenticated version of the legacy /api/gateways/register
    endpoint — CPOs should use this instead.
    """
    # Check for duplicate gateway ID
    existing = await db.execute(select(Gateway).where(Gateway.id == req.gateway_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Gateway '{req.gateway_id}' already exists.")

    gateway = Gateway(
        id=req.gateway_id,
        tenant_id=user.tenant_id,
        name=req.name,
        vpn_ip=req.vpn_ip,
    )
    db.add(gateway)
    await db.commit()
    await db.refresh(gateway)

    logger.info(f"CPO gateway registered: {gateway.id} ({gateway.name}) by {user.email}")

    return {
        "status": "registered",
        "gateway_id": gateway.id,
        "name": gateway.name,
        "vpn_ip": gateway.vpn_ip,
    }


# --- CPO Plug Management ---

@router.get("/api/cpo/plugs")
async def cpo_list_plugs(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all plugs across the CPO's gateways with status and group info."""
    # Group name joined in — previously one ChargerGroup.name query per plug.
    result = await db.execute(
        select(Plug, ChargerGroup.name)
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .outerjoin(ChargerGroup, ChargerGroup.id == Plug.group_id)
        .where(Gateway.tenant_id == user.tenant_id)
    )

    return [
        {
            "id": plug.id,
            "name": plug.name,
            "gateway_id": plug.gateway_id,
            "local_ip": plug.local_ip,
            "plug_model": plug.plug_model,
            "status": plug.status.value,
            "current_power_w": plug.current_power_w,
            "group_id": plug.group_id,
            "group_name": group_name,
            "latitude": plug.latitude,
            "longitude": plug.longitude,
            "last_seen_at": plug.last_seen_at.isoformat() if plug.last_seen_at else None,
            "created_at": plug.created_at.isoformat() if plug.created_at else None,
        }
        for plug, group_name in result.all()
    ]


@router.post("/api/cpo/plugs")
async def cpo_create_plug(
    req: CpoPlugCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new smart plug on one of the CPO's gateways.
    Validates that the gateway belongs to the CPO's tenant.
    """
    # Verify the gateway belongs to this CPO's tenant
    gw_result = await db.execute(
        select(Gateway).where(
            and_(Gateway.id == req.gateway_id, Gateway.tenant_id == user.tenant_id)
        )
    )
    gateway = gw_result.scalar_one_or_none()
    if not gateway:
        raise HTTPException(
            status_code=404,
            detail=f"Gateway '{req.gateway_id}' not found or does not belong to your organization.",
        )

    # Validate group ownership if specified
    if req.group_id:
        group_result = await db.execute(
            select(ChargerGroup).where(
                and_(ChargerGroup.id == req.group_id, ChargerGroup.tenant_id == user.tenant_id)
            )
        )
        if not group_result.scalar_one_or_none():
            raise HTTPException(
                status_code=404,
                detail=f"Group {req.group_id} not found or does not belong to your organization.",
            )

    plug = Plug(
        gateway_id=req.gateway_id,
        name=req.name,
        local_ip=req.local_ip,
        plug_model=req.plug_model,
        group_id=req.group_id,
        latitude=req.latitude,
        longitude=req.longitude,
    )
    db.add(plug)
    await db.commit()
    await db.refresh(plug)

    logger.info(f"CPO plug registered: {plug.id} ({plug.name}) by {user.email}")

    return {
        "status": "registered",
        "plug_id": plug.id,
        "name": plug.name,
        "gateway_id": req.gateway_id,
    }


@router.put("/api/cpo/plugs/{plug_id}")
async def cpo_update_plug(
    plug_id: int,
    req: CpoPlugUpdateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Update an existing plug's details (name, group assignment, status).
    Only plugs on gateways owned by the CPO's tenant can be modified.
    """
    # Load plug and verify ownership via gateway → tenant chain
    result = await db.execute(
        select(Plug)
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(and_(Plug.id == plug_id, Gateway.tenant_id == user.tenant_id))
    )
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail="Plug not found or access denied.")

    # Apply updates
    if req.name is not None:
        plug.name = req.name
    if req.group_id is not None:
        # Validate group ownership
        if req.group_id != 0:  # 0 = remove from group
            group_result = await db.execute(
                select(ChargerGroup).where(
                    and_(ChargerGroup.id == req.group_id, ChargerGroup.tenant_id == user.tenant_id)
                )
            )
            if not group_result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="Group not found or access denied.")
            plug.group_id = req.group_id
        else:
            plug.group_id = None
    if req.status is not None:
        try:
            plug.status = PlugStatus(req.status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{req.status}'. Valid values: {[s.value for s in PlugStatus]}",
            )
    if req.latitude is not None:
        plug.latitude = req.latitude
    if req.longitude is not None:
        plug.longitude = req.longitude

    await db.commit()
    await db.refresh(plug)

    logger.info(f"CPO plug updated: {plug.id} ({plug.name}) by {user.email}")

    return {
        "status": "updated",
        "plug_id": plug.id,
        "name": plug.name,
        "plug_status": plug.status.value,
        "group_id": plug.group_id,
    }


# --- CPO Charger Group Management ---

@router.get("/api/cpo/groups")
async def cpo_list_groups(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all charger groups owned by the CPO's tenant."""
    result = await db.execute(
        select(ChargerGroup).where(ChargerGroup.tenant_id == user.tenant_id)
    )
    groups = list(result.scalars().all())

    response = []
    for group in groups:
        # Count plugs in this group
        plug_count_result = await db.execute(
            select(func.count(Plug.id)).where(Plug.group_id == group.id)
        )
        plug_count = plug_count_result.scalar() or 0

        # Count members (for private groups)
        member_count_result = await db.execute(
            select(func.count(GroupMembership.id)).where(GroupMembership.group_id == group.id)
        )
        member_count = member_count_result.scalar() or 0

        response.append({
            "id": group.id,
            "name": group.name,
            "is_public": group.is_public,
            "access_code": group.access_code if not group.is_public else None,
            "plug_count": plug_count,
            "member_count": member_count,
            "created_at": group.created_at.isoformat() if group.created_at else None,
        })

    return response


@router.post("/api/cpo/groups")
async def cpo_create_group(
    req: CpoGroupCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new charger group under the CPO's tenant.
    Private groups automatically get a generated access code.
    """
    import secrets
    import string

    access_code = None
    if not req.is_public:
        # Generate a unique 8-character alphanumeric access code
        # Using uppercase letters and digits for easy manual entry
        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            existing = await db.execute(
                select(ChargerGroup).where(ChargerGroup.access_code == code)
            )
            if not existing.scalar_one_or_none():
                access_code = code
                break

    group = ChargerGroup(
        tenant_id=user.tenant_id,
        name=req.name,
        is_public=req.is_public,
        access_code=access_code,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)

    logger.info(f"CPO group created: '{group.name}' (id={group.id}, public={group.is_public}) by {user.email}")

    return {
        "status": "created",
        "group_id": group.id,
        "name": group.name,
        "is_public": group.is_public,
        "access_code": access_code,
    }


@router.put("/api/cpo/groups/{group_id}")
async def cpo_update_group(
    group_id: int,
    req: CpoGroupUpdateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a charger group's details. Can regenerate access codes for private groups."""
    import secrets
    import string

    result = await db.execute(
        select(ChargerGroup).where(
            and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found or access denied.")

    if req.name is not None:
        group.name = req.name

    if req.is_public is not None:
        group.is_public = req.is_public
        # If switching to public, clear the access code
        if req.is_public:
            group.access_code = None
        # If switching to private, generate a new access code
        elif not group.access_code:
            while True:
                code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
                existing = await db.execute(
                    select(ChargerGroup).where(ChargerGroup.access_code == code)
                )
                if not existing.scalar_one_or_none():
                    group.access_code = code
                    break

    if req.regenerate_access_code and not group.is_public:
        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            existing = await db.execute(
                select(ChargerGroup).where(ChargerGroup.access_code == code)
            )
            if not existing.scalar_one_or_none():
                group.access_code = code
                break

    await db.commit()
    await db.refresh(group)

    logger.info(f"CPO group updated: '{group.name}' (id={group.id}) by {user.email}")

    return {
        "status": "updated",
        "group_id": group.id,
        "name": group.name,
        "is_public": group.is_public,
        "access_code": group.access_code,
    }


@router.delete("/api/cpo/groups/{group_id}")
async def cpo_delete_group(
    group_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a charger group. Plugs assigned to this group will have their
    group_id set to NULL (they become ungrouped, visible to all users).
    """
    result = await db.execute(
        select(ChargerGroup).where(
            and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found or access denied.")

    group_name = group.name

    # Unlink all plugs from this group before deletion
    plugs_result = await db.execute(
        select(Plug).where(Plug.group_id == group_id)
    )
    for plug in plugs_result.scalars().all():
        plug.group_id = None

    # Delete all memberships (cascade should handle this, but be explicit)
    memberships_result = await db.execute(
        select(GroupMembership).where(GroupMembership.group_id == group_id)
    )
    for membership in memberships_result.scalars().all():
        await db.delete(membership)

    await db.delete(group)
    await db.commit()

    logger.info(f"CPO group deleted: '{group_name}' (id={group_id}) by {user.email}")

    return {"status": "deleted", "group_id": group_id, "group_name": group_name}


# --- CPO Analytics ---

@router.get("/api/cpo/analytics/overview")
async def cpo_analytics_overview(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Summary analytics for the CPO dashboard:
    - Total plugs, active sessions, gateways online/offline
    - Today's energy consumption and revenue
    - All-time totals
    """
    tenant_id = user.tenant_id

    # Total plugs count
    plug_count_result = await db.execute(
        select(func.count(Plug.id))
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(Gateway.tenant_id == tenant_id)
    )
    total_plugs = plug_count_result.scalar() or 0

    # Active sessions right now
    active_sessions_result = await db.execute(
        select(func.count(ChargingSession.id))
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.status == SessionStatus.ACTIVE,
        ))
    )
    active_sessions = active_sessions_result.scalar() or 0

    # Gateways online/offline
    gw_online_result = await db.execute(
        select(func.count(Gateway.id))
        .where(and_(Gateway.tenant_id == tenant_id, Gateway.status == GatewayStatus.ONLINE))
    )
    gateways_online = gw_online_result.scalar() or 0

    gw_total_result = await db.execute(
        select(func.count(Gateway.id)).where(Gateway.tenant_id == tenant_id)
    )
    gateways_total = gw_total_result.scalar() or 0

    # Today's stats (energy and revenue from completed sessions started today)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_stats_result = await db.execute(
        select(
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            func.count(ChargingSession.id),
        )
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.started_at >= today_start,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
    )
    today_row = today_stats_result.first()
    today_energy = float(today_row[0]) if today_row else 0.0
    today_revenue = float(today_row[1]) if today_row else 0.0
    today_sessions = int(today_row[2]) if today_row else 0

    # All-time stats
    alltime_result = await db.execute(
        select(
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            func.count(ChargingSession.id),
        )
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
    )
    alltime_row = alltime_result.first()
    alltime_energy = float(alltime_row[0]) if alltime_row else 0.0
    alltime_revenue = float(alltime_row[1]) if alltime_row else 0.0
    alltime_sessions = int(alltime_row[2]) if alltime_row else 0

    return {
        "plugs": {"total": total_plugs},
        "gateways": {"online": gateways_online, "total": gateways_total},
        "active_sessions": active_sessions,
        "today": {
            "sessions": today_sessions,
            "energy_kwh": round(today_energy, 3),
            "revenue_coins": round(today_revenue, 2),
        },
        "all_time": {
            "sessions": alltime_sessions,
            "energy_kwh": round(alltime_energy, 3),
            "revenue_coins": round(alltime_revenue, 2),
        },
    }


@router.get("/api/cpo/analytics/sessions")
async def cpo_analytics_sessions(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    plug_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    days: int = 30,
    limit: int = 50,
):
    """
    Session history across all of the CPO's plugs.
    Supports optional filters: plug_id, status, and date range (days).
    Returns sessions ordered by most recent first.
    """
    # Base query: sessions belonging to this CPO's tenant, with plug name and
    # driver email joined in (previously two extra queries per session row).
    # Outer joins preserve the old fallback behavior for orphaned references.
    query = (
        select(ChargingSession, Plug.name, User.email)
        .outerjoin(Plug, Plug.id == ChargingSession.plug_id)
        .outerjoin(User, User.id == ChargingSession.user_id)
        .where(ChargingSession.tenant_id == user.tenant_id)
    )

    # Apply optional filters
    if plug_id:
        query = query.where(ChargingSession.plug_id == plug_id)
    if status_filter:
        try:
            status_enum = SessionStatus(status_filter)
            query = query.where(ChargingSession.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status filter '{status_filter}'. Valid: {[s.value for s in SessionStatus]}",
            )

    # Date range filter
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query = query.where(ChargingSession.started_at >= cutoff)

    # Order and limit
    query = query.order_by(ChargingSession.started_at.desc()).limit(limit)

    result = await db.execute(query)

    response = []
    for s, plug_name, user_email in result.all():
        plug_name = plug_name if plug_name is not None else f"Plug #{s.plug_id}"
        user_email = user_email if user_email is not None else "unknown"

        # Calculate duration
        duration_minutes = None
        if s.ended_at and s.started_at:
            duration_minutes = round((s.ended_at - s.started_at).total_seconds() / 60, 1)

        response.append({
            "id": s.id,
            "plug_id": s.plug_id,
            "plug_name": plug_name,
            "user_id": s.user_id,
            "user_email": user_email,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "duration_minutes": duration_minutes,
            "energy_kwh": round(s.energy_kwh, 3),
            "coins_spent": round(s.coins_spent, 2),
            "status": s.status.value,
        })

    return response


@router.get("/api/cpo/analytics/revenue")
async def cpo_analytics_revenue(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    """
    Daily revenue breakdown for the CPO's charting dashboard.
    Returns an array of {date, revenue_coins, session_count} for each day
    in the requested range, suitable for plotting a revenue trend line.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Query: group completed sessions by date, sum revenue
    result = await db.execute(
        select(
            cast(ChargingSession.started_at, Date).label("date"),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0).label("revenue"),
            func.count(ChargingSession.id).label("count"),
        )
        .where(and_(
            ChargingSession.tenant_id == user.tenant_id,
            ChargingSession.started_at >= cutoff,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
        .group_by(cast(ChargingSession.started_at, Date))
        .order_by(cast(ChargingSession.started_at, Date))
    )

    rows = result.all()

    return [
        {
            "date": str(row[0]),
            "revenue_coins": round(float(row[1]), 2),
            "session_count": int(row[2]),
        }
        for row in rows
    ]


@router.get("/api/cpo/analytics/energy")
async def cpo_analytics_energy(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    """
    Daily energy consumption breakdown for the CPO's charting dashboard.
    Returns an array of {date, energy_kwh, session_count} for each day
    in the requested range, suitable for plotting an energy consumption chart.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(
            cast(ChargingSession.started_at, Date).label("date"),
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0).label("energy"),
            func.count(ChargingSession.id).label("count"),
        )
        .where(and_(
            ChargingSession.tenant_id == user.tenant_id,
            ChargingSession.started_at >= cutoff,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
        .group_by(cast(ChargingSession.started_at, Date))
        .order_by(cast(ChargingSession.started_at, Date))
    )

    rows = result.all()

    return [
        {
            "date": str(row[0]),
            "energy_kwh": round(float(row[1]), 3),
            "session_count": int(row[2]),
        }
        for row in rows
    ]


@router.get("/api/cpo/analytics/telemetry")
async def cpo_analytics_telemetry(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    plug_id: Optional[int] = None,
    days: int = 1,
    bucket: str = "hour",
):
    """
    Downsampled time-series telemetry for the CPO's load graphs / energy audits.

    Buckets raw `telemetry_readings` via date_trunc and returns average / peak
    power plus the cumulative energy reading per bucket. Tenant-scoped (uses the
    denormalized telemetry_readings.tenant_id); optional plug_id filter.

    Returns an array of
    {timestamp, avg_power_w, max_power_w, energy_kwh, sample_count}.
    """
    allowed_buckets = {"minute", "hour", "day"}
    if bucket not in allowed_buckets:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bucket '{bucket}'. Allowed: {sorted(allowed_buckets)}.",
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    bucket_col = func.date_trunc(bucket, TelemetryReading.recorded_at).label("bucket")

    conditions = [
        TelemetryReading.tenant_id == user.tenant_id,
        TelemetryReading.recorded_at >= cutoff,
    ]
    if plug_id is not None:
        conditions.append(TelemetryReading.plug_id == plug_id)

    result = await db.execute(
        select(
            bucket_col,
            func.avg(TelemetryReading.power_w).label("avg_power_w"),
            func.max(TelemetryReading.power_w).label("max_power_w"),
            # energy_kwh is cumulative-per-session, so max() = value at end of bucket
            func.max(TelemetryReading.energy_kwh).label("energy_kwh"),
            func.count(TelemetryReading.id).label("sample_count"),
        )
        .where(and_(*conditions))
        .group_by(bucket_col)
        .order_by(bucket_col)
    )

    rows = result.all()

    return [
        {
            "timestamp": row[0].isoformat() if row[0] else None,
            "avg_power_w": round(float(row[1]), 1),
            "max_power_w": round(float(row[2]), 1),
            "energy_kwh": round(float(row[3]), 3),
            "sample_count": int(row[4]),
        }
        for row in rows
    ]


# Wrap FastAPI app with Socket.io ASGI wrapper so they run on the same port
