"""
CPO Charger Group Management routes — split out of the original monolithic
cpo.py (2026-07-21 package split, TD#7 follow-up).
"""
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import get_db
from backend.database.models import ChargerGroup, GroupMembership, Plug, User
from backend.schemas import CpoGroupCreateRequest, CpoGroupUpdateRequest
from backend.services.audit import try_record_audit
from backend.services.rbac import require_role

from ._common import DEFAULT_LIST_LIMIT, _require_tenant_id, clamp_list_window, logger

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


# --- CPO Charger Group Management ---

@router.get("/api/cpo/groups")
async def cpo_list_groups(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
):
    """List all charger groups owned by the CPO's tenant."""
    limit, offset = clamp_list_window(limit, offset)
    result = await db.execute(
        select(ChargerGroup)
        .where(ChargerGroup.tenant_id == user.tenant_id)
        .order_by(ChargerGroup.id)
        .limit(limit)
        .offset(offset)
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

        # [Caps] The circuit limit + the current LIVE load, so the operator sees
        # "24 / 32 A in use". Sums each active plug's MEASURED current (from the
        # live telemetry snapshot) when available, else its configured cap — so
        # the display reflects real draw (measured amps run below the cap at
        # power factor < 1), while START-TIME admission still gates on configured
        # caps (services/caps.py). One query per group, matching the per-group
        # counts above (group counts are small).
        from backend.database.models import CapacityRequest
        from backend.services.caps import measured_circuit_load_a
        load_a = await measured_circuit_load_a(db, group.id, state.telemetry_store)
        # Drivers waiting on "Request capacity" for this circuit — a nudge to
        # raise the cap.
        waiting = (await db.execute(
            select(func.count(CapacityRequest.id)).where(CapacityRequest.group_id == group.id)
        )).scalar() or 0

        response.append({
            "id": group.id,
            "name": group.name,
            "is_public": group.is_public,
            "access_code": group.access_code if not group.is_public else None,
            "plug_count": plug_count,
            "member_count": member_count,
            "max_current_a": group.max_current_a,
            "current_load_a": load_a,
            "pending_capacity_requests": waiting,
            # [Pricing v2] Group-level tariff override (None = inherit tenant
            # default — see resolve_rate_for_plug()). CpoPricing's assignment
            # table/dropdown reads this.
            "tariff_id": group.tariff_id,
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
    # charger_groups.tenant_id is NOT NULL; a platform admin has no tenant, so
    # this used to reach the DB as NULL -> IntegrityError -> 500 (2026-08-18
    # audit). Fail with the same clean 400 the other tenant-scoped routes use.
    tenant_id = _require_tenant_id(user)

    access_code = None
    if not req.is_public:
        access_code = await generate_unique_access_code(db)

    group = ChargerGroup(
        tenant_id=tenant_id,
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

    if req.max_current_a is not None:
        # [Caps] 0 clears the circuit limit (no admission cap).
        group.max_current_a = req.max_current_a or None

    await db.commit()
    await db.refresh(group)

    # [Caps] Raising (or clearing) the circuit cap may make room for drivers
    # waiting on a "Request capacity" — notify any whose plug now fits. No-op if
    # the cap was lowered (nothing new fits). Best-effort; never raises.
    if req.max_current_a is not None:
        from backend.services.capacity import notify_capacity_available
        await notify_capacity_available(db, group.id)

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


@router.get("/api/cpo/groups/{group_id}/members")
async def cpo_list_group_members(
    group_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    [redesign/ui-v3 contract §4] Members of one of the CPO's private groups —
    the drivers who joined via its access code. Tenant-scoped like the other
    group routes (404 for another tenant's group, indistinguishable from
    "doesn't exist"). Returns a bare list per the contract shape:
    [{user_id, email, full_name, joined_at}], oldest joiner first.
    """
    group_result = await db.execute(
        select(ChargerGroup).where(
            and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
        )
    )
    if not group_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Group not found or access denied.")

    result = await db.execute(
        select(GroupMembership, User.email, User.full_name)
        .join(User, User.id == GroupMembership.user_id)
        .where(GroupMembership.group_id == group_id)
        .order_by(GroupMembership.joined_at.asc(), GroupMembership.id.asc())
    )
    return [
        {
            "user_id": m.user_id,
            "email": email,
            "full_name": full_name,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        }
        for m, email, full_name in result.all()
    ]


@router.delete("/api/cpo/groups/{group_id}/members/{user_id}")
async def cpo_remove_group_member(
    group_id: int,
    user_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    [redesign/ui-v3 contract §4] Remove a member from one of the CPO's private
    groups (revokes their access without rotating the code for everyone else).
    Tenant-scoped on the group; 404 if the user isn't a member. Audited
    (group.member_remove) — membership changes gate who may charge on private
    plugs, same taxonomy as access_code.regen.
    """
    group_result = await db.execute(
        select(ChargerGroup).where(
            and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
        )
    )
    group = group_result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found or access denied.")

    membership_result = await db.execute(
        select(GroupMembership).where(
            and_(GroupMembership.group_id == group_id, GroupMembership.user_id == user_id)
        )
    )
    membership = membership_result.scalar_one_or_none()
    if not membership:
        raise HTTPException(status_code=404, detail="That user is not a member of this group.")

    await db.delete(membership)
    await db.commit()

    logger.info(
        f"CPO group member removed: user={user_id} from group={group_id} "
        f"('{group.name}') by {user.email}"
    )

    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="group.member_remove",
        target_type="group",
        target_id=group_id,
        detail=f"user_id={user_id}, group={group.name}",
    )

    return {"status": "removed", "group_id": group_id, "user_id": user_id}
