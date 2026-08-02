"""
CPO Plug Problem Report routes (database/models.py PlugReport — driver-side
filing lives in backend/routers/plugs.py: POST /api/plugs/{plug_id}/report).
Mirrors _disputes.py's shape (list + resolve), minus the money/refund path —
a plug report carries no session and no coins, only a status lifecycle.
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import PlugReport, PlugReportStatus, User
from backend.schemas import CpoPlugReportResolveRequest, PlugReportResponse
from backend.services.rbac import require_role

from ._common import _plug_report_response, logger

router = APIRouter()


@router.get("/api/cpo/plug-reports", response_model=List[PlugReportResponse])
async def cpo_list_plug_reports(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
    limit: int = 100,
):
    """
    Problem reports filed against the CPO's own plugs. `tenant_id` is
    denormalized onto PlugReport at creation time (from the plug's gateway ->
    tenant chain), so this is a single indexed equality filter, no join —
    same convention as GatewayEvent/SessionDispute. Newest first; optional
    `status_filter` (open/acknowledged/resolved).
    """
    limit = max(1, min(limit, 500))
    conditions = [PlugReport.tenant_id == user.tenant_id]
    if status_filter:
        try:
            conditions.append(PlugReport.status == PlugReportStatus(status_filter))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status_filter '{status_filter}'. Valid: {[s.value for s in PlugReportStatus]}",
            )

    result = await db.execute(
        select(PlugReport)
        .where(and_(*conditions))
        .order_by(PlugReport.created_at.desc(), PlugReport.id.desc())
        .limit(limit)
    )
    reports = list(result.scalars().all())
    return [_plug_report_response(r) for r in reports]


@router.post("/api/cpo/plug-reports/{report_id}/resolve", response_model=PlugReportResponse)
async def cpo_resolve_plug_report(
    report_id: int,
    req: CpoPlugReportResolveRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Move a plug report through its lifecycle: open -> acknowledged ->
    resolved. Tenant-scoped (404 for a report belonging to another tenant).

    - "acknowledge": OPEN -> ACKNOWLEDGED ("seen, working on it" — mirrors
      acking a GatewayEvent). Only legal from OPEN.
    - "resolve": OPEN or ACKNOWLEDGED -> RESOLVED (stamps resolved_at /
      resolved_by_user_id). A report may be resolved directly from OPEN —
      acknowledging first is not required.

    No money ever moves here (contrast cpo_resolve_dispute) — this is a
    status transition only, with an optional operator note either way.

    Race safety: the row is locked (SELECT ... FOR UPDATE) and its status
    re-checked under the lock, so two concurrent resolutions of the *same*
    report serialize — the loser re-reads the now-committed row and finds it
    already past the state it expected (409), same pattern as
    cpo_resolve_dispute.
    """
    if req.action not in ("acknowledge", "resolve"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{req.action}'. Must be 'acknowledge' or 'resolve'.",
        )

    result = await db.execute(
        select(PlugReport)
        .where(and_(PlugReport.id == report_id, PlugReport.tenant_id == user.tenant_id))
        .with_for_update()
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Plug report not found or access denied.")

    if report.status == PlugReportStatus.RESOLVED:
        raise HTTPException(
            status_code=409,
            detail="This plug report was already resolved.",
        )

    if req.action == "acknowledge":
        if report.status != PlugReportStatus.OPEN:
            raise HTTPException(
                status_code=409,
                detail=f"This plug report is already {report.status.value}.",
            )
        report.status = PlugReportStatus.ACKNOWLEDGED
        if req.note:
            report.resolution_note = req.note
        await db.commit()
        await db.refresh(report)
        logger.info(f"Plug report {report.id} acknowledged by {user.email}")
        return _plug_report_response(report)

    # action == "resolve" — legal from OPEN or ACKNOWLEDGED ------------------
    now = datetime.now(timezone.utc)
    report.status = PlugReportStatus.RESOLVED
    report.resolved_at = now
    report.resolved_by_user_id = user.id
    if req.note:
        report.resolution_note = req.note

    await db.commit()
    await db.refresh(report)
    logger.info(f"Plug report {report.id} resolved by {user.email}")
    return _plug_report_response(report)
