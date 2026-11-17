"""
CPO promo code routes: POST /api/cpo/promos (issue), GET /api/cpo/promos
(list with redemption counts), POST /api/cpo/promos/{promo_id}/disable.

A promo code is credit a driver claims for themselves (POST
/api/wallet/redeem in routers/payments.py), funded from this tenant's offline
top-up pool exactly like a cash top-up: each redemption writes an
OfflineTopup row through services/payouts.py issue_offline_topup. Nothing is
reserved when a code is issued; the pool is checked at redemption. See
models.PromoCode and services/promos.py for storage (hash only, plain code
shown once).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import PromoCode, PromoRedemption, User
from backend.schemas import PromoCodeCreateRequest, PromoCodeResponse
from backend.services import promos as promo_service
from backend.services.audit import try_record_audit
from backend.services.money import to_money

from ._common import _require_tenant_id, logger, require_cpo_tenant

router = APIRouter()


def _promo_response(
    promo: PromoCode,
    *,
    redemption_count: int,
    now: datetime,
    created_by_email: Optional[str] = None,
    code: Optional[str] = None,
) -> PromoCodeResponse:
    return PromoCodeResponse(
        id=promo.id,
        tenant_id=promo.tenant_id,
        code_hint=promo.code_hint,
        amount_coins=float(promo.amount_coins),
        expires_at=promo.expires_at.isoformat() if promo.expires_at else None,
        max_redemptions=promo.max_redemptions,
        per_user_limit=promo.per_user_limit,
        redemption_count=redemption_count,
        note=promo.note,
        created_by_email=created_by_email,
        created_at=promo.created_at.isoformat() if promo.created_at else None,
        disabled_at=promo.disabled_at.isoformat() if promo.disabled_at else None,
        status=promo_service.promo_status(
            disabled_at=promo.disabled_at,
            expires_at=promo.expires_at,
            redemption_count=redemption_count,
            max_redemptions=promo.max_redemptions,
            now=now,
        ),
        code=code,
    )


@router.post("/api/cpo/promos", response_model=PromoCodeResponse)
async def cpo_create_promo(
    req: PromoCodeCreateRequest,
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Issue a promo code. The plain code is in this response only."""
    tenant_id = _require_tenant_id(user)
    # Plain values: a rollback (collision retry, failed audit write) expires
    # ORM objects, `user` included.
    actor_id, actor_email = user.id, user.email
    now = datetime.now(timezone.utc)

    # 79 bits of randomness makes a digest collision academic; the retry just
    # turns one into a re-roll instead of a 500 (same idea as the claim-code
    # and access-code generators).
    for _ in range(5):
        code = promo_service.generate_code()
        promo = PromoCode(
            tenant_id=tenant_id,
            code_hash=promo_service.hash_code(code),
            code_hint=code[-promo_service.HINT_LEN:],
            amount_coins=to_money(req.amount_coins),
            expires_at=now + timedelta(days=req.expires_in_days) if req.expires_in_days else None,
            max_redemptions=req.max_redemptions,
            per_user_limit=req.per_user_limit,
            note=req.note,
            created_by=actor_id,
            created_at=now,
        )
        db.add(promo)
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
    else:
        raise HTTPException(status_code=500, detail="Could not generate a unique code; try again.")

    await db.refresh(promo)
    logger.info(f"CPO promo code issued: tenant={tenant_id} promo={promo.id} by {actor_email}")

    # Snapshot before the audit attempt (a failed audit write rolls back and
    # expires `promo`). The plain code never goes into the audit or the log.
    response = _promo_response(
        promo, redemption_count=0, now=now, created_by_email=actor_email,
        code=promo_service.format_code(code),
    )
    await try_record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_id,
        action="promo.create",
        target_type="promo_code",
        target_id=response.id,
        detail=(
            f"hint={response.code_hint}, amount={response.amount_coins}, "
            f"max={req.max_redemptions}, per_user={req.per_user_limit}, "
            f"expires={response.expires_at or 'never'}"
        ),
    )
    return response


@router.get("/api/cpo/promos")
async def cpo_list_promos(
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """This tenant's promo codes, newest first, each with its redemption
    count and status. Paginated {total, items} (limit capped at 200)."""
    tenant_id = _require_tenant_id(user)
    limit, offset = max(1, min(limit, 200)), max(0, offset)

    total = (
        await db.execute(
            select(func.count(PromoCode.id)).where(PromoCode.tenant_id == tenant_id)
        )
    ).scalar() or 0

    counts = (
        select(PromoRedemption.code_id, func.count(PromoRedemption.id).label("n"))
        .group_by(PromoRedemption.code_id)
        .subquery()
    )
    result = await db.execute(
        select(PromoCode, func.coalesce(counts.c.n, 0), User.email)
        .outerjoin(counts, counts.c.code_id == PromoCode.id)
        .outerjoin(User, User.id == PromoCode.created_by)
        .where(PromoCode.tenant_id == tenant_id)
        .order_by(PromoCode.created_at.desc(), PromoCode.id.desc())
        .limit(limit)
        .offset(offset)
    )
    now = datetime.now(timezone.utc)
    return {
        "total": int(total),
        "items": [
            _promo_response(p, redemption_count=int(n), now=now, created_by_email=email)
            for p, n, email in result.all()
        ],
    }


@router.post("/api/cpo/promos/{promo_id}/disable", response_model=PromoCodeResponse)
async def cpo_disable_promo(
    promo_id: int,
    user: User = Depends(require_cpo_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Stop a code from being redeemed. Idempotent; past redemptions stand."""
    tenant_id = _require_tenant_id(user)
    promo = (
        await db.execute(
            select(PromoCode)
            .where(PromoCode.id == promo_id, PromoCode.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if promo is None:
        raise HTTPException(status_code=404, detail="Promo code not found.")

    newly_disabled = promo.disabled_at is None
    if newly_disabled:
        promo.disabled_at = datetime.now(timezone.utc)
    count = (
        await db.execute(
            select(func.count(PromoRedemption.id)).where(PromoRedemption.code_id == promo.id)
        )
    ).scalar() or 0
    await db.commit()

    response = _promo_response(promo, redemption_count=int(count), now=datetime.now(timezone.utc))
    if newly_disabled:
        await try_record_audit(
            db,
            tenant_id=tenant_id,
            actor_user_id=user.id,
            action="promo.disable",
            target_type="promo_code",
            target_id=response.id,
            detail=f"hint={response.code_hint}, redemptions={count}",
        )
    return response
