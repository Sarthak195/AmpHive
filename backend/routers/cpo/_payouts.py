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
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    ChargingSession,
    Payout,
    PayoutStatus,
    Tenant,
    User,
    UserRole,
)
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
    from COMPLETED sessions, the platform's cut, and the net owed to the CPO.
    Unsettled gross is every FINISHED session not yet claimed by a payout
    (``settled_payout_id IS NULL`` — services/payouts.py's "Settlement
    marking (gross)"), computed read-only via `sum_unsettled_session_coins`
    so viewing the dashboard can never claim a session out from under a
    future payout. `period_start`/`period_end` below still reflect the
    tenant's settlement watermark, which POST /api/cpo/payouts still uses for
    its refund-clawback window and its own period bounds — see
    services/payouts.py for the full watermark definition. This is the
    read-only dashboard view; POST /api/cpo/payouts is what actually claims
    the unsettled sessions into a payout request.

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
    Snapshot the tenant's unsettled earnings into a new REQUESTED payout.
    "Unsettled" gross is now determined by row-ownership, not a time window:
    every FINISHED session with ``settled_payout_id IS NULL`` is claimed for
    this payout in one atomic UPDATE ... RETURNING
    (services/payouts.py's `mark_unsettled_sessions_and_sum_gross` — see the
    module docstring's "Settlement marking (gross)" section for the watermark
    race this closes). The payable net is still capped by the same available
    top-up pool GET /api/cpo/earnings shows (unsettled net minus offline
    top-ups already issued since the watermark — services/payouts.py's
    "Offline top-up pool"), so a CPO can never draw a bank payout AND a cash
    top-up from the same earnings. 400 if there's nothing left to settle
    (payable net <= 0 — including the case where top-ups already consumed all
    of it), 409 if a payout is already REQUESTED for this tenant (only one
    pending settlement at a time).

    Claim-then-check: the sessions are claimed (and a Payout row inserted)
    BEFORE the payable-net check, because the claim and the gross read are
    one inseparable statement. If the check then fails, the whole attempt is
    rolled back — both the claim and the Payout insert — so a 400 response
    never leaves an orphaned payout row or permanently-claimed sessions
    behind; the sessions go straight back to unsettled for the next request.

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

    watermark = await payout_service.tenant_settlement_watermark(db, tenant_id)
    now = datetime.now(timezone.utc)

    # Insert first (placeholder money columns) so the claiming UPDATE below
    # has a real payout id to stamp onto the sessions it claims. flush(), not
    # commit(): nothing is durable/visible to other transactions yet, so a
    # failed payable-net check below can still roll the whole attempt back.
    payout = Payout(
        tenant_id=tenant_id,
        period_start=watermark,
        period_end=now,
        gross_coins=ZERO_MONEY,
        platform_fee_coins=ZERO_MONEY,
        net_coins=ZERO_MONEY,
        status=PayoutStatus.REQUESTED,
        requested_by_user_id=user.id,
    )
    db.add(payout)
    await db.flush()

    # Claim every unsettled session for this payout and sum their post-refund
    # gross in one statement — services/payouts.py's "Settlement marking".
    gross = await payout_service.mark_unsettled_sessions_and_sum_gross(
        db, tenant_id, payout.id
    )
    fee, net = payout_service.compute_fee_and_net(gross)

    # Same top-up-pool deduction as GET /api/cpo/earnings (unchanged windowing
    # — services/payouts.py's "Offline top-up pool").
    topups = await payout_service.sum_offline_topups(
        db, tenant_id, window_start=watermark, window_end=now
    )
    payable_net = to_money(max(net - topups, ZERO_MONEY))
    if payable_net <= ZERO_MONEY:
        # Undo the claim AND the payout insert -- neither is committed yet,
        # so the claimed sessions simply go back to unsettled (settled_payout_id
        # reverts with the transaction) and no orphaned payout row survives.
        await db.rollback()
        detail = "No unsettled earnings to pay out."
        if topups > ZERO_MONEY:
            detail += f" ({topups} coins of this window were already issued as offline top-ups.)"
        raise HTTPException(status_code=400, detail=detail)

    payout.gross_coins = gross
    payout.platform_fee_coins = fee
    payout.net_coins = payable_net
    await db.commit()
    await db.refresh(payout)

    logger.info(
        f"CPO payout requested: tenant={tenant_id} payout={payout.id} "
        f"window=[{watermark.isoformat()}, {now.isoformat()}) "
        f"gross={gross} fee={fee} net={payable_net} "
        f"(raw_net={net}, topups_deducted={topups}) by {user.email}"
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
    Cancel a REQUESTED payout, freeing every session it had claimed for a
    future request: ``ChargingSession.settled_payout_id`` is reset back to
    NULL for every row claiming this payout's id (services/payouts.py's
    "Settlement marking (gross)"), the row-ownership replacement for the old
    "watermark excludes CANCELLED payouts" window-freeing behavior. The
    owning CPO or an admin may cancel; any other tenant's CPO gets 404 (not
    "403", so a cross-tenant caller can't distinguish "not yours" from
    "doesn't exist"). 409 if the payout isn't REQUESTED — same race-safe
    row-lock as mark_paid.
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

    # Free every session this payout had claimed -- they go back to
    # unsettled (settled_payout_id NULL) so the next request can claim them.
    await db.execute(
        update(ChargingSession)
        .where(ChargingSession.settled_payout_id == payout.id)
        .values(settled_payout_id=None)
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
