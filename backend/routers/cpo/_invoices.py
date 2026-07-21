"""
CPO GST Tax Invoice routes — split out of the original monolithic cpo.py
(2026-07-21 package split, TD#7 follow-up). Issuance itself happens on the
driver side (GET /api/sessions/{id}/invoice, routers/sessions.py) — this is
the CPO-side read-only list of what's already been issued for the tenant.
"""
import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import Invoice, User
from backend.services.invoices import invoice_to_dict
from backend.services.rbac import require_role

from ._common import _require_tenant_id

router = APIRouter()


@router.get("/api/cpo/invoices")
async def cpo_list_invoices(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """List the tenant's issued GST invoices, newest first. Tenant-scoped.

    [redesign/ui-v3 contract §4] Paginated: {total, items} with the house
    limit/offset params (limit capped at 200)."""
    tenant_id = _require_tenant_id(user)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    total = (
        await db.execute(
            select(func.count(Invoice.id)).where(Invoice.tenant_id == tenant_id)
        )
    ).scalar() or 0

    result = await db.execute(
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id)
        .order_by(Invoice.issued_at.desc(), Invoice.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [invoice_to_dict(inv) for inv in result.scalars().all()],
    }


@router.get("/api/cpo/invoices.csv")
async def cpo_export_invoices_csv(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    days: Optional[int] = None,
):
    """
    Export the tenant's issued GST invoices as CSV (accounting / spreadsheet
    import) — mirrors GET /api/cpo/analytics/sessions.csv: same auth, a
    downloadable `text/csv` attachment, capped at 10k rows to bound memory.
    `days` is optional (unlike sessions.csv): invoices are a legal ledger, so
    the default export is the full history; pass days=N to window it to
    invoices issued in the last N days.
    """
    tenant_id = _require_tenant_id(user)

    query = select(Invoice).where(Invoice.tenant_id == tenant_id)
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.where(Invoice.issued_at >= cutoff)
    query = query.order_by(Invoice.issued_at.desc(), Invoice.id.desc()).limit(10000)

    result = await db.execute(query)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "invoice_number", "issued_at", "session_id", "driver_user_id",
        "energy_kwh", "rate_coins_per_kwh", "amount_coins",
        "taxable_value_inr", "gst_rate_pct", "gst_amount_inr", "total_inr",
        "seller_legal_name", "seller_gstin",
    ])
    for inv in result.scalars().all():
        writer.writerow([
            inv.invoice_number,
            inv.issued_at.isoformat() if inv.issued_at else "",
            inv.session_id,
            inv.driver_user_id,
            round(inv.energy_kwh, 3),
            round(float(inv.rate_coins_per_kwh), 2),
            round(float(inv.amount_coins), 2),
            round(float(inv.taxable_value_inr), 2),
            round(float(inv.gst_rate_pct), 2),
            round(float(inv.gst_amount_inr), 2),
            round(float(inv.total_inr), 2),
            inv.seller_legal_name or "",
            inv.seller_gstin or "",
        ])

    filename = f"amphive-invoices-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
