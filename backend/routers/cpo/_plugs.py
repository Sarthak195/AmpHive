"""
CPO Plug Management routes — split out of the original monolithic cpo.py
(2026-07-21 package split, TD#7 follow-up).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import get_db
from backend.database.models import (
    ChargerGroup,
    ChargingSession,
    Gateway,
    Plug,
    PlugStatus,
    SessionStatus,
    User,
)
from backend.schemas import (
    CpoPlugCreateRequest,
    CpoPlugMaintenanceRequest,
    CpoPlugUpdateRequest,
)
from backend.services.audit import try_record_audit
from backend.services.plug_hours import (
    access_note_of,
    amenities_of,
    hours_of,
    max_minutes_of,
    normalize_amenities,
    normalize_hours,
)

from ._common import (
    DEFAULT_LIST_LIMIT,
    clamp_list_window,
    hide_for,
    logger,
    redact_for,
    require_cpo_tenant,
)

router = APIRouter()


# --- CPO Plug Management ---

@router.get("/api/cpo/plugs")
async def cpo_list_plugs(
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
):
    """List all plugs across the CPO's gateways with status and group info."""
    limit, offset = clamp_list_window(limit, offset)
    # Group name joined in — previously one ChargerGroup.name query per plug.
    result = await db.execute(
        select(Plug, ChargerGroup.name)
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .outerjoin(ChargerGroup, ChargerGroup.id == Plug.group_id)
        .where(Gateway.tenant_id == user.tenant_id)
        .order_by(Plug.id)
        .limit(limit)
        .offset(offset)
    )

    return [
        {
            "id": plug.id,
            "name": plug.name,
            "gateway_id": plug.gateway_id,
            # [F10] The plug's home-LAN address and its coordinates (a
            # private-group plug is not on the public map) are None for a
            # read-only cpo_viewer.
            "local_ip": hide_for(user, plug.local_ip),
            "plug_model": plug.plug_model,
            "status": plug.status.value,
            "current_power_w": plug.current_power_w,
            "group_id": plug.group_id,
            "group_name": group_name,
            # [Pricing v2] Per-plug tariff override (None = inherit group / tenant
            # default — see resolve_rate_for_plug()). CpoPricing's assignment
            # table/dropdown reads this.
            "tariff_id": plug.tariff_id,
            "latitude": hide_for(user, plug.latitude),
            "longitude": hide_for(user, plug.longitude),
            # [Caps] The per-plug current cap (None = default hardware cutoff).
            "max_current_a": plug.max_current_a,
            # [Discovery] Advertised specs shown to drivers — pre-fill the edit form.
            "rated_power_w": plug.rated_power_w,
            "connector_type": plug.connector_type,
            # [Queued charge] Per-plug overrides (None = inherit the tenant
            # default) — pre-fill the edit form.
            "queued_charging_enabled": plug.queued_charging_enabled,
            "auto_start_delay_min": plug.auto_start_delay_min,
            # [Plug hours] Pre-fill the edit form. The access note is free
            # text (it may name a gate code or a phone number), so it is
            # masked for a read-only cpo_viewer.
            "opening_hours": hours_of(plug),
            "max_session_minutes": max_minutes_of(plug),
            "amenities": amenities_of(plug),
            "access_note": redact_for(user, access_note_of(plug)),
            "last_seen_at": plug.last_seen_at.isoformat() if plug.last_seen_at else None,
            "created_at": plug.created_at.isoformat() if plug.created_at else None,
        }
        for plug, group_name in result.all()
    ]


async def _publish_gateway_roster(db: AsyncSession, gateway_id: str) -> None:
    """
    Republish the retained plug roster (amphive/gateways/{gw}/config) for a
    gateway after a plug create/update so the firmware reconciles its slot table
    (add / re-IP / remove). Built from the current request session, so it must
    run AFTER commit. No-op when the MQTT manager isn't up (tests / pre-startup).
    """
    if not state.mqtt_manager:
        return
    rows = (await db.execute(
        select(Plug.id, Plug.local_ip, Plug.max_current_a)
        .where(Plug.gateway_id == gateway_id)
    )).all()
    roster = [
        {"plug_id": pid, "local_ip": ip, "max_current_a": cap}
        for pid, ip, cap in rows
    ]
    state.mqtt_manager.publish_plug_roster(gateway_id, roster)


@router.post("/api/cpo/plugs")
async def cpo_create_plug(
    req: CpoPlugCreateRequest,
    user: User = Depends(require_cpo_tenant),
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
        rated_power_w=req.rated_power_w,
        connector_type=req.connector_type,
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

    # Retained roster push so the gateway starts tracking the new plug now,
    # without waiting for the first ON/OFF command.
    await _publish_gateway_roster(db, req.gateway_id)

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
    user: User = Depends(require_cpo_tenant),
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
    if req.local_ip is not None:
        plug.local_ip = req.local_ip
    if req.max_current_a is not None:
        # [Caps] 0 clears the cap back to the default hardware cutoff (NULL).
        plug.max_current_a = req.max_current_a or None
    if "queued_charging_enabled" in req.model_fields_set:
        # [Queued charge] Explicit per-plug on/off override of the tenant
        # default; an explicit JSON null clears it back to "inherit" (NULL),
        # while an omitted key leaves it unchanged.
        plug.queued_charging_enabled = req.queued_charging_enabled
    if "auto_start_delay_min" in req.model_fields_set:
        # [Queued charge] null or 0 clears the override back to the tenant
        # default (NULL); an omitted key leaves it unchanged.
        plug.auto_start_delay_min = req.auto_start_delay_min or None
    if req.rated_power_w is not None:
        # [Discovery] 0 clears the advertised spec back to unset (NULL) —
        # same convention as max_current_a above.
        plug.rated_power_w = req.rated_power_w or None
    if req.connector_type is not None:
        # [Discovery] Empty string clears it back to unset (NULL), the
        # string-field analogue of "0 clears" above.
        plug.connector_type = req.connector_type or None
    # [Plug hours] An explicit null (or empty) clears; an omitted key leaves
    # the field unchanged. Validation errors become a 400 before any write.
    fields = req.model_fields_set
    try:
        if "opening_hours" in fields:
            plug.opening_hours = normalize_hours(
                [w.model_dump() for w in req.opening_hours] if req.opening_hours else None
            )
        if "amenities" in fields:
            plug.amenities = normalize_amenities(req.amenities)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if "max_session_minutes" in fields:
        plug.max_session_minutes = req.max_session_minutes or None
    if "access_note" in fields:
        plug.access_note = (req.access_note or "").strip() or None

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

    # Retained roster push — picks up a changed local_ip / name / max_current_a
    # so the gateway re-IPs or re-caps the plug's slot.
    await _publish_gateway_roster(db, plug.gateway_id)

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
    user: User = Depends(require_cpo_tenant),
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

        # [Plug watches] `clear` just returned the plug to AVAILABLE — fan
        # out "notify me when free" to its watchers (one-shot; best-effort by
        # contract, never raises — services/plug_watch.py). Nobody excluded:
        # unlike a session end, no watcher caused this flip.
        if plug.status == PlugStatus.AVAILABLE:
            from backend.services.plug_watch import notify_watchers_plug_available
            await notify_watchers_plug_available(db, plug)

    return {
        "status": "updated",
        "plug_id": plug.id,
        "action": action,
        "plug_status": plug.status.value,
    }
