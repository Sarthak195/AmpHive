"""
CPO Gateway Events/Alerts + Admin Audit Trail routes — split out of the
original monolithic cpo.py (2026-07-21 package split, TD#7 follow-up).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import AuditLog, GatewayEvent, User
from backend.schemas import GatewayEventResponse
from backend.services.rbac import require_role

router = APIRouter()


# --- CPO Gateway Events / Alerts ---

@router.get("/api/cpo/events")
async def cpo_list_events(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    unacknowledged_only: bool = False,
    severity: Optional[str] = None,
):
    """
    Gateway/plug operational events for the CPO's tenant — safety cutoffs,
    unauthorized-on alarms, OTA lifecycle notices — newest first. Powers the
    operator alert feed. `unacknowledged_only` narrows to the active alert set;
    `severity` filters (e.g. "critical").

    [redesign/ui-v3 contract §4] Paginated: {total, items} with the house
    limit/offset params (limit capped at 200); `total` counts the full
    filtered set, not the page.
    """
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    conditions = [GatewayEvent.tenant_id == user.tenant_id]
    if unacknowledged_only:
        conditions.append(GatewayEvent.acknowledged == False)  # noqa: E712
    if severity:
        conditions.append(GatewayEvent.severity == severity)

    total = (
        await db.execute(
            select(func.count(GatewayEvent.id)).where(and_(*conditions))
        )
    ).scalar() or 0

    result = await db.execute(
        select(GatewayEvent)
        .where(and_(*conditions))
        .order_by(GatewayEvent.created_at.desc(), GatewayEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    events = list(result.scalars().all())

    return {
        "total": int(total),
        "items": [
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
        ],
    }


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
    status changes, access-code regeneration, and payout money ops
    (request / mark_paid / cancel) for the CPO's tenant, newest first.
    `limit` capped at 200 per page; `offset` paginates past it.
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
