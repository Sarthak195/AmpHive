"""
CPO Tariff (per-CPO/per-site pricing) Management routes, including
time-of-day slots and plug/group/tenant-default assignment — split out of
the original monolithic cpo.py (2026-07-21 package split, TD#7 follow-up).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    ChargerGroup,
    Gateway,
    Plug,
    Tariff,
    TariffSlot,
    Tenant,
    User,
)
from backend.schemas import (
    CpoTariffAssignRequest,
    CpoTariffCreateRequest,
    CpoTariffSlotCreateRequest,
    CpoTariffSlotUpdateRequest,
    CpoTariffUpdateRequest,
)
from backend.services.money import to_money
from backend.services.pricing import mark_tenant_sessions_for_reprice, slot_overlaps
from backend.services.rbac import require_role

from ._common import _fmt_min, _load_tenant_tariff, _require_tenant_id, _slot_dict, logger

router = APIRouter()


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
    # tariffs.tenant_id is NOT NULL — same bare-admin guard as group create.
    tenant_id = _require_tenant_id(user)

    tariff = Tariff(
        tenant_id=tenant_id,
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

    # [Pricing v2 Phase 3] Reprice in-flight sessions forward-only if the rate
    # moved (a rename alone is a no-op reprice, so this is safe to always call).
    if req.price_per_kwh is not None:
        await mark_tenant_sessions_for_reprice(db, user.tenant_id)
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
    # [Pricing v2 Phase 3] Sessions billing on this tariff fall back to the next
    # rule (its FKs are ON DELETE SET NULL) — reprice them forward-only.
    await mark_tenant_sessions_for_reprice(db, user.tenant_id)
    await db.commit()

    logger.info(f"CPO tariff deleted: '{tariff_name}' (id={tariff_id}) by {user.email}")

    return {"status": "deleted", "tariff_id": tariff_id, "name": tariff_name}


# --- Time-of-day slots on a tariff (Pricing v2 Phase 4) --------------------

async def _load_tenant_slot(db: AsyncSession, tariff_id: int, slot_id: int) -> TariffSlot:
    """Load a slot, enforcing it belongs to `tariff_id` (whose tenant the caller
    already proved via _load_tenant_tariff)."""
    result = await db.execute(
        select(TariffSlot).where(
            and_(TariffSlot.id == slot_id, TariffSlot.tariff_id == tariff_id)
        )
    )
    slot = result.scalar_one_or_none()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found on this tariff.")
    return slot


async def _validate_slot_window(db, tariff_id, start_min, end_min, days_mask, exclude_id):
    """Reject an invalid or overlapping window (400 / 409). A wrap-around window
    must be entered as two slots, so start<end is required; overlap is checked
    against the tariff's other slots via services/pricing.py slot_overlaps."""
    if start_min >= end_min:
        raise HTTPException(
            status_code=400,
            detail="Slot start must be before its end. Enter a wrap-around "
                   "window (e.g. 22:00–06:00) as two slots.",
        )
    existing = (
        await db.execute(select(TariffSlot).where(TariffSlot.tariff_id == tariff_id))
    ).scalars().all()
    for s in existing:
        if exclude_id is not None and s.id == exclude_id:
            continue
        if slot_overlaps(start_min, end_min, days_mask, s.start_min, s.end_min, s.days_mask):
            raise HTTPException(
                status_code=409,
                detail=f"That window overlaps an existing slot "
                       f"({_fmt_min(s.start_min)}–{_fmt_min(s.end_min)}).",
            )


@router.get("/api/cpo/tariffs/{tariff_id}/slots")
async def cpo_list_tariff_slots(
    tariff_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List a tariff's time-of-day slots (tenant-scoped via the parent tariff)."""
    await _load_tenant_tariff(db, tariff_id, user.tenant_id)
    result = await db.execute(
        select(TariffSlot)
        .where(TariffSlot.tariff_id == tariff_id)
        .order_by(TariffSlot.start_min)
    )
    return [_slot_dict(s) for s in result.scalars().all()]


@router.post("/api/cpo/tariffs/{tariff_id}/slots")
async def cpo_create_tariff_slot(
    tariff_id: int,
    req: CpoTariffSlotCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Add a TOD slot to a tariff. Validates the window + non-overlap first."""
    await _load_tenant_tariff(db, tariff_id, user.tenant_id)
    await _validate_slot_window(
        db, tariff_id, req.start_min, req.end_min, req.days_mask, exclude_id=None
    )
    slot = TariffSlot(
        tariff_id=tariff_id,
        start_min=req.start_min,
        end_min=req.end_min,
        price_per_kwh=to_money(req.price_per_kwh),
        days_mask=req.days_mask,
    )
    db.add(slot)
    await mark_tenant_sessions_for_reprice(db, user.tenant_id)  # [Phase 3]
    await db.commit()
    await db.refresh(slot)
    logger.info(
        f"CPO tariff slot created on tariff {tariff_id} "
        f"({_fmt_min(slot.start_min)}–{_fmt_min(slot.end_min)} @ {slot.price_per_kwh}) "
        f"by {user.email}"
    )
    return {"status": "created", **_slot_dict(slot)}


@router.put("/api/cpo/tariffs/{tariff_id}/slots/{slot_id}")
async def cpo_update_tariff_slot(
    tariff_id: int,
    slot_id: int,
    req: CpoTariffSlotUpdateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a slot's window / rate / days. Re-validates the resulting window
    (omitted fields keep their current value)."""
    await _load_tenant_tariff(db, tariff_id, user.tenant_id)
    slot = await _load_tenant_slot(db, tariff_id, slot_id)

    new_start = req.start_min if req.start_min is not None else slot.start_min
    new_end = req.end_min if req.end_min is not None else slot.end_min
    new_days = req.days_mask if req.days_mask is not None else slot.days_mask
    await _validate_slot_window(db, tariff_id, new_start, new_end, new_days, exclude_id=slot_id)

    slot.start_min = new_start
    slot.end_min = new_end
    slot.days_mask = new_days
    if req.price_per_kwh is not None:
        slot.price_per_kwh = to_money(req.price_per_kwh)
    await mark_tenant_sessions_for_reprice(db, user.tenant_id)  # [Phase 3]
    await db.commit()
    await db.refresh(slot)
    logger.info(f"CPO tariff slot {slot_id} updated on tariff {tariff_id} by {user.email}")
    return {"status": "updated", **_slot_dict(slot)}


@router.delete("/api/cpo/tariffs/{tariff_id}/slots/{slot_id}")
async def cpo_delete_tariff_slot(
    tariff_id: int,
    slot_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a TOD slot. The parent tariff's flat price then applies to that
    window again (slots are a strict refinement)."""
    await _load_tenant_tariff(db, tariff_id, user.tenant_id)
    slot = await _load_tenant_slot(db, tariff_id, slot_id)
    await db.delete(slot)
    await mark_tenant_sessions_for_reprice(db, user.tenant_id)  # [Phase 3]
    await db.commit()
    logger.info(f"CPO tariff slot {slot_id} deleted from tariff {tariff_id} by {user.email}")
    return {"status": "deleted", "slot_id": slot_id, "tariff_id": tariff_id}


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
