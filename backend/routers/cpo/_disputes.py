"""
CPO Session Dispute / Refund routes (coins-only — see database/models.py
SessionDispute and MARKET_GAP_ANALYSIS.md §3 "Refunds". Driver-side filing
lives in backend/routers/sessions.py: POST /api/sessions/{id}/dispute.)
Split out of the original monolithic cpo.py (2026-07-21 package split,
TD#7 follow-up).
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    ChargingSession,
    DisputeStatus,
    LedgerTransaction,
    SessionDispute,
    Tenant,
    TransactionType,
    User,
)
from backend.schemas import CpoDisputeResolveRequest, DisputeResponse
from backend.services.invoices import adjust_invoice_for_session_refund
from backend.services.money import ZERO_MONEY, to_money
from backend.services.rbac import require_role
from backend.services.wallet import credit_wallet

from ._common import _dispute_response, logger

router = APIRouter()


@router.get("/api/cpo/disputes", response_model=List[DisputeResponse])
async def cpo_list_disputes(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
    limit: int = 100,
):
    """
    Disputes filed against sessions on the CPO's own plugs. `tenant_id` is
    denormalized onto SessionDispute at creation time (from the session's
    plug -> gateway -> tenant chain), so this is a single indexed equality
    filter, no join — same convention as GatewayEvent/TelemetryReading.
    Newest first; optional `status_filter` (open/approved/rejected).
    """
    limit = max(1, min(limit, 500))
    conditions = [SessionDispute.tenant_id == user.tenant_id]
    if status_filter:
        try:
            conditions.append(SessionDispute.status == DisputeStatus(status_filter))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status_filter '{status_filter}'. Valid: {[s.value for s in DisputeStatus]}",
            )

    result = await db.execute(
        select(SessionDispute)
        .where(and_(*conditions))
        .order_by(SessionDispute.created_at.desc(), SessionDispute.id.desc())
        .limit(limit)
    )
    disputes = list(result.scalars().all())
    return [_dispute_response(d) for d in disputes]


@router.post("/api/cpo/disputes/{dispute_id}/resolve", response_model=DisputeResponse)
async def cpo_resolve_dispute(
    dispute_id: int,
    req: CpoDisputeResolveRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Approve or reject a dispute filed against one of the CPO's own sessions
    (tenant-scoped: 404 for a dispute belonging to another tenant).

    Approve: credits the driver's coin wallet (services/wallet.credit_wallet)
    and writes a REFUND LedgerTransaction referencing the session — coins
    only, there is no Razorpay money-out path. `refund_coins` defaults to
    the session's `coins_spent`; the cumulative APPROVED refund_coins for a
    session may never exceed that session's `coins_spent`. If the session
    already has an issued GST invoice, its totals are brought down in the
    same transaction to reflect the refund (services/invoices.py
    adjust_invoice_for_session_refund) — otherwise the tenant's invoice
    list/CSV export would keep reporting the pre-refund gross total forever.
    The driver-facing `session.coins_spent` itself is never touched by this
    — only the invoice's own money columns.

    Reject: status + resolution_note only, no money movement.

    Race safety:
    - The tenant row is locked FIRST (SELECT Tenant ... FOR UPDATE), the same
      row + statement cpo_request_payout (_payouts.py) and cpo_create_topup
      (_topups.py) lock before they compute the tenant's payable pool. Both of
      them read the approved-refund clawback (services/payouts.py
      sum_approved_refund_coins, windowed by SessionDispute.resolved_at)
      UNLOCKED, so without this an approve could commit its refund in the gap
      after a payout has locked the tenant but before it reads that sum: the
      refund's resolved_at lands before the payout's period_end, yet the next
      settlement window opens at that same period_end — so the refund would be
      dropped from this payout AND every future one, and the CPO's pool would
      never be reduced for a credit the driver already received (money leaking
      both sides, retryable at will). Holding this lock forces the resolve to
      serialize against any such payout/top-up for the same tenant.
    - The dispute row is then locked (SELECT ... FOR UPDATE) and its status
      re-checked under the lock, so two concurrent resolves of the *same*
      dispute serialize — the loser blocks on the lock, then (once it's
      free) re-reads the now-committed row and finds it's no longer OPEN
      (409). Same pattern as finalize_charging_session's session lock.
    - On approve, the *session* row is also locked. This serializes
      concurrent approvals of *two different* OPEN disputes on the *same*
      session, so the cumulative-refund read a few lines down is never
      computed against a picture a sibling in-flight approval is about to
      invalidate — the dispute-row lock alone only protects THIS dispute
      from a double-resolve, not siblings on the same session.

    The resulting global lock order is Tenant -> Dispute -> Session (-> User,
    inside credit_wallet). Every tenant-locking path in the codebase acquires
    the Tenant row first (payout, top-up, invoice issuance); none locks a
    Dispute/Session/User row and then a Tenant, so taking Tenant up front here
    introduces no AB-BA deadlock.
    """
    if req.action not in ("approve", "reject"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{req.action}'. Must be 'approve' or 'reject'.",
        )

    # Serialize this resolution against a concurrent payout / offline-top-up
    # pool computation for the SAME tenant (see the "Race safety" note above).
    # Taken BEFORE the dispute/session locks below so the global order stays
    # Tenant -> Dispute -> Session, matching the Tenant-first payout/top-up
    # path. The dispute is tenant-scoped to user.tenant_id (the WHERE just
    # below), so this locks the very tenant that owns it; the lock result isn't
    # inspected because the dispute lookup is the real gate — if the tenant (or
    # dispute) doesn't exist, that lookup 404s exactly as before. A bare admin
    # with no tenant matches no dispute and moves no money, so there is nothing
    # to serialize and nothing to lock.
    if user.tenant_id is not None:
        await db.execute(
            select(Tenant.id).where(Tenant.id == user.tenant_id).with_for_update()
        )

    result = await db.execute(
        select(SessionDispute)
        .where(and_(SessionDispute.id == dispute_id, SessionDispute.tenant_id == user.tenant_id))
        .with_for_update()
    )
    dispute = result.scalar_one_or_none()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found or access denied.")
    if dispute.status != DisputeStatus.OPEN:
        raise HTTPException(
            status_code=409,
            detail=f"This dispute was already resolved ({dispute.status.value}).",
        )

    # Common to both outcomes; a failed approve (raised below, before commit)
    # discards these along with everything else in the transaction — get_db()
    # closes (and thus rolls back) the session on the way out.
    now = datetime.now(timezone.utc)
    dispute.resolution_note = req.note
    dispute.resolved_at = now
    dispute.resolved_by_user_id = user.id

    if req.action == "reject":
        dispute.status = DisputeStatus.REJECTED
        await db.commit()
        await db.refresh(dispute)
        logger.info(f"Dispute {dispute.id} rejected by {user.email}")
        return _dispute_response(dispute)

    # action == "approve" ---------------------------------------------------
    session_result = await db.execute(
        select(ChargingSession).where(ChargingSession.id == dispute.session_id).with_for_update()
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="The disputed session no longer exists.")

    session_cost = to_money(session.coins_spent)
    if req.refund_coins is not None:
        refund_amount = to_money(req.refund_coins)
    elif session_cost > ZERO_MONEY:
        refund_amount = session_cost
    else:
        # No override given, and the session itself cost nothing (e.g. a
        # CANCELLED session that never billed) — the generic "must be > 0"
        # message below would be true but unhelpful here, so say why.
        raise HTTPException(
            status_code=400,
            detail="This session cost 0 coins, so there is nothing to refund by "
                   "default. Pass refund_coins explicitly if a refund is still warranted.",
        )
    if refund_amount <= ZERO_MONEY:
        # Reachable even on the explicit-refund_coins path: Field(gt=0)
        # only guards the raw float (e.g. 0.001 passes), but to_money's
        # 2dp rounding can still land exactly on 0.00.
        raise HTTPException(status_code=400, detail="refund_coins must be greater than 0.")

    already_refunded_result = await db.execute(
        select(func.coalesce(func.sum(SessionDispute.refund_coins), 0)).where(
            and_(
                SessionDispute.session_id == dispute.session_id,
                SessionDispute.status == DisputeStatus.APPROVED,
            )
        )
    )
    already_refunded = to_money(already_refunded_result.scalar_one())
    if already_refunded + refund_amount > session_cost:
        remaining = max(session_cost - already_refunded, ZERO_MONEY)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Refund of {refund_amount} coins would exceed the session's cost "
                f"({session_cost} coins, {remaining} remaining refundable)."
            ),
        )

    new_balance = await credit_wallet(db, dispute.driver_user_id, refund_amount)
    if new_balance is None:
        raise HTTPException(status_code=404, detail="The disputing driver's account no longer exists.")

    db.add(LedgerTransaction(
        user_id=dispute.driver_user_id,
        session_id=dispute.session_id,
        amount=refund_amount,  # Positive = credit
        transaction_type=TransactionType.REFUND,
        description=f"Dispute #{dispute.id} approved: refund for session {dispute.session_id}",
        balance_after=new_balance,
    ))

    # No-op if the session hasn't been invoiced yet — nothing "already
    # issued" to adjust. session_cost (session.coins_spent) is passed
    # read-only and is never itself mutated by this call.
    await adjust_invoice_for_session_refund(
        db, dispute.session_id, session_cost, already_refunded + refund_amount,
    )

    dispute.status = DisputeStatus.APPROVED
    dispute.refund_coins = refund_amount

    await db.commit()
    await db.refresh(dispute)

    logger.info(
        f"Dispute {dispute.id} approved by {user.email}: refunded {refund_amount} coins "
        f"to user {dispute.driver_user_id} (session {dispute.session_id})"
    )
    return _dispute_response(dispute)
