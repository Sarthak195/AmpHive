"""
Payments routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import asyncio
import json
import logging
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    LedgerTransaction,
    TransactionType,
    User,
)
from backend.schemas import (
    CreateOrderRequest,
    CreateOrderResponse,
    LedgerEntryResponse,
    VerifyPaymentRequest,
)
from backend.services import payments as payment_service
from backend.services.auth import (
    get_current_user,
)
from backend.services.money import to_money
from backend.services.rate_limit import (
    account_rate_limit_dependency,
    payments_create_order_account_rate_limiter,
)
from backend.services.wallet import available_balance, credit_wallet, debit_wallet_clamped

logger = logging.getLogger("amphive.api")
router = APIRouter()

# ===========================================================================
# Payment Endpoints (Razorpay)
# ===========================================================================

async def _already_credited(db: AsyncSession, razorpay_id: str) -> bool:
    """True if a ledger row already exists carrying this Razorpay id in the
    (unique) razorpay_payment_id column.

    Despite the column's name, this same slot doubles as the idempotency key
    for refund debits: TOPUP rows key it on the payment id ("pay_..."), REFUND
    rows created by `_debit_refund` key it on the refund id ("rfnd_..."). The
    two id spaces never collide (disjoint prefixes, and Razorpay ids are
    globally unique), so one column + one UNIQUE constraint safely dedupes
    both a redelivered payment.captured webhook and a redelivered refund
    webhook without a schema change.
    """
    existing = await db.execute(
        select(LedgerTransaction.id).where(
            LedgerTransaction.razorpay_payment_id == razorpay_id
        )
    )
    return existing.first() is not None


async def _credit_topup(
    db: AsyncSession,
    *,
    user_id: int,
    coins: float,
    payment_id: str,
    description: str,
) -> Optional[float]:
    """
    Atomically credit `coins` to a user and write the TOPUP ledger row keyed by
    `payment_id`. The UNIQUE constraint on razorpay_payment_id makes this
    idempotent even under a concurrent /verify + webhook race: the loser's
    INSERT raises IntegrityError, we roll back (undoing the balance bump), and
    return None so the caller reports an idempotent no-op.

    The balance bump happens DB-side (credit_wallet's atomic UPDATE), never as
    arithmetic on an ORM instance: on the /verify path the request session
    already holds the auth-loaded User in its identity map, and a re-select —
    even with_for_update — returns that cached instance with its stale
    balance, silently undoing any write committed since auth (lost update).

    Returns the new balance on success, or None if this payment was already
    credited by a concurrent request.
    """
    credit = to_money(coins)  # normalise the float from the payment service
    new_balance = await credit_wallet(db, user_id, credit)
    if new_balance is None:
        return None

    db.add(LedgerTransaction(
        user_id=user_id,
        amount=credit,  # Positive = credit
        transaction_type=TransactionType.TOPUP,
        description=description,
        razorpay_payment_id=payment_id,
        balance_after=new_balance,
    ))
    try:
        await db.commit()
    except IntegrityError:
        # Another request (webhook vs verify) already inserted this payment_id.
        await db.rollback()
        return None

    # Driver notification — matters most on the webhook path, where the credit
    # can land while the app is closed. Idempotent with the credit itself: the
    # duplicate-payment loser returned above and never reaches this.
    from backend.services.notifications import notify
    await notify(
        user_id,
        "topup_credited",
        "Charging credit added",
        f"₹{credit:.2f} added — your charging credit is now ₹{new_balance:.2f}.",
    )
    return new_balance


async def _topup_user_for_payment(db: AsyncSession, payment_id: str) -> Optional[int]:
    """The user_id credited by the original TOPUP ledger row for this
    razorpay payment_id, or None if we have no such row (e.g. a payment made
    before this feature existed, or one whose webhook we never attributed to
    a user — see extract_payment_from_webhook). A refund can only be clawed
    back from a wallet we actually credited."""
    result = await db.execute(
        select(LedgerTransaction.user_id).where(
            LedgerTransaction.razorpay_payment_id == payment_id
        )
    )
    return result.scalars().first()


async def _debit_refund(
    db: AsyncSession,
    *,
    user_id: int,
    coins: float,
    refund_id: str,
    payment_id: str,
    description: str,
) -> Optional[Decimal]:
    """
    Atomically debit `coins` from a user for a Razorpay refund and write a
    negative-amount REFUND ledger row keyed by `refund_id`.

    Idempotency mirrors `_credit_topup`: the UNIQUE constraint on
    razorpay_payment_id (reused here for the refund id — see `_already_credited`)
    makes a redelivered refund webhook a no-op — the loser's INSERT raises
    IntegrityError, we roll back (undoing the balance debit), and return None
    so the caller reports an idempotent no-op.

    Uses debit_wallet_clamped so a refund that arrives after the driver has
    already spent the topped-up coins can't push the balance negative; the
    ledger row records what was ACTUALLY clawed back, which may be less than
    the refunded amount if the balance couldn't cover it (logged as a
    warning — that shortfall is effectively forgiven, not tracked as debt).
    """
    debit = to_money(coins)  # normalise the float from the payment service
    actual_debit, new_balance = await debit_wallet_clamped(db, user_id, debit)
    if actual_debit < debit:
        logger.warning(
            f"Refund {refund_id} (payment {payment_id}): wallet balance could only "
            f"cover {actual_debit:.2f} of the {debit:.2f} coins owed back; "
            "shortfall forgiven."
        )

    db.add(LedgerTransaction(
        user_id=user_id,
        amount=-actual_debit,  # Negative = debit
        transaction_type=TransactionType.REFUND,
        description=description,
        razorpay_payment_id=refund_id,
        balance_after=new_balance,
    ))
    try:
        await db.commit()
    except IntegrityError:
        # Another request (a redelivered refund webhook) already inserted
        # this refund_id.
        await db.rollback()
        return None

    from backend.services.notifications import notify
    await notify(
        user_id,
        "topup_refunded",
        "Charging credit reversed",
        f"₹{actual_debit:.2f} was deducted after a refund — your charging credit is now ₹{new_balance:.2f}.",
    )
    return new_balance


@router.get("/api/wallet/ledger", response_model=List[LedgerEntryResponse])
async def get_wallet_ledger(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    """
    Unified wallet ledger for the current user: **both** top-up credits and
    session debits, newest first, each with the running `balance_after`. The
    driver History view previously showed only charging-session debits; this is
    the full money trail (`transaction_type` distinguishes them, `direction`
    is derived from the sign for convenient client rendering).

    [Auth holds] Each row also carries `available_balance` — the driver's
    CURRENT available balance (coin_balance minus what active sessions hold
    right now, services/wallet.py available_balance), computed once and
    repeated on every row. This is deliberately NOT a per-transaction
    historical figure like `balance_after` (which is the coin_balance
    snapshot immediately after that one transaction posted, forever fixed);
    it is "what you could spend right now," attached to the response rather
    than changing this endpoint's list-shaped body into an object, so
    existing list consumers (the frontend History ledger tab) are unaffected
    — additive field only, same convention as UserResponse.available_balance.
    """
    limit = max(1, min(limit, 500))
    result = await db.execute(
        select(LedgerTransaction)
        .where(LedgerTransaction.user_id == user.id)
        .order_by(LedgerTransaction.created_at.desc(), LedgerTransaction.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())

    current_available = float(await available_balance(db, user.id))

    return [
        LedgerEntryResponse(
            id=tx.id,
            amount=float(tx.amount),
            transaction_type=tx.transaction_type.value,
            direction="credit" if tx.amount >= 0 else "debit",
            description=tx.description,
            balance_after=float(tx.balance_after),
            session_id=tx.session_id,
            razorpay_payment_id=tx.razorpay_payment_id,
            created_at=tx.created_at.isoformat() if tx.created_at else None,
            available_balance=current_available,
        )
        for tx in rows
    ]


@router.post(
    "/api/payments/create-order",
    response_model=CreateOrderResponse,
    dependencies=[
        Depends(account_rate_limit_dependency(payments_create_order_account_rate_limiter, "payment order"))
    ],
)
async def create_payment_order(
    req: CreateOrderRequest,
    user: User = Depends(get_current_user),
):
    """
    Create a Razorpay order for a wallet top-up.
    The frontend will use the returned order_id to open the Razorpay Checkout modal.
    """
    # Validate amount (minimum ₹10, maximum ₹10,000)
    if req.amount_inr < 10:
        raise HTTPException(status_code=400, detail="Minimum top-up amount is ₹10.")
    if req.amount_inr > 10000:
        raise HTTPException(status_code=400, detail="Maximum top-up amount is ₹10,000.")

    order = await asyncio.to_thread(payment_service.create_order, req.amount_inr, user.id)
    if order is None:
        raise HTTPException(
            status_code=503,
            detail="Payment service is not configured. Contact support.",
        )

    return CreateOrderResponse(
        order_id=order["id"],
        amount=order["amount"],
        currency=order["currency"],
        key_id=payment_service.RAZORPAY_KEY_ID,
    )


@router.post("/api/payments/verify")
async def verify_payment(
    req: VerifyPaymentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Verify a Razorpay payment after the user completes checkout.
    If the signature is valid, credit coins to the user's wallet and
    create a ledger top-up transaction.
    """
    # Verify the payment signature (HMAC SHA256)
    is_valid = payment_service.verify_payment_signature(
        order_id=req.razorpay_order_id,
        payment_id=req.razorpay_payment_id,
        signature=req.razorpay_signature,
    )

    if not is_valid:
        raise HTTPException(status_code=400, detail="Payment verification failed. Invalid signature.")

    # --- Authoritative amount ----------------------------------------------
    # The checkout signature only proves (order_id, payment_id) are genuine —
    # it does NOT cover the amount, so the client-sent amount must never be
    # trusted. Fetch the payment from Razorpay and credit what was actually
    # paid. (razorpay SDK is sync/blocking → offload to a thread.)
    payment = await asyncio.to_thread(
        payment_service.fetch_captured_payment,
        req.razorpay_payment_id,
        req.razorpay_order_id,
    )
    if payment is None:
        raise HTTPException(
            status_code=502,
            detail="Could not confirm the payment with Razorpay. If you were "
                   "charged, your wallet will be credited automatically shortly.",
        )

    # Only credit money that has actually settled. Authorized-but-uncaptured
    # payments are credited by the webhook once capture happens.
    if payment["status"] != "captured":
        raise HTTPException(
            status_code=409,
            detail="Payment not captured yet. Your wallet will be credited "
                   "automatically once the payment settles.",
        )

    # The order's notes carry the user it was created for. Fail closed: an
    # order with no user attribution can't be safely credited to anyone, and a
    # note that names a DIFFERENT user must never credit this caller.
    notes_user_id = payment["notes"].get("user_id")
    if notes_user_id is None:
        raise HTTPException(
            status_code=400,
            detail="This order is missing its user attribution; cannot credit.",
        )
    if str(notes_user_id) != str(user.id):
        raise HTTPException(
            status_code=403,
            detail="This payment belongs to a different account.",
        )

    amount_inr = payment["amount_inr"]

    # --- Idempotency (fast path) -------------------------------------------
    # Both the webhook and the client-side /verify path credit the same
    # payment. A prior TOPUP row for this razorpay_payment_id means it's
    # already done. The UNIQUE constraint on razorpay_payment_id is the
    # authoritative guard for the concurrent case (handled below); this check
    # just avoids the wasted work in the common already-credited case.
    already = await _already_credited(db, req.razorpay_payment_id)
    if already:
        logger.info(f"Payment {req.razorpay_payment_id} already credited — skipping verify (idempotent).")
        # Column select: `user` was loaded at auth time and may be stale.
        fresh = await db.execute(select(User.coin_balance).where(User.id == user.id))
        return {"status": "success", "coins_credited": 0, "new_balance": round(fresh.scalar_one(), 2)}

    # Calculate coins to credit from the Razorpay-confirmed amount
    coins = payment_service.calculate_coins(amount_inr)

    credited = await _credit_topup(
        db,
        user_id=user.id,
        coins=coins,
        payment_id=req.razorpay_payment_id,
        description=f"Charging credit added: ₹{amount_inr:.2f} → {coins:.2f} credit (Razorpay: {req.razorpay_payment_id})",
    )
    if credited is None:
        # Lost the race to the webhook (or a duplicate submit). Not an error.
        logger.info(f"Payment {req.razorpay_payment_id} credited concurrently — treating verify as idempotent.")
        fresh = await db.execute(select(User.coin_balance).where(User.id == user.id))
        return {"status": "success", "coins_credited": 0, "new_balance": round(fresh.scalar_one(), 2)}

    logger.info(f"Payment verified: user={user.email}, ₹{amount_inr:.2f} → {coins:.2f} coins")
    return {
        "status": "success",
        "coins_credited": coins,
        "new_balance": round(credited, 2),
    }


async def _handle_refund_webhook(db: AsyncSession, refund: dict) -> dict:
    """
    Handle a verified Razorpay refund event (see `razorpay_webhook`'s
    docstring, step 3b): debit the driver by the refunded coin-equivalent so
    they can't keep coins for money that's been returned outside AmpHive.

    `refund` is the dict returned by `extract_payment_from_webhook`'s sibling
    `extract_refund_from_webhook`: {refund_id, payment_id, amount_inr, coins}.

    Idempotent on refund_id (see `_debit_refund`). Gracefully acks — rather
    than erroring — a refund whose payment_id has no matching TOPUP row in
    our ledger (e.g. a payment predating this feature, or one we could never
    attribute to a user): there's nothing here to claw back, and erroring
    would just make Razorpay retry forever.
    """
    refund_id = refund["refund_id"]
    payment_id = refund["payment_id"]

    # --- Idempotency (fast path) -------------------------------------------
    # A prior ledger row keyed on this refund_id means it was already
    # processed (by an earlier webhook retry). The authoritative guard for
    # the concurrent case is the UNIQUE constraint, handled by _debit_refund.
    if await _already_credited(db, refund_id):
        logger.info(f"Webhook refund {refund_id} already processed — skipping (idempotent).")
        return {"status": "already_refunded", "refund_id": refund_id}

    user_id = await _topup_user_for_payment(db, payment_id)
    if user_id is None:
        logger.warning(
            f"Webhook refund {refund_id} references payment {payment_id}, which has "
            "no matching topup in our ledger; nothing to claw back."
        )
        return {"status": "payment_not_found", "refund_id": refund_id, "payment_id": payment_id}

    coins = refund["coins"]
    new_balance = await _debit_refund(
        db,
        user_id=user_id,
        coins=coins,
        refund_id=refund_id,
        payment_id=payment_id,
        description=f"Charging credit reversed (refund): ₹{refund['amount_inr']:.2f} → -{coins:.2f} credit (Razorpay refund: {refund_id})",
    )
    if new_balance is None:
        # Lost the race to a redelivered webhook.
        if await _already_credited(db, refund_id):
            return {"status": "already_refunded", "refund_id": refund_id}
        logger.warning(f"Webhook refund {refund_id} could not be applied to user {user_id}.")
        return {"status": "user_not_found", "refund_id": refund_id}

    logger.info(
        f"Webhook refund debited user={user_id}: ₹{refund['amount_inr']:.2f} → {coins:.2f} coins clawed back "
        f"(refund={refund_id}, payment={payment_id})"
    )
    return {
        "status": "refunded",
        "refund_id": refund_id,
        "payment_id": payment_id,
        "coins_debited": coins,
        "new_balance": round(new_balance, 2),
    }


@router.post("/api/payments/webhook")
async def razorpay_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Razorpay server-to-server webhook (server-authoritative credit path).

    Razorpay calls this endpoint when a payment event occurs. Unlike the
    client-side /verify path (which depends on the browser round-tripping the
    signature back to us), this callback comes straight from Razorpay's
    servers, so it credits the wallet even if the user closes the tab right
    after paying.

    Flow:
      1. Verify the HMAC signature (X-Razorpay-Signature) against the raw body.
         This applies uniformly to every event type below, refunds included —
         an attacker can't forge a wallet debit any more than a credit.
      2. Parse the event and extract either a settled payment (payment.captured)
         or a refund (refund.created / refund.processed / payment.refunded).
      3a. Payment path — idempotency guard: if a ledger TOPUP row already
          references this razorpay_payment_id (created here OR by /verify),
          do nothing. This prevents double-crediting when both paths fire for
          the same payment. Then atomically credit coins and write a ledger
          entry.
      3b. Refund path — a driver can top up, then get the charge refunded
          externally (chargeback, or a refund issued directly by the card
          issuer / in the Razorpay dashboard) while keeping the credited
          coins; this claws them back. Idempotency guard mirrors the payment
          path but keyed on the refund_id. The debited user is looked up via
          the refund's payment_id against our own TOPUP ledger row (refund
          entities don't reliably carry the notes we attach at order-creation
          time). Debits are clamped so the balance never goes negative.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not payment_service.verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    # Parse the event
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    event_type = event.get("event", "")
    logger.info(f"Razorpay webhook received: {event_type}")

    # --- Refund path: claw back coins for money returned outside AmpHive ---
    # Checked before the capture path since the event names are disjoint
    # (payment.refunded is the only overlap risk, and extract_payment_from_webhook
    # only matches "payment.captured", so there's no double-handling).
    refund = payment_service.extract_refund_from_webhook(event)
    if refund is not None:
        return await _handle_refund_webhook(db, refund)

    # Extract the creditable payment. Returns None for non-payment events
    # (e.g. order.paid) or captures we can't attribute to a user.
    payment = payment_service.extract_payment_from_webhook(event)
    if payment is None:
        # Not a creditable event — acknowledge so Razorpay stops retrying.
        return {"status": "ignored", "event": event_type}

    payment_id = payment["payment_id"]

    # --- Idempotency (fast path) -------------------------------------------
    # A prior TOPUP row for this razorpay_payment_id means it was already
    # credited (by an earlier webhook retry or by /verify). The authoritative
    # guard for the concurrent case is the UNIQUE constraint, handled by
    # _credit_topup below.
    if await _already_credited(db, payment_id):
        logger.info(f"Webhook payment {payment_id} already credited — skipping (idempotent).")
        return {"status": "already_credited", "payment_id": payment_id}

    coins = payment["coins"]
    new_balance = await _credit_topup(
        db,
        user_id=payment["user_id"],
        coins=coins,
        payment_id=payment_id,
        description=f"Charging credit added (webhook): ₹{payment['amount_inr']:.2f} → {coins:.2f} credit (Razorpay: {payment_id})",
    )
    if new_balance is None:
        # Either the user doesn't exist, or /verify won the race. Distinguish
        # so Razorpay gets a truthful ack and stops retrying in both cases.
        if await _already_credited(db, payment_id):
            return {"status": "already_credited", "payment_id": payment_id}
        logger.warning(f"Webhook payment {payment_id} references unknown user {payment['user_id']}.")
        return {"status": "user_not_found", "payment_id": payment_id}

    logger.info(
        f"Webhook credited user={payment['user_id']}: ₹{payment['amount_inr']:.2f} → {coins:.2f} coins "
        f"(payment={payment_id})"
    )
    return {
        "status": "credited",
        "payment_id": payment_id,
        "coins_credited": coins,
        "new_balance": round(new_balance, 2),
    }


