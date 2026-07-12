"""
Payments routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import async_session_factory, get_db
from backend.database.models import (
    ChargerGroup, ChargingSession, Gateway, GatewayStatus, GroupMembership,
    LedgerTransaction, Plug, PlugStatus, SessionStatus, TelemetryReading,
    Tenant, TransactionType, User, UserRole,
)
from backend.schemas import (
    AuthResponse, CpoGatewayCreateRequest, CpoGroupCreateRequest,
    CpoGroupUpdateRequest, CpoPlugCreateRequest, CpoPlugUpdateRequest,
    CpoSetupRequest, CreateOrderRequest, CreateOrderResponse,
    DirectPlugRequest, GatewayRegisterRequest, GroupResponse,
    JoinGroupRequest, LedgerEntryResponse, LoginRequest, PlugRegisterRequest,
    PlugResponse, RegisterRequest, SessionStartRequest, SessionStopRequest,
    UserResponse, VerifyPaymentRequest,
)
from backend.services import payments as payment_service
from backend.services.auth import (
    create_access_token, decode_access_token, get_current_user,
    hash_password, verify_password,
)
from backend.services.money import ZERO_MONEY, to_money
from backend.services.rbac import require_role
from backend.services.session_lifecycle import (
    check_and_speed_up_active_session, finalize_charging_session,
    gateway_is_live, set_plug_telemetry_interval,
)
from backend.services.telemetry import COINS_PER_KWH
from backend.services.wallet import available_balance, credit_wallet

logger = logging.getLogger("amphive.api")
router = APIRouter()

# ===========================================================================
# Payment Endpoints (Razorpay)
# ===========================================================================

async def _already_credited(db: AsyncSession, payment_id: str) -> bool:
    """True if a TOPUP ledger row already exists for this razorpay_payment_id."""
    existing = await db.execute(
        select(LedgerTransaction.id).where(
            LedgerTransaction.razorpay_payment_id == payment_id
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
        "Wallet topped up",
        f"{credit:.2f} coins credited — balance is now {new_balance:.2f}.",
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


@router.post("/api/payments/create-order", response_model=CreateOrderResponse)
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

    order = payment_service.create_order(req.amount_inr, user.id)
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

    # The order's notes carry the user it was created for — refuse to credit
    # this caller with a payment made for a different account.
    notes_user_id = payment["notes"].get("user_id")
    if notes_user_id is not None and str(notes_user_id) != str(user.id):
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
        description=f"Wallet top-up: ₹{amount_inr:.2f} → {coins:.2f} coins (Razorpay: {req.razorpay_payment_id})",
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
      2. Parse the event and extract the settled payment (payment.captured).
      3. Idempotency guard: if a ledger TOPUP row already references this
         razorpay_payment_id (created here OR by /verify), do nothing. This
         prevents double-crediting when both paths fire for the same payment.
      4. Atomically credit coins (row-locked user) and write a ledger entry.
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

    # Extract the creditable payment. Returns None for non-payment events
    # (e.g. order.paid, refund.*) or captures we can't attribute to a user.
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
        description=f"Wallet top-up (webhook): ₹{payment['amount_inr']:.2f} → {coins:.2f} coins (Razorpay: {payment_id})",
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


