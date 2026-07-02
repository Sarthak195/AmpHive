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
"""

import hashlib
import hmac
import os
import logging
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


def create_order(amount_inr: int, user_id: int, receipt_prefix: str = "amphive") -> Optional[dict]:
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
            "amount": amount_inr * 100,  # Convert to paise
            "currency": "INR",
            "receipt": f"{receipt_prefix}_user{user_id}_{amount_inr}",
            "notes": {
                "user_id": str(user_id),
                "coins": str(int(amount_inr * COINS_PER_RUPEE)),
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


def calculate_coins(amount_inr: int) -> int:
    """
    Calculate the number of coins to credit for a given INR amount.
    Uses the COINS_PER_RUPEE conversion rate (default 1:1).
    """
    return int(amount_inr * COINS_PER_RUPEE)


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
    amount_inr = int(entity.get("amount", 0)) // 100

    # Prefer the coins we pre-computed at order time; fall back to deriving
    # them from the settled amount so the credit is never zero due to notes.
    coins_raw = notes.get("coins")
    try:
        coins = int(coins_raw) if coins_raw is not None else calculate_coins(amount_inr)
    except (TypeError, ValueError):
        coins = calculate_coins(amount_inr)

    return {
        "payment_id": payment_id,
        "user_id": user_id,
        "amount_inr": amount_inr,
        "coins": coins,
    }
