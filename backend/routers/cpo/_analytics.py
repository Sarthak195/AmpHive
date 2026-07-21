"""
CPO Analytics routes — split out of the original monolithic cpo.py
(2026-07-21 package split, TD#7 follow-up).
"""
import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import Date, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    ChargingSession,
    Gateway,
    GatewayStatus,
    Plug,
    SessionStatus,
    TelemetryReading,
    User,
)
from backend.services.rbac import require_role

router = APIRouter()


# --- CPO Analytics ---

@router.get("/api/cpo/analytics/overview")
async def cpo_analytics_overview(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Summary analytics for the CPO dashboard:
    - Total plugs, active sessions, gateways online/offline
    - Today's energy consumption and revenue
    - All-time totals
    """
    tenant_id = user.tenant_id

    # Total plugs count
    plug_count_result = await db.execute(
        select(func.count(Plug.id))
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(Gateway.tenant_id == tenant_id)
    )
    total_plugs = plug_count_result.scalar() or 0

    # Active sessions right now
    active_sessions_result = await db.execute(
        select(func.count(ChargingSession.id))
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.status == SessionStatus.ACTIVE,
        ))
    )
    active_sessions = active_sessions_result.scalar() or 0

    # Gateways online/offline
    gw_online_result = await db.execute(
        select(func.count(Gateway.id))
        .where(and_(Gateway.tenant_id == tenant_id, Gateway.status == GatewayStatus.ONLINE))
    )
    gateways_online = gw_online_result.scalar() or 0

    gw_total_result = await db.execute(
        select(func.count(Gateway.id)).where(Gateway.tenant_id == tenant_id)
    )
    gateways_total = gw_total_result.scalar() or 0

    # Today's stats (energy and revenue from completed sessions started today)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_stats_result = await db.execute(
        select(
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            func.count(ChargingSession.id),
        )
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.started_at >= today_start,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
    )
    today_row = today_stats_result.first()
    today_energy = float(today_row[0]) if today_row else 0.0
    today_revenue = float(today_row[1]) if today_row else 0.0
    today_sessions = int(today_row[2]) if today_row else 0

    # All-time stats
    alltime_result = await db.execute(
        select(
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            func.count(ChargingSession.id),
        )
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
    )
    alltime_row = alltime_result.first()
    alltime_energy = float(alltime_row[0]) if alltime_row else 0.0
    alltime_revenue = float(alltime_row[1]) if alltime_row else 0.0
    alltime_sessions = int(alltime_row[2]) if alltime_row else 0

    return {
        "plugs": {"total": total_plugs},
        "gateways": {"online": gateways_online, "total": gateways_total},
        "active_sessions": active_sessions,
        "today": {
            "sessions": today_sessions,
            "energy_kwh": round(today_energy, 3),
            "revenue_coins": round(today_revenue, 2),
        },
        "all_time": {
            "sessions": alltime_sessions,
            "energy_kwh": round(alltime_energy, 3),
            "revenue_coins": round(alltime_revenue, 2),
        },
    }


@router.get("/api/cpo/analytics/sessions")
async def cpo_analytics_sessions(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    plug_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    days: int = 30,
    limit: int = 50,
    offset: int = 0,
):
    """
    Session history across all of the CPO's plugs.
    Supports optional filters: plug_id, status, and date range (days).
    Sessions are ordered most recent first.

    [redesign/ui-v3 contract §4] Paginated: house limit/offset params (limit
    capped at 200) and `total` + `totals` {count, energy_kwh, revenue_coins}
    computed SERVER-SIDE over the full filtered set — the page slice never
    truncates the aggregates (the old client summed the ≤100 rows it got).
    Returns {total, totals, items, sessions} — `sessions` aliases `items` for
    callers written against the pre-contract bare-list shape.
    """
    limit, offset = max(1, min(limit, 200)), max(0, offset)

    # Shared filter set — applied identically to the aggregate and page
    # queries so the totals always describe exactly what's being paged.
    conditions = [ChargingSession.tenant_id == user.tenant_id]
    if plug_id:
        conditions.append(ChargingSession.plug_id == plug_id)
    if status_filter:
        try:
            conditions.append(ChargingSession.status == SessionStatus(status_filter))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status filter '{status_filter}'. Valid: {[s.value for s in SessionStatus]}",
            )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    conditions.append(ChargingSession.started_at >= cutoff)

    totals_row = (
        await db.execute(
            select(
                func.count(ChargingSession.id),
                func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
                func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            ).where(and_(*conditions))
        )
    ).first()
    total = int(totals_row[0]) if totals_row else 0
    totals = {
        "count": total,
        "energy_kwh": round(float(totals_row[1]), 3) if totals_row else 0.0,
        "revenue_coins": round(float(totals_row[2]), 2) if totals_row else 0.0,
    }

    # Page query: sessions belonging to this CPO's tenant, with plug name and
    # driver email joined in (previously two extra queries per session row).
    # Outer joins preserve the old fallback behavior for orphaned references.
    result = await db.execute(
        select(ChargingSession, Plug.name, User.email)
        .outerjoin(Plug, Plug.id == ChargingSession.plug_id)
        .outerjoin(User, User.id == ChargingSession.user_id)
        .where(and_(*conditions))
        .order_by(ChargingSession.started_at.desc(), ChargingSession.id.desc())
        .limit(limit)
        .offset(offset)
    )

    items = []
    for s, plug_name, user_email in result.all():
        plug_name = plug_name if plug_name is not None else f"Plug #{s.plug_id}"
        user_email = user_email if user_email is not None else "unknown"

        # Calculate duration
        duration_minutes = None
        if s.ended_at and s.started_at:
            duration_minutes = round((s.ended_at - s.started_at).total_seconds() / 60, 1)

        items.append({
            "id": s.id,
            "plug_id": s.plug_id,
            "plug_name": plug_name,
            "user_id": s.user_id,
            "user_email": user_email,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "duration_minutes": duration_minutes,
            "energy_kwh": round(s.energy_kwh, 3),
            "coins_spent": round(s.coins_spent, 2),
            "status": s.status.value,
        })

    return {"total": total, "totals": totals, "items": items, "sessions": items}


@router.get("/api/cpo/analytics/sessions.csv")
async def cpo_export_sessions_csv(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    plug_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    days: int = 30,
):
    """
    Export the CPO's session history as CSV (accounting / spreadsheet import).
    Same tenant scope and filters as `/api/cpo/analytics/sessions`, but returns
    a downloadable `text/csv` attachment. Capped at 10k rows to bound memory.
    """
    query = (
        select(ChargingSession, Plug.name, User.email)
        .outerjoin(Plug, Plug.id == ChargingSession.plug_id)
        .outerjoin(User, User.id == ChargingSession.user_id)
        .where(ChargingSession.tenant_id == user.tenant_id)
    )
    if plug_id:
        query = query.where(ChargingSession.plug_id == plug_id)
    if status_filter:
        try:
            query = query.where(ChargingSession.status == SessionStatus(status_filter))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status filter '{status_filter}'. Valid: {[s.value for s in SessionStatus]}",
            )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query = query.where(ChargingSession.started_at >= cutoff)
    query = query.order_by(ChargingSession.started_at.desc()).limit(10000)

    result = await db.execute(query)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "session_id", "plug_id", "plug_name", "user_email",
        "started_at", "ended_at", "duration_minutes", "energy_kwh",
        "coins_spent", "status",
    ])
    for s, plug_name, user_email in result.all():
        duration_minutes = ""
        if s.ended_at and s.started_at:
            duration_minutes = round((s.ended_at - s.started_at).total_seconds() / 60, 1)
        writer.writerow([
            s.id,
            s.plug_id,
            plug_name if plug_name is not None else f"Plug #{s.plug_id}",
            user_email if user_email is not None else "unknown",
            s.started_at.isoformat() if s.started_at else "",
            s.ended_at.isoformat() if s.ended_at else "",
            duration_minutes,
            round(s.energy_kwh, 3),
            round(float(s.coins_spent), 2),
            s.status.value,
        ])

    filename = f"amphive-sessions-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/cpo/analytics/revenue")
async def cpo_analytics_revenue(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    """
    Daily revenue breakdown for the CPO's charting dashboard.
    Returns an array of {date, revenue_coins, session_count} for each day
    in the requested range, suitable for plotting a revenue trend line.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Query: group completed sessions by date, sum revenue
    result = await db.execute(
        select(
            cast(ChargingSession.started_at, Date).label("date"),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0).label("revenue"),
            func.count(ChargingSession.id).label("count"),
        )
        .where(and_(
            ChargingSession.tenant_id == user.tenant_id,
            ChargingSession.started_at >= cutoff,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
        .group_by(cast(ChargingSession.started_at, Date))
        .order_by(cast(ChargingSession.started_at, Date))
    )

    rows = result.all()

    return [
        {
            "date": str(row[0]),
            "revenue_coins": round(float(row[1]), 2),
            "session_count": int(row[2]),
        }
        for row in rows
    ]


@router.get("/api/cpo/analytics/energy")
async def cpo_analytics_energy(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    """
    Daily energy consumption breakdown for the CPO's charting dashboard.
    Returns an array of {date, energy_kwh, session_count} for each day
    in the requested range, suitable for plotting an energy consumption chart.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(
            cast(ChargingSession.started_at, Date).label("date"),
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0).label("energy"),
            func.count(ChargingSession.id).label("count"),
        )
        .where(and_(
            ChargingSession.tenant_id == user.tenant_id,
            ChargingSession.started_at >= cutoff,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
        .group_by(cast(ChargingSession.started_at, Date))
        .order_by(cast(ChargingSession.started_at, Date))
    )

    rows = result.all()

    return [
        {
            "date": str(row[0]),
            "energy_kwh": round(float(row[1]), 3),
            "session_count": int(row[2]),
        }
        for row in rows
    ]


@router.get("/api/cpo/analytics/telemetry")
async def cpo_analytics_telemetry(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    plug_id: Optional[int] = None,
    days: int = 1,
    bucket: str = "hour",
):
    """
    Downsampled time-series telemetry for the CPO's load graphs / energy audits.

    Buckets raw `telemetry_readings` via date_trunc and returns average / peak
    power plus the cumulative energy reading per bucket. Tenant-scoped (uses the
    denormalized telemetry_readings.tenant_id); optional plug_id filter.

    Returns an array of
    {timestamp, avg_power_w, max_power_w, energy_kwh, sample_count}.
    """
    allowed_buckets = {"minute", "hour", "day"}
    if bucket not in allowed_buckets:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bucket '{bucket}'. Allowed: {sorted(allowed_buckets)}.",
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    bucket_col = func.date_trunc(bucket, TelemetryReading.recorded_at).label("bucket")

    conditions = [
        TelemetryReading.tenant_id == user.tenant_id,
        TelemetryReading.recorded_at >= cutoff,
    ]
    if plug_id is not None:
        conditions.append(TelemetryReading.plug_id == plug_id)

    result = await db.execute(
        select(
            bucket_col,
            func.avg(TelemetryReading.power_w).label("avg_power_w"),
            func.max(TelemetryReading.power_w).label("max_power_w"),
            # energy_kwh is cumulative-per-session, so max() = value at end of bucket
            func.max(TelemetryReading.energy_kwh).label("energy_kwh"),
            # Current (amps): avg + peak per bucket, so a CPO can see draw, not
            # just power. Persisted on every reading (derived power/voltage).
            func.avg(TelemetryReading.current_a).label("avg_current_a"),
            func.max(TelemetryReading.current_a).label("max_current_a"),
            func.count(TelemetryReading.id).label("sample_count"),
        )
        .where(and_(*conditions))
        .group_by(bucket_col)
        .order_by(bucket_col)
    )

    rows = result.all()

    return [
        {
            "timestamp": row[0].isoformat() if row[0] else None,
            "avg_power_w": round(float(row[1]), 1),
            "max_power_w": round(float(row[2]), 1),
            "energy_kwh": round(float(row[3]), 3),
            "avg_current_a": round(float(row[4]), 2),
            "max_current_a": round(float(row[5]), 2),
            "sample_count": int(row[6]),
        }
        for row in rows
    ]
