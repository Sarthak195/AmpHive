"""
Shared helpers for the backend.routers.cpo package: the tenant-scope guard
used by payouts/invoices/reservations, the cross-tenant tariff loader used by
the tariff-assignment endpoints, and the response-shape builders reused by
more than one route in their domain. Split out of the original monolithic
cpo.py (2026-07-21 package split, TD#7 follow-up) so each domain submodule
doesn't need to import from a sibling.
"""
import logging

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Payout, SessionDispute, Tariff, TariffSlot, User
from backend.schemas import DisputeResponse

logger = logging.getLogger("amphive.api")


def _require_tenant_id(user: User) -> int:
    """Every /api/cpo/payouts* and /api/cpo/earnings route is tenant-scoped.
    A 'cpo' always has a tenant_id (set by cpo_setup); a bare 'admin' with no
    tenant attached hits this — same shape as cpo_setup's own checks."""
    if user.tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail="You are not associated with a tenant.",
        )
    return user.tenant_id


async def _load_tenant_tariff(db: AsyncSession, tariff_id: int, tenant_id: int) -> Tariff:
    """Fetch a tariff, enforcing it belongs to `tenant_id`. Shared by the
    three assignment endpoints below so cross-tenant assignment (attaching
    another tenant's tariff to your plug/group/tenant-default) is rejected
    the same way everywhere."""
    result = await db.execute(
        select(Tariff).where(and_(Tariff.id == tariff_id, Tariff.tenant_id == tenant_id))
    )
    tariff = result.scalar_one_or_none()
    if not tariff:
        raise HTTPException(
            status_code=404,
            detail="Tariff not found or does not belong to your organization.",
        )
    return tariff


def _payout_response(payout: Payout) -> dict:
    return {
        "id": payout.id,
        "tenant_id": payout.tenant_id,
        "period_start": payout.period_start.isoformat() if payout.period_start else None,
        "period_end": payout.period_end.isoformat() if payout.period_end else None,
        "gross_coins": float(payout.gross_coins),
        "platform_fee_coins": float(payout.platform_fee_coins),
        "net_coins": float(payout.net_coins),
        "status": payout.status.value,
        "requested_by_user_id": payout.requested_by_user_id,
        "requested_at": payout.requested_at.isoformat() if payout.requested_at else None,
        "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
        "note": payout.note,
    }


def _dispute_response(dispute: SessionDispute) -> DisputeResponse:
    return DisputeResponse(
        id=dispute.id,
        session_id=dispute.session_id,
        tenant_id=dispute.tenant_id,
        driver_user_id=dispute.driver_user_id,
        reason=dispute.reason,
        status=dispute.status.value,
        resolution_note=dispute.resolution_note,
        refund_coins=float(dispute.refund_coins) if dispute.refund_coins is not None else None,
        created_at=dispute.created_at.isoformat() if dispute.created_at else None,
        resolved_at=dispute.resolved_at.isoformat() if dispute.resolved_at else None,
        resolved_by_user_id=dispute.resolved_by_user_id,
    )


def _fmt_min(m: int) -> str:
    """A minute-of-day as HH:MM (1440 -> 24:00) for operator-facing messages."""
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def _slot_dict(s: TariffSlot) -> dict:
    return {
        "id": s.id,
        "tariff_id": s.tariff_id,
        "start_min": s.start_min,
        "end_min": s.end_min,
        "price_per_kwh": float(s.price_per_kwh),
        "days_mask": s.days_mask,
    }
