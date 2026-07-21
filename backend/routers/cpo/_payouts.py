"""
CPO Payout / Settlement Ledger routes — split out of the original monolithic
cpo.py (2026-07-21 package split, TD#7 follow-up).

Manual settlement — RECORD-KEEPING ONLY. There is no bank/UPI/payment-
gateway integration: a CPO snapshots its unsettled coin earnings into a
REQUESTED payout, and the platform operator (admin) marks it PAID once the
transfer has happened out-of-band. See services/payouts.py for the
earnings/watermark math (requirements.md §5.1's "withdraw earnings" promise
— MARKET_GAP_ANALYSIS.md §1.6 / §7).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import Payout, PayoutStatus, Tenant, User, UserRole
from backend.services import payouts as payout_service
from backend.services.audit import try_record_audit
from backend.services.money import ZERO_MONEY, to_money
from backend.services.rbac import require_role

from ._common import _payout_response, _require_tenant_id, logger

router = APIRouter()


@router.get("/api/cpo/earnings")
async def cpo_earnings(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Lifetime + unsettled earnings for the CPO's tenant: gross coins collected
    from COMPLETED sessions, the platform's cut, and the net owed to the CPO
    — plus the settlement watermark (unsettled = watermark -> now). This is
    the read-only dashboard view; POST /api/cpo/payouts snapshots the
    unsettled leg into a payout request.

    `topup_pool` is the CPO offline-top-up feature's available balance:
    unsettled net earnings since the watermark, minus offline top-ups already
    issued in that same window (services/payouts.py's "Offline top-up pool"
    — POST /api/cpo/topups is capped at `topup_pool.available_coins`, and a
    later payout request pays out that same reduced figure, not the raw
    unsettled net, so the same earnings are never drawn out twice).
    """
    tenant_id = _require_tenant_id(user)
    summary = await payout_service.tenant_earnings_summary(db, tenant_id)

    return {
        "watermark": summary["watermark"].isoformat(),
        "as_of": summary["now"].isoformat(),
        "platform_fee_pct": float(payout_service.platform_fee_pct()),
        "lifetime": {
            "gross_coins": float(summary["lifetime_gross_coins"]),
            "platform_fee_coins": float(summary["lifetime_platform_fee_coins"]),
            "net_coins": float(summary["lifetime_net_coins"]),
        },
        "unsettled": {
            "period_start": summary["watermark"].isoformat(),
            "period_end": summary["now"].isoformat(),
            "gross_coins": float(summary["unsettled_gross_coins"]),
            "platform_fee_coins": float(summary["unsettled_platform_fee_coins"]),
            "net_coins": float(summary["unsettled_net_coins"]),
        },
        "topup_pool": {
            "available_coins": float(summary["available_pool_coins"]),
            "already_issued_coins": float(summary["topups_since_watermark_coins"]),
        },
    }


@router.post("/api/cpo/payouts")
async def cpo_request_payout(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Snapshot the tenant's unsettled earnings (watermark -> now) into a new
    REQUESTED payout. The payable net is capped by the same available top-up
    pool GET /api/cpo/earnings shows (unsettled net minus offline top-ups
    already issued since the watermark — services/payouts.py's "Offline
    top-up pool"), so a CPO can never draw a bank payout AND a cash top-up
    from the same earnings. 400 if there's nothing left to settle (payable
    net <= 0 — including the case where top-ups already consumed all of it),
    409 if a payout is already REQUESTED for this tenant (only one pending
    settlement at a time).

    Race-safe: row-locks the tenant for the duration of this transaction, so
    two concurrent requests — or a concurrent offline top-up, which locks the
    same tenant row (routers/cpo/_topups.py) — serialize: the loser re-reads
    the watermark/pool (and the now-committed REQUESTED row or OfflineTopup)
    after the winner commits. See services/payouts.py for the watermark
    definition.
    """
    tenant_id = _require_tenant_id(user)

    lock_result = await db.execute(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()
    )
    if lock_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    existing = await db.execute(
        select(Payout.id).where(
            and_(Payout.tenant_id == tenant_id, Payout.status == PayoutStatus.REQUESTED)
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="A payout request is already pending for this tenant.",
        )

    # Single source of truth for the earnings/pool math — same function GET
    # /api/cpo/earnings calls, so the two can never disagree.
    summary = await payout_service.tenant_earnings_summary(db, tenant_id)
    watermark = summary["watermark"]
    now = summary["now"]
    gross = summary["unsettled_gross_coins"]
    fee = summary["unsettled_platform_fee_coins"]
    topups = summary["topups_since_watermark_coins"]
    payable_net = summary["available_pool_coins"]  # unsettled net, minus top-ups already issued
    if payable_net <= ZERO_MONEY:
        detail = "No unsettled earnings to pay out."
        if topups > ZERO_MONEY:
            detail += f" ({topups} coins of this window were already issued as offline top-ups.)"
        raise HTTPException(status_code=400, detail=detail)

    payout = Payout(
        tenant_id=tenant_id,
        period_start=watermark,
        period_end=now,
        gross_coins=gross,
        platform_fee_coins=fee,
        net_coins=payable_net,
        status=PayoutStatus.REQUESTED,
        requested_by_user_id=user.id,
    )
    db.add(payout)
    await db.commit()
    await db.refresh(payout)

    logger.info(
        f"CPO payout requested: tenant={tenant_id} payout={payout.id} "
        f"window=[{watermark.isoformat()}, {now.isoformat()}) "
        f"gross={gross} fee={fee} net={payable_net} "
        f"(raw_net={to_money(gross - fee)}, topups_deducted={topups}) by {user.email}"
    )

    # Snapshot the response BEFORE the audit attempt: try_record_audit's
    # failure path rolls the session back, which expires `payout` — touching
    # an expired attribute on an AsyncSession afterwards raises
    # MissingGreenlet (see the session-state caveat on try_record_audit).
    response = _payout_response(payout)

    await try_record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=user.id,
        action="payout.request",
        target_type="payout",
        target_id=response["id"],
        detail=(
            f"window=[{watermark.isoformat()}, {now.isoformat()}), "
            f"gross={gross}, fee={fee}, net={payable_net}"
            + (f", topups_deducted={topups}" if topups > ZERO_MONEY else "")
        ),
    )

    return response


@router.get("/api/cpo/payouts")
async def cpo_list_payouts(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List the tenant's payouts, newest request first."""
    tenant_id = _require_tenant_id(user)
    result = await db.execute(
        select(Payout)
        .where(Payout.tenant_id == tenant_id)
        .order_by(Payout.requested_at.desc(), Payout.id.desc())
    )
    return [_payout_response(p) for p in result.scalars().all()]


@router.post("/api/cpo/payouts/{payout_id}/mark_paid")
async def cpo_mark_payout_paid(
    payout_id: int,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Platform operator marks a payout PAID after settling it manually
    out-of-band (bank transfer / UPI outside this app). Admin-only — a CPO
    cannot mark its own payout paid. 409 if the payout isn't REQUESTED
    (already PAID/CANCELLED, or a concurrent mark_paid/cancel got there
    first — the row lock below makes the state check itself race-safe).
    """
    result = await db.execute(
        select(Payout).where(Payout.id == payout_id).with_for_update()
    )
    payout = result.scalar_one_or_none()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout not found.")
    if payout.status != PayoutStatus.REQUESTED:
        raise HTTPException(
            status_code=409,
            detail=f"Payout is '{payout.status.value}', not 'requested'.",
        )

    payout.status = PayoutStatus.PAID
    payout.paid_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(payout)

    logger.info(f"CPO payout {payout.id} marked PAID by admin {user.email}")

    # Response snapshot BEFORE the audit attempt — a failed audit write rolls
    # the session back and expires `payout` (see try_record_audit's caveat).
    response = _payout_response(payout)

    # Audited into the PAYOUT's tenant (the admin actor has no tenant of
    # their own), so the CPO sees the settlement of its money in its own
    # GET /api/cpo/audit trail.
    await try_record_audit(
        db,
        tenant_id=response["tenant_id"],
        actor_user_id=user.id,
        action="payout.mark_paid",
        target_type="payout",
        target_id=response["id"],
        detail=(
            f"window=[{response['period_start']}, "
            f"{response['period_end']}), net={response['net_coins']:.2f}"
        ),
    )

    return response


@router.post("/api/cpo/payouts/{payout_id}/cancel")
async def cpo_cancel_payout(
    payout_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Cancel a REQUESTED payout, freeing its window for a future request (the
    watermark excludes CANCELLED payouts). The owning CPO or an admin may
    cancel; any other tenant's CPO gets 404 (not "403", so a cross-tenant
    caller can't distinguish "not yours" from "doesn't exist"). 409 if the
    payout isn't REQUESTED — same race-safe row-lock as mark_paid.
    """
    result = await db.execute(
        select(Payout).where(Payout.id == payout_id).with_for_update()
    )
    payout = result.scalar_one_or_none()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout not found.")
    if user.role != UserRole.ADMIN and payout.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Payout not found.")
    if payout.status != PayoutStatus.REQUESTED:
        raise HTTPException(
            status_code=409,
            detail=f"Payout is '{payout.status.value}', not 'requested'.",
        )

    payout.status = PayoutStatus.CANCELLED
    await db.commit()
    await db.refresh(payout)

    logger.info(f"CPO payout {payout.id} cancelled by {user.email}")

    # Response snapshot BEFORE the audit attempt — a failed audit write rolls
    # the session back and expires `payout` (see try_record_audit's caveat).
    response = _payout_response(payout)

    # The payout's tenant_id (not user.tenant_id): an admin canceller may have
    # no tenant — the row must land in the owning CPO's audit trail either way.
    await try_record_audit(
        db,
        tenant_id=response["tenant_id"],
        actor_user_id=user.id,
        action="payout.cancel",
        target_type="payout",
        target_id=response["id"],
        detail=(
            f"window=[{response['period_start']}, "
            f"{response['period_end']}), net={response['net_coins']:.2f}"
        ),
    )

    return response
