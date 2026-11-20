"""
CPO Charger Group Management routes — split out of the original monolithic
cpo.py (2026-07-21 package split, TD#7 follow-up).
"""
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import get_db
from backend.database.models import ChargerGroup, GroupInvite, GroupMembership, Plug, User
from backend.schemas import CpoGroupCreateRequest, CpoGroupUpdateRequest
from backend.services import group_invites
from backend.services.audit import try_record_audit
from backend.services.money import to_money

from ._common import (
    DEFAULT_LIST_LIMIT,
    _require_tenant_id,
    clamp_list_window,
    is_cpo_viewer,
    logger,
    redact_for,
    redact_name_for,
    require_cpo_tenant,
)

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
    user: User = Depends(require_cpo_tenant),
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
            # [F10] A private group's join code is a credential (it lets a
            # driver account charge on the group's plugs): never shown to a
            # read-only viewer.
            "access_code": (
                group.access_code
                if not group.is_public and not is_cpo_viewer(user)
                else None
            ),
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
    user: User = Depends(require_cpo_tenant),
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
    user: User = Depends(require_cpo_tenant),
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
    user: User = Depends(require_cpo_tenant),
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
    user: User = Depends(require_cpo_tenant),
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
            "email": redact_for(user, email),
            "full_name": redact_name_for(user, full_name),
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            # [C9] The member's own flat price on this group's plugs (null =
            # the normal tariff). A price, not personal data.
            "price_per_kwh_override": (
                float(m.price_per_kwh_override)
                if m.price_per_kwh_override is not None else None
            ),
        }
        for m, email, full_name in result.all()
    ]


@router.delete("/api/cpo/groups/{group_id}/members/{user_id}")
async def cpo_remove_group_member(
    group_id: int,
    user_id: int,
    user: User = Depends(require_cpo_tenant),
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


# --- [C9] Per-member price override + email invites ---

class MemberPriceRequest(BaseModel):
    """A member's flat coins-per-kWh price on this group's plugs; null
    clears it. Same bounds as a tariff's price_per_kwh. The key is REQUIRED
    (an explicit null is the only way to clear) and unknown keys are rejected,
    so an empty body or a misspelled key answers 422 instead of silently
    clearing the member's price."""
    model_config = ConfigDict(extra="forbid")
    price_per_kwh_override: Optional[float] = Field(..., gt=0, le=1000)


class GroupInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)


async def _own_group(db: AsyncSession, group_id: int, user: User) -> ChargerGroup:
    group = (
        await db.execute(
            select(ChargerGroup).where(
                and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
            )
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found or access denied.")
    return group


@router.put("/api/cpo/groups/{group_id}/members/{user_id}/price")
async def cpo_set_member_price(
    group_id: int,
    user_id: int,
    req: MemberPriceRequest,
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    [C9] Set or clear one member's flat price on this private group's plugs.
    It beats the plug, group and tenant tariffs for that member only and is
    frozen onto a session at its start, so a change here never moves a
    session already running. Tenant-scoped (404 for another tenant's group or
    a non-member). Audited (group.member_price).
    """
    group = await _own_group(db, group_id, user)
    if group.is_public and req.price_per_kwh_override is not None:
        raise HTTPException(
            status_code=400, detail="Member prices apply to private groups only."
        )
    membership = (
        await db.execute(
            select(GroupMembership).where(
                and_(GroupMembership.group_id == group_id, GroupMembership.user_id == user_id)
            )
        )
    ).scalar_one_or_none()
    if not membership:
        raise HTTPException(status_code=404, detail="That user is not a member of this group.")

    old = membership.price_per_kwh_override
    new = to_money(req.price_per_kwh_override) if req.price_per_kwh_override is not None else None
    membership.price_per_kwh_override = new
    group_name = group.name
    await db.commit()

    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="group.member_price",
        target_type="group",
        target_id=group_id,
        detail=f"user_id={user_id}, group={group_name}, old={old}, new={new}",
    )
    return {
        "status": "updated",
        "group_id": group_id,
        "user_id": user_id,
        "price_per_kwh_override": float(new) if new is not None else None,
    }


@router.post("/api/cpo/groups/{group_id}/invites")
async def cpo_invite_to_group(
    group_id: int,
    req: GroupInviteRequest,
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    [C9] Email an invite to this private group (services/group_invites.py):
    digest-only, single-use, 7-day token bound to the address, capped at
    GROUP_INVITES_PER_DAY per CPO and GROUP_INVITES_GLOBAL_PER_DAY overall
    (429 past either), and only for a tenant that owns a claimed gateway (409
    otherwise). The response is the same whether or not the address has an
    account. Audited (group.invite) without the address: the invite row holds
    it, and account closure scrubs that row, which a retained audit line
    would not get.
    """
    group = await _own_group(db, group_id, user)
    if group.is_public:
        raise HTTPException(
            status_code=400, detail="Public groups need no invite: every driver can use them."
        )
    email = group_invites.normalize_invite_email(req.email)
    group_name = group.name
    invite, token = await group_invites.create_invite(db, user, group, email)
    invite_id, expires_at = invite.id, invite.expires_at
    group_invites.schedule_invite_email(email, group_name, token)

    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="group.invite",
        target_type="group",
        target_id=group_id,
        detail=f"group={group_name}, invite_id={invite_id}",
    )
    return {
        "status": "sent",
        "invite_id": invite_id,
        "group_id": group_id,
        "email": email,
        "expires_at": expires_at.isoformat(),
    }


@router.get("/api/cpo/groups/{group_id}/invites")
async def cpo_list_group_invites(
    group_id: int,
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    [C9] Pending (unaccepted, unrevoked, unexpired) invites for one of the
    CPO's groups, newest first, at most 100. Emails pass through redact_for,
    so a read-only cpo_viewer sees them masked.
    """
    await _own_group(db, group_id, user)
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(GroupInvite)
            .where(
                and_(
                    GroupInvite.group_id == group_id,
                    GroupInvite.accepted_at.is_(None),
                    GroupInvite.revoked_at.is_(None),
                    GroupInvite.expires_at > now,
                )
            )
            .order_by(GroupInvite.created_at.desc(), GroupInvite.id.desc())
            .limit(100)
        )
    ).scalars().all()
    return [
        {
            "id": inv.id,
            "email": redact_for(user, inv.email),
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
        }
        for inv in rows
    ]


@router.delete("/api/cpo/groups/{group_id}/invites/{invite_id}")
async def cpo_revoke_group_invite(
    group_id: int,
    invite_id: int,
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
):
    """[C9] Revoke a pending invite (its link stops working). 404 when it is
    not a pending invite of the caller's group. Audited (group.invite_revoke)."""
    await _own_group(db, group_id, user)
    revoked = await db.execute(
        update(GroupInvite)
        .where(
            and_(
                GroupInvite.id == invite_id,
                GroupInvite.group_id == group_id,
                GroupInvite.accepted_at.is_(None),
                GroupInvite.revoked_at.is_(None),
            )
        )
        .values(revoked_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if not getattr(revoked, "rowcount", 0):
        await db.rollback()
        raise HTTPException(status_code=404, detail="Invite not found or no longer pending.")
    await db.commit()
    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="group.invite_revoke",
        target_type="group",
        target_id=group_id,
        detail=f"invite_id={invite_id}",
    )
    return {"status": "revoked", "invite_id": invite_id}
