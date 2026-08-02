"""
Groups routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    ChargerGroup,
    GroupMembership,
    Plug,
    User,
)
from backend.schemas import (
    GroupResponse,
    JoinGroupRequest,
)
from backend.services.auth import (
    get_current_user,
)
from backend.services.rate_limit import (
    account_rate_limit_dependency,
    group_join_account_rate_limiter,
)

logger = logging.getLogger("amphive.api")
router = APIRouter()

# ===========================================================================
# Charger Group Endpoints
# ===========================================================================

@router.post(
    "/api/groups/join",
    dependencies=[
        Depends(account_rate_limit_dependency(group_join_account_rate_limiter, "group join"))
    ],
)
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
    limit: int = 200,
):
    """
    List all charger groups the current user has access to:
    - All public groups
    - All private groups the user has joined

    `limit` capped at 200 (same house pagination bound as the other list
    endpoints, see routers/admin.py) so a growing public-group count can't
    return an unbounded payload.
    """
    limit = max(1, min(limit, 200))

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
        .where(or_(ChargerGroup.is_public == True, GroupMembership.id.is_not(None)))  # noqa: E712 (SQL boolean, not Python)
        .group_by(ChargerGroup.id)
        .limit(limit)
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


@router.delete("/api/groups/{group_id}/leave")
async def leave_group(
    group_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Leave a group the caller previously joined — deletes their
    GroupMembership row (the join_group inverse; redesign/ui-v3 contract §4
    "Driver gaps"). 404 when the caller isn't a member: that covers a group
    that doesn't exist too, and public groups have no memberships to leave.
    Membership removal only — CPO-side group CRUD lives in routers/cpo.py.
    """
    result = await db.execute(
        select(GroupMembership).where(
            and_(
                GroupMembership.user_id == user.id,
                GroupMembership.group_id == group_id,
            )
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=404, detail="You are not a member of this group."
        )

    await db.delete(membership)
    await db.commit()
    logger.info(f"User {user.email} left group id={group_id}")

    return {"status": "left", "group_id": group_id}


