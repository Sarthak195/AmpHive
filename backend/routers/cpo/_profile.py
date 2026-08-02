"""
CPO Setup & Profile routes — split out of the original monolithic cpo.py
(2026-07-21 package split, TD#7 follow-up). Onboarding (tenant creation) and
the CPO's own tenant-settings profile page.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import ChargerGroup, Gateway, Plug, Tenant, User, UserRole
from backend.schemas import CpoProfileUpdateRequest, CpoSetupRequest
from backend.services.auth import get_current_user
from backend.services.rate_limit import (
    account_rate_limit_dependency,
    cpo_setup_account_rate_limiter,
)
from backend.services.rbac import require_role

from ._common import logger

router = APIRouter()


# --- CPO Setup & Profile ---

@router.post(
    "/api/cpo/setup",
    dependencies=[
        Depends(account_rate_limit_dependency(cpo_setup_account_rate_limiter, "workspace setup"))
    ],
)
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
            # [Pricing v2] The wall-clock zone the operator's TOD slots are
            # interpreted in (services/pricing.py). Shown read-only on the
            # tariff editor so "peak 18:00–22:00" is unambiguous.
            "timezone": tenant.timezone,
            # [Queued charge] Tenant-level queued-charge defaults (per-plug
            # overrides live on the plug). Editable via PUT /api/cpo/profile.
            "queued_charging_enabled": tenant.queued_charging_enabled,
            "auto_start_delay_min": tenant.auto_start_delay_min,
            "queue_ttl_min": tenant.queue_ttl_min,
            # [GST invoices] Seller identity stamped onto issued invoices
            # (services/invoices.py); configured on the CPO Settings page.
            "gstin": tenant.gstin,
            "legal_name": tenant.legal_name,
            "invoice_prefix": tenant.invoice_prefix,
        },
        "stats": {
            "gateway_count": gateway_count,
            "plug_count": plug_count,
            "group_count": group_count,
        },
    }


@router.put("/api/cpo/profile")
async def cpo_update_profile(
    req: CpoProfileUpdateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    [Queued charge] Update the CPO's tenant-level settings — the queued-charge
    defaults every plug inherits unless it carries its own override. All fields
    optional; omitted ones are left unchanged.
    """
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    ).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    if req.queued_charging_enabled is not None:
        tenant.queued_charging_enabled = req.queued_charging_enabled
    if req.auto_start_delay_min is not None:
        tenant.auto_start_delay_min = req.auto_start_delay_min
    if req.queue_ttl_min is not None:
        tenant.queue_ttl_min = req.queue_ttl_min
    # [GST invoices] Seller identity for issued tax invoices. An empty/blank
    # string clears the field back to NULL (so an operator can un-set it); a
    # present value is stored trimmed.
    if req.gstin is not None:
        tenant.gstin = req.gstin.strip() or None
    if req.legal_name is not None:
        tenant.legal_name = req.legal_name.strip() or None
    if req.invoice_prefix is not None:
        tenant.invoice_prefix = req.invoice_prefix.strip() or None

    await db.commit()
    await db.refresh(tenant)

    logger.info(f"CPO tenant settings updated: {tenant.id} by {user.email}")

    return {
        "status": "updated",
        "queued_charging_enabled": tenant.queued_charging_enabled,
        "auto_start_delay_min": tenant.auto_start_delay_min,
        "queue_ttl_min": tenant.queue_ttl_min,
        "gstin": tenant.gstin,
        "legal_name": tenant.legal_name,
        "invoice_prefix": tenant.invoice_prefix,
    }
