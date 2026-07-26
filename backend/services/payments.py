"""
AmpHive Razorpay Payment Service
=================================
Handles Razorpay order creation, payment verification (HMAC SHA256), and
webhook processing for crediting coins to user wallets.

Design decisions:
- Razorpay is used because Stripe is not viable for Indian MVPs without a
  registered business entity. Razorpay works in test mode with no entity.
- Payment flow:
  1. Frontend calls POST /api/payments/create-order → backend creates Razorpay order
  2. Frontend opens Razorpay Checkout modal with the order_id
  3. User pays via UPI/card/wallet/net banking
  4. Razorpay returns payment_id + signature to frontend
  5. Frontend calls POST /api/payments/verify → backend verifies signature,
     credits coins to the user's wallet
- Webhook endpoint is a backup verification path (Razorpay server-to-server).
- The webhook also handles refund events (refund.created / refund.processed /
  payment.refunded) so a driver who tops up and then gets refunded outside
  AmpHive (e.g. a chargeback or a refund issued directly on the card/UPI
  rail) has the equivalent coins clawed back from their wallet — see
  extract_refund_from_webhook() and routers/payments.py `_debit_refund`.
"""

import hashlib
import hmac
import logging
import os
from typing import Optional

import razorpay

logger = logging.getLogger("amphive.payments")

# --- Configuration ---

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# Coin conversion rate: how many coins per ₹1
# Default: 1 coin = ₹1 (1:1 mapping for simplicity)
COINS_PER_RUPEE = float(os.getenv("COINS_PER_RUPEE", "1.0"))


def get_razorpay_client() -> Optional[razorpay.Client]:
    """
    Initialize and return a Razorpay client. Returns None if keys are not
    configured (e.g. in development without Razorpay credentials).
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        logger.warning(
            "Razorpay keys not configured (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET). "
            "Payment features will be disabled."
        )
        return None

    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def create_order(amount_inr: float, user_id: int, receipt_prefix: str = "amphive") -> Optional[dict]:
    """
    Create a Razorpay order for a wallet top-up.

    Args:
        amount_inr: Amount in Indian Rupees (e.g. 100 for ₹100).
        user_id: The AmpHive user ID making the payment.
        receipt_prefix: Prefix for the receipt ID.

    Returns:
        Razorpay order dict containing 'id', 'amount', 'currency', etc.
        Returns None if Razorpay client is not configured.

    Note: Razorpay expects amount in paise (1 INR = 100 paise).
    """
    client = get_razorpay_client()
    if client is None:
        return None

    try:
        order_data = {
            "amount": int(round(amount_inr * 100)),  # Convert to paise
            "currency": "INR",
            "receipt": f"{receipt_prefix}_user{user_id}_{int(amount_inr)}",
            "notes": {
                "user_id": str(user_id),
                "coins": str(calculate_coins(amount_inr)),
                "platform": "amphive",
            },
        }
        order = client.order.create(data=order_data)
        logger.info(f"Created Razorpay order {order['id']} for user {user_id}: ₹{amount_inr}")
        return order
    except Exception as e:
        logger.error(f"Failed to create Razorpay order for user {user_id}: {e}")
        return None


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """
    Verify the payment signature returned by Razorpay Checkout.

    After the user completes payment, Razorpay returns:
    - razorpay_order_id
    - razorpay_payment_id
    - razorpay_signature

    The signature is an HMAC SHA256 of "order_id|payment_id" using the
    Razorpay Key Secret. This ensures the payment wasn't tampered with.

    Returns True if the signature is valid, False otherwise.
    """
    client = get_razorpay_client()
    if client is None:
        return False

    try:
        params = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        }
        client.utility.verify_payment_signature(params)
        logger.info(f"Payment signature verified: order={order_id}, payment={payment_id}")
        return True
    except razorpay.errors.SignatureVerificationError:
        logger.warning(f"Payment signature verification FAILED: order={order_id}, payment={payment_id}")
        return False


def fetch_captured_payment(payment_id: str, expected_order_id: str) -> Optional[dict]:
    """
    Fetch the authoritative payment entity from Razorpay's server API.

    The checkout signature (HMAC of "order_id|payment_id") proves the pair is
    genuine, but it does NOT cover the amount — so the amount to credit must
    always come from Razorpay itself, never from the client request.

    Returns {payment_id, order_id, status, amount_inr, notes} on success, or
    None if the payment can't be fetched or doesn't belong to
    expected_order_id (a mismatched order means the caller is replaying a
    signature from a different purchase).
    """
    client = get_razorpay_client()
    if client is None:
        return None

    try:
        payment = client.payment.fetch(payment_id)
    except Exception as e:
        logger.error(f"Failed to fetch payment {payment_id} from Razorpay: {e}")
        return None

    if payment.get("order_id") != expected_order_id:
        logger.warning(
            f"Payment {payment_id} belongs to order {payment.get('order_id')!r}, "
            f"not the claimed {expected_order_id!r} — refusing to credit."
        )
        return None

    return {
        "payment_id": payment_id,
        "order_id": payment.get("order_id"),
        "status": payment.get("status", ""),
        # Razorpay reports the amount in paise; convert to rupees.
        "amount_inr": float(payment.get("amount", 0)) / 100.0,
        "notes": payment.get("notes") or {},
    }


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """
    Verify the webhook signature from Razorpay's server-to-server callbacks.

    Razorpay signs webhook payloads using HMAC SHA256 with the Webhook Secret.
    This is a backup verification path — the primary path is the client-side
    signature verification in verify_payment_signature().

    Returns True if the signature is valid, False otherwise.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.warning("Webhook secret not configured, skipping verification.")
        return False

    expected = hmac.new(
        key=RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def calculate_coins(amount_inr: float) -> float:
    """
    Calculate the number of coins to credit for a given INR amount.
    Uses the COINS_PER_RUPEE conversion rate (default 1:1).
    """
    return round(float(amount_inr * COINS_PER_RUPEE), 2)


def extract_payment_from_webhook(event: dict) -> Optional[dict]:
    """
    Extract the credit-relevant fields from a Razorpay webhook event payload.

    Razorpay wraps the payment entity inside the webhook body like so:
        {
          "event": "payment.captured",
          "payload": { "payment": { "entity": { ...payment fields... } } }
        }

    The payment entity carries the `notes` dict we attached at order-creation
    time in create_order() (user_id + coins), plus the authoritative `amount`
    (in paise) and a unique `id` (the razorpay_payment_id). We prefer the
    `notes` values but fall back to deriving coins from `amount` so a webhook
    can still credit correctly even if the notes were stripped.

    Args:
        event: The parsed JSON webhook body.

    Returns:
        A dict {payment_id, user_id, amount_inr, coins} when the event is a
        creditable payment we can attribute to a user, or None otherwise
        (e.g. non-payment events, or a payment with no resolvable user_id).
    """
    # Only payment.captured represents money that has actually settled.
    # (authorized -> captured; we credit on capture to avoid crediting funds
    # that were authorized but never captured.)
    if event.get("event") != "payment.captured":
        return None

    entity = (
        event.get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )
    if not entity:
        return None

    payment_id = entity.get("id")
    if not payment_id:
        return None

    notes = entity.get("notes") or {}

    # Resolve the user_id — required to know whose wallet to credit.
    user_id_raw = notes.get("user_id")
    try:
        user_id = int(user_id_raw) if user_id_raw is not None else None
    except (TypeError, ValueError):
        user_id = None
    if user_id is None:
        logger.warning(
            f"Webhook payment {payment_id} has no resolvable user_id in notes; "
            "cannot attribute credit."
        )
        return None

    # Amount is authoritative from Razorpay, in paise. Convert to rupees.
    amount_inr = float(entity.get("amount", 0)) / 100.0

    # Prefer the coins we pre-computed at order time; fall back to deriving
    # them from the settled amount so the credit is never zero due to notes.
    coins_raw = notes.get("coins")
    try:
        coins = float(coins_raw) if coins_raw is not None else calculate_coins(amount_inr)
    except (TypeError, ValueError):
        coins = calculate_coins(amount_inr)

    return {
        "payment_id": payment_id,
        "user_id": user_id,
        "amount_inr": amount_inr,
        "coins": coins,
    }


# Razorpay fires slightly different event names depending on the refund's
# lifecycle stage / how it was initiated, but all three carry the same refund
# entity shape under payload["refund"]["entity"] (payment.refunded additionally
# includes a payload["payment"]["entity"], which we don't need — the refund
# entity alone has everything required to reverse the credit).
REFUND_EVENT_NAMES = {"refund.processed", "refund.created", "payment.refunded"}


def extract_refund_from_webhook(event: dict) -> Optional[dict]:
    """
    Extract the debit-relevant fields from a Razorpay refund webhook event
    payload.

    Razorpay wraps the refund entity inside the webhook body like so:
        {
          "event": "refund.processed",
          "payload": { "refund": { "entity": { ...refund fields... } } }
        }

    Unlike payment.captured, the refund entity does not reliably carry the
    `notes` (user_id/coins) we attached at order-creation time — Razorpay only
    copies notes onto a refund if they were explicitly passed when the refund
    was created, which is out of our control since refunds are typically
    issued by the driver's card issuer or by an operator directly in the
    Razorpay dashboard, not through AmpHive. So this function deliberately
    does NOT resolve a user_id — the caller must look up the user via the
    refund's `payment_id`, matching it against the original TOPUP ledger row
    (see routers/payments.py `_topup_user_for_payment`).

    Args:
        event: The parsed JSON webhook body.

    Returns:
        A dict {refund_id, payment_id, amount_inr, coins} when the event is a
        refund we can extract, or None otherwise (e.g. non-refund events, or a
        refund entity missing its id/payment_id).
    """
    if event.get("event") not in REFUND_EVENT_NAMES:
        return None

    entity = (
        event.get("payload", {})
        .get("refund", {})
        .get("entity", {})
    )
    if not entity:
        return None

    refund_id = entity.get("id")
    payment_id = entity.get("payment_id")
    if not refund_id or not payment_id:
        return None

    # Amount is authoritative from Razorpay, in paise. Convert to rupees. This
    # is the amount of THIS refund (partial refunds carry only their own
    # slice), mirroring how amount is read off the payment entity above.
    amount_inr = float(entity.get("amount", 0)) / 100.0
    coins = calculate_coins(amount_inr)

    return {
        "refund_id": refund_id,
        "payment_id": payment_id,
        "amount_inr": amount_inr,
        "coins": coins,
    }
