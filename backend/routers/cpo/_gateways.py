"""
CPO Gateway Management routes — split out of the original monolithic cpo.py
(2026-07-21 package split, TD#7 follow-up).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import get_db
from backend.database.models import (
    FirmwareRelease,
    Gateway,
    GatewayStatus,
    Plug,
    User,
    UserRole,
)
from backend.schemas import (
    CpoGatewayClaimRequest,
    CpoGatewayCreateRequest,
    CpoGatewayOtaRequest,
)
from backend.services.audit import try_record_audit
from backend.services.rate_limit import (
    account_rate_limit_dependency,
    cpo_gateway_claim_account_rate_limiter,
)
from backend.services.rbac import require_role
from backend.services.session_lifecycle import gateway_is_live
from backend.services.versioning import version_sort_key

from ._common import DEFAULT_LIST_LIMIT, _require_tenant_id, clamp_list_window, logger

router = APIRouter()

# Generic response for EVERY claim failure (unknown code, already-claimed
# code, malformed code) — deliberately identical so a caller can never learn
# which of those it hit (no enumeration oracle). Same reasoning as
# routers/auth.py's forgot_password generic response.
_CLAIM_NOT_FOUND_DETAIL = "Claim code not found or already used."


def _normalize_claim_code(raw: str) -> str:
    """Buyer-friendly normalization: strip surrounding whitespace and the
    dashes/spaces someone might type between groups of characters (codes are
    printed as one unbroken string, but "copy the code off the label" typos
    are common), then uppercase — claim codes are minted uppercase (see
    routers/admin.py's inventory-mint helper)."""
    return raw.strip().upper().replace("-", "").replace(" ", "")


# --- CPO Gateway Management ---

@router.get("/api/cpo/gateways")
async def cpo_list_gateways(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
):
    """List all gateways owned by the CPO's tenant (newest first, windowed)."""
    limit, offset = clamp_list_window(limit, offset)
    result = await db.execute(
        select(Gateway)
        .where(Gateway.tenant_id == user.tenant_id)
        .order_by(Gateway.id)
        .limit(limit)
        .offset(offset)
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
            # Liveness-derived, not the raw DB flag: the stored status only
            # flips on a delivered LWT, so a crash outage (broker died with the
            # connection — no LWT) leaves it ONLINE forever, and the firmware's
            # retained `online` replay re-asserts it on every broker restart.
            # Same status+freshness rule the session-start gate uses.
            "status": "online" if gateway_is_live(gw) else "offline",
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
    # A platform admin has no tenant of its own — refuse rather than create a
    # phantom tenant-less gateway (2026-08-18 audit).
    tenant_id = _require_tenant_id(user)

    # Check for duplicate gateway ID
    existing = await db.execute(select(Gateway).where(Gateway.id == req.gateway_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Gateway '{req.gateway_id}' already exists.")

    # vpn_ip is a legacy overlay field (NOT NULL + UNIQUE) that direct-MQTT
    # gateways don't use. Fall back to the gateway_id so the unique constraint
    # is satisfied without forcing the operator to invent an overlay IP.
    gateway = Gateway(
        id=req.gateway_id,
        tenant_id=tenant_id,
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


@router.post(
    "/api/cpo/gateways/claim",
    dependencies=[
        Depends(account_rate_limit_dependency(cpo_gateway_claim_account_rate_limiter, "gateway claim"))
    ],
)
async def cpo_claim_gateway(
    req: CpoGatewayClaimRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Bind a preflashed, admin-minted unclaimed gateway (POST
    /api/admin/gateways/inventory) to the caller's tenant, replacing the
    "operator hand-registers every unit" flow above for preflashed hardware.

    Every failure mode — unknown code, a code that's already been claimed,
    or a malformed one — returns the SAME generic 404
    (_CLAIM_NOT_FOUND_DETAIL), and the lookup query always runs before any
    branch decision, so a caller can't distinguish "wrong code" from
    "someone already claimed this" by response shape or rough timing (same
    enumeration-safety reasoning as routers/auth.py's forgot_password).
    """
    # A platform admin has no tenant — claiming as one would stamp claimed_at
    # (burning the code permanently) while leaving the owner NULL. Checked
    # before the lookup: it depends only on the CALLER, never on the code, so
    # it adds no enumeration oracle (2026-08-18 audit).
    tenant_id = _require_tenant_id(user)

    code = _normalize_claim_code(req.claim_code)

    # Always query — even a blank/malformed code — rather than short-circuit
    # on an empty string, so "malformed" isn't a cheaper/faster path than
    # "wrong but well-formed" (claim_code is never empty on a real row, so
    # this simply finds nothing).
    result = await db.execute(select(Gateway).where(Gateway.claim_code == code))
    gateway = result.scalar_one_or_none()

    if gateway is None or gateway.claimed_at is not None:
        raise HTTPException(status_code=404, detail=_CLAIM_NOT_FOUND_DETAIL)

    gateway.tenant_id = tenant_id
    gateway.claimed_at = datetime.now(timezone.utc)
    if req.name:
        gateway.name = req.name
    await db.commit()
    await db.refresh(gateway)

    # Snapshot before the audit write — a failed audit commit rolls back and
    # expires every ORM instance in the session (see try_record_audit's
    # docstring), which would make a later gateway.id/name access raise.
    gw_id, gw_name, gw_vpn_ip = gateway.id, gateway.name, gateway.vpn_ip

    logger.info(f"Gateway claimed: {gw_id} ({gw_name}) by {user.email} (tenant {user.tenant_id})")

    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="gateway.claim",
        target_type="gateway",
        target_id=gw_id,
        detail=f"name={gw_name}, claim_code_tail=***{code[-4:] if len(code) >= 4 else code}",
    )

    return {
        "status": "claimed",
        "gateway_id": gw_id,
        "name": gw_name,
        "vpn_ip": gw_vpn_ip,
    }


@router.get("/api/cpo/firmware-releases")
async def cpo_list_firmware_releases(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Active firmware releases for the OTA version picker (feat/ota-version-
    picker), newest-version-first. The release catalog is global (not
    tenant-scoped — one firmware fleet, see FirmwareRelease), so this is the
    same list for every CPO; only `is_active` rows are returned (deactivated
    releases are admin-only, see GET /api/admin/firmware-releases).

    Ordering is semver-aware (services/versioning.version_sort_key), not a
    string sort: "2.10.0" must rank above "2.9.0".
    """
    rows = (
        await db.execute(select(FirmwareRelease).where(FirmwareRelease.is_active.is_(True)))
    ).scalars().all()
    rows = sorted(rows, key=lambda r: version_sort_key(r.version), reverse=True)

    return [
        {
            "id": r.id,
            "version": r.version,
            "url": r.url,
            "notes": r.notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/api/cpo/gateways/{gateway_id}/ota")
async def cpo_gateway_ota(
    gateway_id: str,
    req: CpoGatewayOtaRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger an OTA firmware update on one of the CPO's gateways.

    The image URL comes from either `req.release_id` (a registered firmware
    release — the version-picker flow, GET /api/cpo/firmware-releases) or
    `req.firmware_url` (a raw https URL, admin-only — see
    CpoGatewayOtaRequest's docstring; a non-admin caller gets 403 here).
    Either way the firmware downloads it into its passive OTA slot and
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

    release_version = None
    if req.firmware_url:
        # Custom-URL escape hatch — admin only (CpoGatewayOtaRequest can't
        # see the caller's role, so the restriction lives here).
        if user.role != UserRole.ADMIN:
            raise HTTPException(
                status_code=403,
                detail="Custom firmware URLs are restricted to admin users — pick a registered release instead.",
            )
        firmware_url = req.firmware_url
    else:
        release_result = await db.execute(
            select(FirmwareRelease).where(
                and_(FirmwareRelease.id == req.release_id, FirmwareRelease.is_active.is_(True))
            )
        )
        release = release_result.scalar_one_or_none()
        if release is None:
            raise HTTPException(
                status_code=404,
                detail="Firmware release not found or no longer active.",
            )
        firmware_url = release.url
        release_version = release.version

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

    published = state.mqtt_manager.send_gateway_ota(gateway_id, plug_id, firmware_url)
    if not published:
        raise HTTPException(
            status_code=502,
            detail="Failed to publish the OTA command to the broker.",
        )

    logger.info(
        f"OTA command published for gateway {gateway_id} "
        f"(url={firmware_url}, release={release_version}) by {user.email}"
    )
    return {
        "status": "ota_triggered",
        "gateway_id": gateway_id,
        "firmware_url": firmware_url,
        "release_version": release_version,
        "message": "OTA command published. Watch the gateway's alarms topic / status fw version.",
    }
