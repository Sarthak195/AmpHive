"""
Cpo routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import csv
import io
import json
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import async_session_factory, get_db
from backend.database.models import (
    AuditLog, ChargerGroup, ChargingSession, Gateway, GatewayEvent,
    GatewayStatus, GroupMembership, LedgerTransaction, Payout,
    PayoutStatus, Plug, PlugStatus, SessionStatus, TelemetryReading,
    Tenant, TransactionType, User, UserRole,
)
from backend.schemas import (
    AuthResponse, CpoGatewayCreateRequest, CpoGatewayOtaRequest,
    CpoGroupCreateRequest, CpoGroupUpdateRequest, CpoPlugCreateRequest,
    CpoPlugMaintenanceRequest, CpoPlugUpdateRequest, CpoSetupRequest,
    CreateOrderRequest, CreateOrderResponse, DirectPlugRequest,
    GatewayEventResponse, GatewayRegisterRequest, GroupResponse,
    JoinGroupRequest, LoginRequest, PlugRegisterRequest, PlugResponse,
    RegisterRequest, SessionStartRequest, SessionStopRequest, UserResponse,
    VerifyPaymentRequest,
)
from backend.services import payments as payment_service
from backend.services.audit import try_record_audit

from backend.services import payouts as payout_service
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


async def generate_unique_access_code(db: AsyncSession) -> str:
    """
    Unique 8-character access code for a private charger group — uppercase
    letters and digits only, for easy manual entry. Retries until the code
    is unused (the column is UNIQUE; collisions on 36^8 are near-impossible,
    the loop just makes them a retry instead of an IntegrityError).
    """
    while True:
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        existing = await db.execute(
            select(ChargerGroup).where(ChargerGroup.access_code == code)
        )
        if not existing.scalar_one_or_none():
            return code


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
            "firmware_version": gw.firmware_version,
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

    # vpn_ip is a legacy overlay field (NOT NULL + UNIQUE) that direct-MQTT
    # gateways don't use. Fall back to the gateway_id so the unique constraint
    # is satisfied without forcing the operator to invent an overlay IP.
    gateway = Gateway(
        id=req.gateway_id,
        tenant_id=user.tenant_id,
        name=req.name,
        vpn_ip=req.vpn_ip or req.gateway_id,
    )
    db.add(gateway)
    await db.commit()
    await db.refresh(gateway)

    logger.info(f"CPO gateway registered: {gateway.id} ({gateway.name}) by {user.email}")

    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="gateway.create",
        target_type="gateway",
        target_id=gateway.id,
        detail=f"name={gateway.name}, vpn_ip={gateway.vpn_ip}",
    )

    return {
        "status": "registered",
        "gateway_id": gateway.id,
        "name": gateway.name,
        "vpn_ip": gateway.vpn_ip,
    }


@router.post("/api/cpo/gateways/{gateway_id}/ota")
async def cpo_gateway_ota(
    gateway_id: str,
    req: CpoGatewayOtaRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger an OTA firmware update on one of the CPO's gateways.

    The firmware downloads `firmware_url` into its passive OTA slot and
    reboots (rollback-protected: an image that fails to reach the broker is
    reverted on the next boot). The gateway must be live, and it refuses the
    update on its side if a charging session is active on it.
    """
    gw_result = await db.execute(
        select(Gateway).where(
            and_(Gateway.id == gateway_id, Gateway.tenant_id == user.tenant_id)
        )
    )
    gateway = gw_result.scalar_one_or_none()
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found or access denied.")

    # OTA deliberately uses the ONLINE flag, NOT gateway_is_live(): the latter
    # requires fresh *telemetry*, which stops whenever the plug is unreachable
    # even though the gateway's MQTT link (and thus its ability to receive an
    # OTA) is fine. Coupling firmware updates to plug health would be wrong,
    # and a false positive here is benign — the OTA command is a QoS-1 publish
    # that a truly-gone gateway simply never receives (no session pinned, no
    # billing), unlike session-start where gateway_is_live guards real money.
    if gateway.status != GatewayStatus.ONLINE:
        raise HTTPException(
            status_code=409,
            detail="Gateway is offline; it must be connected to receive an OTA command.",
        )

    # The firmware only subscribes to the per-plug command topic, so the OTA
    # command rides one of the gateway's plugs (the firmware ignores plug_id
    # for OTA). Any registered plug works; there must be at least one.
    plug_result = await db.execute(
        select(Plug.id).where(Plug.gateway_id == gateway_id).order_by(Plug.id).limit(1)
    )
    plug_id = plug_result.scalar_one_or_none()
    if plug_id is None:
        raise HTTPException(
            status_code=409,
            detail="Gateway has no registered plugs to route the OTA command through.",
        )

    published = state.mqtt_manager.send_gateway_ota(gateway_id, plug_id, req.firmware_url)
    if not published:
        raise HTTPException(
            status_code=502,
            detail="Failed to publish the OTA command to the broker.",
        )

    logger.info(
        f"OTA command published for gateway {gateway_id} "
        f"(url={req.firmware_url}) by {user.email}"
    )
    return {
        "status": "ota_triggered",
        "gateway_id": gateway_id,
        "firmware_url": req.firmware_url,
        "message": "OTA command published. Watch the gateway's alarms topic / status fw version.",
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

    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="plug.create",
        target_type="plug",
        target_id=plug.id,
        detail=f"name={plug.name}, gateway_id={plug.gateway_id}",
    )

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

    old_status = plug.status  # captured pre-mutation for the status_change audit diff

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

    if req.status is not None and plug.status != old_status:
        await try_record_audit(
            db,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            action="plug.status_change",
            target_type="plug",
            target_id=plug.id,
            detail=f"{old_status.value} -> {plug.status.value}",
        )

    return {
        "status": "updated",
        "plug_id": plug.id,
        "name": plug.name,
        "plug_status": plug.status.value,
        "group_id": plug.group_id,
    }


@router.post("/api/cpo/plugs/{plug_id}/maintenance")
async def cpo_plug_maintenance(
    plug_id: int,
    req: CpoPlugMaintenanceRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Operator maintenance workflow (fault console): explicitly put a plug into
    MAINTENANCE or clear it back to AVAILABLE. Distinct from the general
    status setter on PUT /api/cpo/plugs/{id} — this is the dedicated action
    the fault console drives, and it always audits (action
    `plug.maintenance_enter` / `plug.maintenance_clear`).

    `clear` is refused (409) while the plug still has an ACTIVE session —
    mirrors session-start's own 409 on a non-AVAILABLE plug, just from the
    other direction: a plug can't be handed back into service out from under
    a driver mid-charge.
    """
    action = (req.action or "").strip().lower()
    if action not in ("enter", "clear"):
        raise HTTPException(
            status_code=400,
            detail="Invalid action. Must be 'enter' or 'clear'.",
        )

    result = await db.execute(
        select(Plug)
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(and_(Plug.id == plug_id, Gateway.tenant_id == user.tenant_id))
    )
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail="Plug not found or access denied.")

    old_status = plug.status  # captured pre-mutation for the audit diff

    if action == "enter":
        plug.status = PlugStatus.MAINTENANCE
    else:  # clear
        active_result = await db.execute(
            select(func.count())
            .select_from(ChargingSession)
            .where(and_(
                ChargingSession.plug_id == plug_id,
                ChargingSession.status == SessionStatus.ACTIVE,
            ))
        )
        if active_result.scalar_one() > 0:
            raise HTTPException(
                status_code=409,
                detail="Cannot clear maintenance: this plug has an active charging session.",
            )
        plug.status = PlugStatus.AVAILABLE

    await db.commit()
    await db.refresh(plug)

    logger.info(
        f"CPO plug maintenance '{action}': {plug.id} ({plug.name}) "
        f"{old_status.value} -> {plug.status.value} by {user.email}"
    )

    detail = f"{old_status.value} -> {plug.status.value}"
    if req.note:
        detail += f"; note={req.note}"
    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action=f"plug.maintenance_{action}",
        target_type="plug",
        target_id=plug.id,
        detail=detail,
    )

    if plug.status != old_status:
        from backend.services.socketio_manager import emit_plug_status
        await emit_plug_status(plug.id, plug.status.value)

    return {
        "status": "updated",
        "plug_id": plug.id,
        "action": action,
        "plug_status": plug.status.value,
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
    access_code = None
    if not req.is_public:
        access_code = await generate_unique_access_code(db)

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

    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="group.create",
        target_type="group",
        target_id=group.id,
        detail=f"name={group.name}, is_public={group.is_public}",
    )

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
    result = await db.execute(
        select(ChargerGroup).where(
            and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found or access denied.")

    old_access_code = group.access_code  # captured pre-mutation for the access_code.regen audit

    if req.name is not None:
        group.name = req.name

    if req.is_public is not None:
        group.is_public = req.is_public
        # If switching to public, clear the access code
        if req.is_public:
            group.access_code = None
        # If switching to private, generate a new access code
        elif not group.access_code:
            group.access_code = await generate_unique_access_code(db)

    if req.regenerate_access_code and not group.is_public:
        group.access_code = await generate_unique_access_code(db)

    await db.commit()
    await db.refresh(group)

    logger.info(f"CPO group updated: '{group.name}' (id={group.id}) by {user.email}")

    # Only a genuine rotation to a NEW code counts as access_code.regen — not
    # the is_public:true path above, which clears the code to None (that's a
    # removal, not a regen, and isn't part of the audited action taxonomy).
    if group.access_code is not None and group.access_code != old_access_code:
        await try_record_audit(
            db,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            action="access_code.regen",
            target_type="group",
            target_id=group.id,
            detail=f"name={group.name}",
        )

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

    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="group.delete",
        target_type="group",
        target_id=group_id,
        detail=f"name={group_name}",
    )

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


@router.get("/api/cpo/analytics/sessions.csv")
async def cpo_export_sessions_csv(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    plug_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    days: int = 30,
):
    """
    Export the CPO's session history as CSV (accounting / spreadsheet import).
    Same tenant scope and filters as `/api/cpo/analytics/sessions`, but returns
    a downloadable `text/csv` attachment. Capped at 10k rows to bound memory.
    """
    query = (
        select(ChargingSession, Plug.name, User.email)
        .outerjoin(Plug, Plug.id == ChargingSession.plug_id)
        .outerjoin(User, User.id == ChargingSession.user_id)
        .where(ChargingSession.tenant_id == user.tenant_id)
    )
    if plug_id:
        query = query.where(ChargingSession.plug_id == plug_id)
    if status_filter:
        try:
            query = query.where(ChargingSession.status == SessionStatus(status_filter))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status filter '{status_filter}'. Valid: {[s.value for s in SessionStatus]}",
            )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query = query.where(ChargingSession.started_at >= cutoff)
    query = query.order_by(ChargingSession.started_at.desc()).limit(10000)

    result = await db.execute(query)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "session_id", "plug_id", "plug_name", "user_email",
        "started_at", "ended_at", "duration_minutes", "energy_kwh",
        "coins_spent", "status",
    ])
    for s, plug_name, user_email in result.all():
        duration_minutes = ""
        if s.ended_at and s.started_at:
            duration_minutes = round((s.ended_at - s.started_at).total_seconds() / 60, 1)
        writer.writerow([
            s.id,
            s.plug_id,
            plug_name if plug_name is not None else f"Plug #{s.plug_id}",
            user_email if user_email is not None else "unknown",
            s.started_at.isoformat() if s.started_at else "",
            s.ended_at.isoformat() if s.ended_at else "",
            duration_minutes,
            round(s.energy_kwh, 3),
            round(float(s.coins_spent), 2),
            s.status.value,
        ])

    filename = f"amphive-sessions-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
            # Current (amps): avg + peak per bucket, so a CPO can see draw, not
            # just power. Persisted on every reading (derived power/voltage).
            func.avg(TelemetryReading.current_a).label("avg_current_a"),
            func.max(TelemetryReading.current_a).label("max_current_a"),
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
            "avg_current_a": round(float(row[4]), 2),
            "max_current_a": round(float(row[5]), 2),
            "sample_count": int(row[6]),
        }
        for row in rows
    ]


# --- CPO Gateway Events / Alerts ---

@router.get("/api/cpo/events", response_model=List[GatewayEventResponse])
async def cpo_list_events(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    unacknowledged_only: bool = False,
    severity: Optional[str] = None,
):
    """
    Gateway/plug operational events for the CPO's tenant — safety cutoffs,
    unauthorized-on alarms, OTA lifecycle notices — newest first. Powers the
    operator alert feed. `unacknowledged_only` narrows to the active alert set;
    `severity` filters (e.g. "critical").
    """
    limit = max(1, min(limit, 200))
    conditions = [GatewayEvent.tenant_id == user.tenant_id]
    if unacknowledged_only:
        conditions.append(GatewayEvent.acknowledged == False)  # noqa: E712
    if severity:
        conditions.append(GatewayEvent.severity == severity)

    result = await db.execute(
        select(GatewayEvent)
        .where(and_(*conditions))
        .order_by(GatewayEvent.created_at.desc(), GatewayEvent.id.desc())
        .limit(limit)
    )
    events = list(result.scalars().all())

    return [
        GatewayEventResponse(
            id=ev.id,
            gateway_id=ev.gateway_id,
            plug_id=ev.plug_id,
            event_type=ev.event_type,
            severity=ev.severity,
            detail=ev.detail,
            acknowledged=ev.acknowledged,
            created_at=ev.created_at.isoformat() if ev.created_at else None,
        )
        for ev in events
    ]


@router.post("/api/cpo/events/{event_id}/ack")
async def cpo_acknowledge_event(
    event_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Acknowledge (clear from the active feed) a single event. Tenant-scoped."""
    result = await db.execute(
        select(GatewayEvent).where(
            and_(GatewayEvent.id == event_id, GatewayEvent.tenant_id == user.tenant_id)
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found or access denied.")

    event.acknowledged = True
    await db.commit()
    return {"status": "acknowledged", "event_id": event_id}


# --- CPO Admin Audit Trail ---

@router.get("/api/cpo/audit")
async def cpo_list_audit_log(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """
    CPO admin action audit trail (TD#26) — gateway/plug/group create-delete,
    status changes, and access-code regeneration for the CPO's tenant, newest
    first. `limit` capped at 200 per page; `offset` paginates past it.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    result = await db.execute(
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .where(AuditLog.tenant_id == user.tenant_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )

    return [
        {
            "id": entry.id,
            "actor_user_id": entry.actor_user_id,
            "actor_email": actor_email,
            "action": entry.action,
            "target_type": entry.target_type,
            "target_id": entry.target_id,
            "detail": entry.detail,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
        for entry, actor_email in result.all()
    ]

# ===========================================================================
# CPO Payout / Settlement Ledger
# ===========================================================================
# Manual settlement — RECORD-KEEPING ONLY. There is no bank/UPI/payment-
# gateway integration: a CPO snapshots its unsettled coin earnings into a
# REQUESTED payout, and the platform operator (admin) marks it PAID once the
# transfer has happened out-of-band. See services/payouts.py for the
# earnings/watermark math (requirements.md §5.1's "withdraw earnings" promise
# — MARKET_GAP_ANALYSIS.md §1.6 / §7).
# ===========================================================================


def _require_tenant_id(user: User) -> int:
    """Every /api/cpo/payouts* and /api/cpo/earnings route is tenant-scoped.
    A 'cpo' always has a tenant_id (set by cpo_setup); a bare 'admin' with no
    tenant attached hits this — same shape as cpo_setup's own checks."""
    if user.tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail="You are not associated with a tenant.",
        )
    return user.tenant_id


def _payout_response(payout: Payout) -> dict:
    return {
        "id": payout.id,
        "tenant_id": payout.tenant_id,
        "period_start": payout.period_start.isoformat() if payout.period_start else None,
        "period_end": payout.period_end.isoformat() if payout.period_end else None,
        "gross_coins": float(payout.gross_coins),
        "platform_fee_coins": float(payout.platform_fee_coins),
        "net_coins": float(payout.net_coins),
        "status": payout.status.value,
        "requested_by_user_id": payout.requested_by_user_id,
        "requested_at": payout.requested_at.isoformat() if payout.requested_at else None,
        "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
        "note": payout.note,
    }


@router.get("/api/cpo/earnings")
async def cpo_earnings(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Lifetime + unsettled earnings for the CPO's tenant: gross coins collected
    from COMPLETED sessions, the platform's cut, and the net owed to the CPO
    — plus the settlement watermark (unsettled = watermark -> now). This is
    the read-only dashboard view; POST /api/cpo/payouts snapshots the
    unsettled leg into a payout request.
    """
    tenant_id = _require_tenant_id(user)
    summary = await payout_service.tenant_earnings_summary(db, tenant_id)

    return {
        "watermark": summary["watermark"].isoformat(),
        "as_of": summary["now"].isoformat(),
        "platform_fee_pct": float(payout_service.platform_fee_pct()),
        "lifetime": {
            "gross_coins": float(summary["lifetime_gross_coins"]),
            "platform_fee_coins": float(summary["lifetime_platform_fee_coins"]),
            "net_coins": float(summary["lifetime_net_coins"]),
        },
        "unsettled": {
            "period_start": summary["watermark"].isoformat(),
            "period_end": summary["now"].isoformat(),
            "gross_coins": float(summary["unsettled_gross_coins"]),
            "platform_fee_coins": float(summary["unsettled_platform_fee_coins"]),
            "net_coins": float(summary["unsettled_net_coins"]),
        },
    }


@router.post("/api/cpo/payouts")
async def cpo_request_payout(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Snapshot the tenant's unsettled earnings (watermark -> now) into a new
    REQUESTED payout. 400 if there's nothing to settle (unsettled net <= 0),
    409 if a payout is already REQUESTED for this tenant (only one pending
    settlement at a time).

    Race-safe: row-locks the tenant for the duration of this transaction, so
    two concurrent requests serialize — the loser re-reads the watermark
    (and the now-committed REQUESTED row) after the winner commits, and gets
    the 409 instead of snapshotting an overlapping window. See
    services/payouts.py for the watermark definition.
    """
    tenant_id = _require_tenant_id(user)

    lock_result = await db.execute(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()
    )
    if lock_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    existing = await db.execute(
        select(Payout.id).where(
            and_(Payout.tenant_id == tenant_id, Payout.status == PayoutStatus.REQUESTED)
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="A payout request is already pending for this tenant.",
        )

    watermark = await payout_service.tenant_settlement_watermark(db, tenant_id)
    now = datetime.now(timezone.utc)
    gross = await payout_service.sum_completed_session_coins(
        db, tenant_id, window_start=watermark, window_end=now
    )
    fee, net = payout_service.compute_fee_and_net(gross)
    if net <= ZERO_MONEY:
        raise HTTPException(
            status_code=400,
            detail="No unsettled earnings to pay out.",
        )

    payout = Payout(
        tenant_id=tenant_id,
        period_start=watermark,
        period_end=now,
        gross_coins=gross,
        platform_fee_coins=fee,
        net_coins=net,
        status=PayoutStatus.REQUESTED,
        requested_by_user_id=user.id,
    )
    db.add(payout)
    await db.commit()
    await db.refresh(payout)

    logger.info(
        f"CPO payout requested: tenant={tenant_id} payout={payout.id} "
        f"window=[{watermark.isoformat()}, {now.isoformat()}) "
        f"gross={gross} fee={fee} net={net} by {user.email}"
    )
    return _payout_response(payout)


@router.get("/api/cpo/payouts")
async def cpo_list_payouts(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List the tenant's payouts, newest request first."""
    tenant_id = _require_tenant_id(user)
    result = await db.execute(
        select(Payout)
        .where(Payout.tenant_id == tenant_id)
        .order_by(Payout.requested_at.desc(), Payout.id.desc())
    )
    return [_payout_response(p) for p in result.scalars().all()]


@router.post("/api/cpo/payouts/{payout_id}/mark_paid")
async def cpo_mark_payout_paid(
    payout_id: int,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Platform operator marks a payout PAID after settling it manually
    out-of-band (bank transfer / UPI outside this app). Admin-only — a CPO
    cannot mark its own payout paid. 409 if the payout isn't REQUESTED
    (already PAID/CANCELLED, or a concurrent mark_paid/cancel got there
    first — the row lock below makes the state check itself race-safe).
    """
    result = await db.execute(
        select(Payout).where(Payout.id == payout_id).with_for_update()
    )
    payout = result.scalar_one_or_none()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout not found.")
    if payout.status != PayoutStatus.REQUESTED:
        raise HTTPException(
            status_code=409,
            detail=f"Payout is '{payout.status.value}', not 'requested'.",
        )

    payout.status = PayoutStatus.PAID
    payout.paid_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(payout)

    logger.info(f"CPO payout {payout.id} marked PAID by admin {user.email}")
    return _payout_response(payout)


@router.post("/api/cpo/payouts/{payout_id}/cancel")
async def cpo_cancel_payout(
    payout_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Cancel a REQUESTED payout, freeing its window for a future request (the
    watermark excludes CANCELLED payouts). The owning CPO or an admin may
    cancel; any other tenant's CPO gets 404 (not "403", so a cross-tenant
    caller can't distinguish "not yours" from "doesn't exist"). 409 if the
    payout isn't REQUESTED — same race-safe row-lock as mark_paid.
    """
    result = await db.execute(
        select(Payout).where(Payout.id == payout_id).with_for_update()
    )
    payout = result.scalar_one_or_none()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout not found.")
    if user.role != UserRole.ADMIN and payout.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Payout not found.")
    if payout.status != PayoutStatus.REQUESTED:
        raise HTTPException(
            status_code=409,
            detail=f"Payout is '{payout.status.value}', not 'requested'.",
        )

    payout.status = PayoutStatus.CANCELLED
    await db.commit()
    await db.refresh(payout)

    logger.info(f"CPO payout {payout.id} cancelled by {user.email}")
    return _payout_response(payout)


# ===========================================================================
# CPO Tariff (per-CPO/per-site pricing) Management
# ===========================================================================
# Appended at the end of the file (feat/tariff-pricing) to avoid touching the
# shared header import block above, which another concurrent change also
# edits. Local imports here are intentional, not an oversight.
from backend.database.models import Tariff  # noqa: E402
from backend.schemas import (  # noqa: E402
    CpoTariffAssignRequest, CpoTariffCreateRequest, CpoTariffUpdateRequest,
)


async def _load_tenant_tariff(db: AsyncSession, tariff_id: int, tenant_id: int) -> Tariff:
    """Fetch a tariff, enforcing it belongs to `tenant_id`. Shared by the
    three assignment endpoints below so cross-tenant assignment (attaching
    another tenant's tariff to your plug/group/tenant-default) is rejected
    the same way everywhere."""
    result = await db.execute(
        select(Tariff).where(and_(Tariff.id == tariff_id, Tariff.tenant_id == tenant_id))
    )
    tariff = result.scalar_one_or_none()
    if not tariff:
        raise HTTPException(
            status_code=404,
            detail="Tariff not found or does not belong to your organization.",
        )
    return tariff


@router.get("/api/cpo/tariffs")
async def cpo_list_tariffs(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all tariffs (pricing plans) owned by the CPO's tenant."""
    result = await db.execute(
        select(Tariff).where(Tariff.tenant_id == user.tenant_id).order_by(Tariff.name)
    )
    tariffs = list(result.scalars().all())

    return [
        {
            "id": t.id,
            "name": t.name,
            "price_per_kwh": float(t.price_per_kwh),
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        for t in tariffs
    ]


@router.post("/api/cpo/tariffs")
async def cpo_create_tariff(
    req: CpoTariffCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new tariff under the CPO's tenant."""
    tariff = Tariff(
        tenant_id=user.tenant_id,
        name=req.name,
        price_per_kwh=to_money(req.price_per_kwh),
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)

    logger.info(
        f"CPO tariff created: '{tariff.name}' (id={tariff.id}, "
        f"{tariff.price_per_kwh}/kWh) by {user.email}"
    )

    return {
        "status": "created",
        "tariff_id": tariff.id,
        "name": tariff.name,
        "price_per_kwh": float(tariff.price_per_kwh),
    }


@router.put("/api/cpo/tariffs/{tariff_id}")
async def cpo_update_tariff(
    tariff_id: int,
    req: CpoTariffUpdateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a tariff's name and/or rate. Tenant-scoped — a CPO can only
    edit their own tenant's tariffs."""
    tariff = await _load_tenant_tariff(db, tariff_id, user.tenant_id)

    if req.name is not None:
        tariff.name = req.name
    if req.price_per_kwh is not None:
        tariff.price_per_kwh = to_money(req.price_per_kwh)

    await db.commit()
    await db.refresh(tariff)

    logger.info(f"CPO tariff updated: '{tariff.name}' (id={tariff.id}) by {user.email}")

    return {
        "status": "updated",
        "tariff_id": tariff.id,
        "name": tariff.name,
        "price_per_kwh": float(tariff.price_per_kwh),
    }


@router.delete("/api/cpo/tariffs/{tariff_id}")
async def cpo_delete_tariff(
    tariff_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a tariff. Any plug / charger group / tenant-default currently
    pointing at it falls back to the next link in the resolution chain (each
    FK is ON DELETE SET NULL — see models.py) rather than being left dangling.
    """
    tariff = await _load_tenant_tariff(db, tariff_id, user.tenant_id)

    tariff_name = tariff.name
    await db.delete(tariff)
    await db.commit()

    logger.info(f"CPO tariff deleted: '{tariff_name}' (id={tariff_id}) by {user.email}")

    return {"status": "deleted", "tariff_id": tariff_id, "name": tariff_name}


@router.put("/api/cpo/plugs/{plug_id}/tariff")
async def cpo_assign_plug_tariff(
    plug_id: int,
    req: CpoTariffAssignRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Assign (tariff_id set) or unassign (tariff_id null) the tariff a specific
    plug bills at. Both the plug and the tariff must belong to the caller's
    tenant — a CPO cannot attach another tenant's tariff to their own plug,
    nor reach a plug they don't own.
    """
    plug_result = await db.execute(
        select(Plug)
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(and_(Plug.id == plug_id, Gateway.tenant_id == user.tenant_id))
    )
    plug = plug_result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail="Plug not found or access denied.")

    if req.tariff_id is not None:
        await _load_tenant_tariff(db, req.tariff_id, user.tenant_id)

    plug.tariff_id = req.tariff_id
    await db.commit()

    logger.info(f"CPO plug tariff assignment: plug={plug_id} tariff={req.tariff_id} by {user.email}")

    return {"status": "updated", "plug_id": plug_id, "tariff_id": req.tariff_id}


@router.put("/api/cpo/groups/{group_id}/tariff")
async def cpo_assign_group_tariff(
    group_id: int,
    req: CpoTariffAssignRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Assign/unassign the tariff a charger group's plugs fall back to when
    they have no tariff of their own. Tenant-scoped on both the group and the
    tariff, same cross-tenant protection as the plug endpoint above."""
    group_result = await db.execute(
        select(ChargerGroup).where(
            and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
        )
    )
    group = group_result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found or access denied.")

    if req.tariff_id is not None:
        await _load_tenant_tariff(db, req.tariff_id, user.tenant_id)

    group.tariff_id = req.tariff_id
    await db.commit()

    logger.info(f"CPO group tariff assignment: group={group_id} tariff={req.tariff_id} by {user.email}")

    return {"status": "updated", "group_id": group_id, "tariff_id": req.tariff_id}


@router.put("/api/cpo/tenant/default-tariff")
async def cpo_assign_tenant_default_tariff(
    req: CpoTariffAssignRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Assign/unassign the CPO's tenant-wide default tariff — the last link
    in the resolution chain before the global COINS_PER_KWH env fallback."""
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    if req.tariff_id is not None:
        await _load_tenant_tariff(db, req.tariff_id, user.tenant_id)

    tenant.default_tariff_id = req.tariff_id
    await db.commit()

    logger.info(
        f"CPO tenant default tariff assignment: tenant={user.tenant_id} "
        f"tariff={req.tariff_id} by {user.email}"
    )

    return {"status": "updated", "tenant_id": user.tenant_id, "tariff_id": req.tariff_id}


# Wrap FastAPI app with Socket.io ASGI wrapper so they run on the same port
