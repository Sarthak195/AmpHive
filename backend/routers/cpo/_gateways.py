"""
CPO Gateway Management routes — split out of the original monolithic cpo.py
(2026-07-21 package split, TD#7 follow-up).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import get_db
from backend.database.models import Gateway, GatewayStatus, Plug, User
from backend.schemas import CpoGatewayCreateRequest, CpoGatewayOtaRequest
from backend.services.audit import try_record_audit
from backend.services.rbac import require_role

from ._common import logger

router = APIRouter()


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
