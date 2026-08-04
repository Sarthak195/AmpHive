"""
CPO Offline (cash) Top-up routes — POST /api/cpo/topups + GET /api/cpo/topups.

Let a CPO manually credit a driver's coin wallet for a payment collected
offline (cash), without creating coins out of thin air: the amount is capped
by the tenant's currently-available top-up pool (services/payouts.py
tenant_earnings_summary's available_pool_coins — unsettled net session
earnings since the settlement watermark, minus offline top-ups already
issued in that same window). See models.py OfflineTopup + services/payouts.py
module docstring's "Offline top-up pool" section for the full math, and
routers/cpo/_payouts.py cpo_request_payout for the other half: a bank payout
request pays out that same reduced figure so the same earnings are never
drawn out twice.

Modeled on the dispute-approve precedent (_disputes.py cpo_resolve_dispute):
row-lock, credit_wallet(), a driver-side LedgerTransaction, a try_record_audit
row, and a best-effort driver notification.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.database.db import get_db
from backend.database.models import (
    LedgerTransaction,
    OfflineTopup,
    Tenant,
    TransactionType,
    User,
    UserRole,
)
from backend.schemas import CpoTopupCreateRequest, CpoTopupResponse
from backend.services import payouts as payout_service
from backend.services.audit import try_record_audit
from backend.services.auth import normalize_email
from backend.services.money import ZERO_MONEY, to_money
from backend.services.rate_limit import (
    account_rate_limit_dependency,
    cpo_topup_account_rate_limiter,
)
from backend.services.rbac import require_role
from backend.services.wallet import credit_wallet

from ._common import _require_tenant_id, logger

router = APIRouter()


def _topup_response(
    topup: OfflineTopup, *, driver_email: Optional[str] = None, actor_email: Optional[str] = None
) -> CpoTopupResponse:
    return CpoTopupResponse(
        id=topup.id,
        tenant_id=topup.tenant_id,
        actor_user_id=topup.actor_user_id,
        actor_email=actor_email,
        driver_user_id=topup.driver_user_id,
        driver_email=driver_email,
        amount_coins=float(topup.amount_coins),
        note=topup.note,
        created_at=topup.created_at.isoformat() if topup.created_at else None,
    )


@router.post(
    "/api/cpo/topups",
    response_model=CpoTopupResponse,
    dependencies=[Depends(account_rate_limit_dependency(cpo_topup_account_rate_limiter, "offline top-up"))],
)
async def cpo_create_topup(
    req: CpoTopupCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Credit `driver_email`'s wallet by `amount_coins`, funded from this
    tenant's own available top-up pool. 404 if no driver account exists with
    that email; 409 (with the actual available figure) if the amount would
    exceed the pool.

    Race-safe: row-locks the Tenant first (same convention + same row as
    cpo_request_payout, so a concurrent top-up and payout request for this
    tenant serialize against each other, not just against their own kind),
    then row-locks the driver's User row before crediting.
    """
    tenant_id = _require_tenant_id(user)

    lock_result = await db.execute(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()
    )
    if lock_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    # Case-insensitive lookup, matching register/login (services/auth.py
    # normalize_email) so a differently-cased driver email still resolves.
    driver_result = await db.execute(
        select(User)
        .where(func.lower(User.email) == normalize_email(req.driver_email))
        .with_for_update()
    )
    driver = driver_result.scalar_one_or_none()
    if not driver or driver.role != UserRole.DRIVER:
        raise HTTPException(
            status_code=404,
            detail=f"No driver account found for '{req.driver_email}'.",
        )

    amount = to_money(req.amount_coins)
    if amount <= ZERO_MONEY:
        raise HTTPException(status_code=400, detail="amount_coins must be greater than 0.")

    summary = await payout_service.tenant_earnings_summary(db, tenant_id)
    available_pool = summary["available_pool_coins"]
    if amount > available_pool:
        raise HTTPException(
            status_code=409,
            detail=(
                f"That would exceed your available top-up pool "
                f"({available_pool} coins available)."
            ),
        )

    new_balance = await credit_wallet(db, driver.id, amount)
    if new_balance is None:
        raise HTTPException(
            status_code=404,
            detail=f"No driver account found for '{req.driver_email}'.",
        )

    topup = OfflineTopup(
        tenant_id=tenant_id,
        actor_user_id=user.id,
        driver_user_id=driver.id,
        amount_coins=amount,
        note=req.note,
    )
    db.add(topup)

    db.add(LedgerTransaction(
        user_id=driver.id,
        amount=amount,  # Positive = credit
        transaction_type=TransactionType.CPO_TOPUP,
        description=(
            f"Cash top-up credited by {user.email}" + (f": {req.note}" if req.note else "")
        ),
        balance_after=new_balance,
    ))

    await db.commit()
    await db.refresh(topup)

    logger.info(
        f"CPO offline top-up: tenant={tenant_id} topup={topup.id} "
        f"driver={driver.email} amount={amount} by {user.email}"
    )

    # Snapshot the response BEFORE the audit attempt — a failed audit write
    # rolls the session back and expires `topup` (see try_record_audit's
    # session-state caveat).
    response = _topup_response(topup, driver_email=driver.email, actor_email=user.email)

    await try_record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=user.id,
        action="topup.create",
        target_type="offline_topup",
        target_id=response.id,
        detail=f"driver={driver.email}, amount={amount}"
        + (f", note={req.note}" if req.note else ""),
    )

    # Best-effort driver notification (notify() never raises).
    from backend.services.notifications import notify
    await notify(
        driver.id,
        "topup_credited",
        "Wallet credited",
        f"{user.email} credited your wallet {amount} coins for a cash payment.",
        severity="info",
    )

    # Fire-and-forget receipt email — schedule() backgrounds the send so the
    # route never waits on SMTP.
    from backend.services import billing_emails
    billing_emails.schedule(billing_emails.send_topup_receipt(
        to_addr=driver.email,
        full_name=driver.full_name,
        amount_coins=float(amount),
        new_balance=float(new_balance),
        credited_by=user.email,
        note=req.note,
    ))

    return response


@router.get("/api/cpo/topups")
async def cpo_list_topups(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """
    This tenant's offline top-up history, newest first. [redesign/ui-v3
    contract §4] Paginated: {total, items} with the house limit/offset params
    (limit capped at 200).
    """
    tenant_id = _require_tenant_id(user)
    limit, offset = max(1, min(limit, 200)), max(0, offset)

    total = (
        await db.execute(
            select(func.count(OfflineTopup.id)).where(OfflineTopup.tenant_id == tenant_id)
        )
    ).scalar() or 0

    Driver = aliased(User)
    Actor = aliased(User)
    result = await db.execute(
        select(OfflineTopup, Driver.email, Actor.email)
        .outerjoin(Driver, Driver.id == OfflineTopup.driver_user_id)
        .outerjoin(Actor, Actor.id == OfflineTopup.actor_user_id)
        .where(OfflineTopup.tenant_id == tenant_id)
        .order_by(OfflineTopup.created_at.desc(), OfflineTopup.id.desc())
        .limit(limit)
        .offset(offset)
    )

    return {
        "total": int(total),
        "items": [
            _topup_response(t, driver_email=driver_email, actor_email=actor_email)
            for t, driver_email, actor_email in result.all()
        ],
    }
