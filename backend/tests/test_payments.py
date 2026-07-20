"""
Tests for the server-authoritative payment amount fix, plus the previously
untested money-credit gates: webhook/checkout signature verification and the
router-level status/ownership checks on /api/payments/verify and
/api/payments/webhook.

The Razorpay checkout signature covers only (order_id, payment_id) — not the
amount — so /api/payments/verify must credit the amount reported by Razorpay's
API, never the client request. These tests pin fetch_captured_payment(), the
helper the endpoint relies on for that guarantee.
"""

import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import razorpay
from fastapi import HTTPException

import backend.routers.payments as payments_router
import backend.services.payments as payments
from backend.schemas import VerifyPaymentRequest


def _client_returning(payment: dict) -> MagicMock:
    client = MagicMock()
    client.payment.fetch.return_value = payment
    return client


def test_returns_razorpay_amount_not_client_amount():
    """Amount comes from the fetched entity (paise → rupees)."""
    payment = {
        "id": "pay_1",
        "order_id": "order_1",
        "status": "captured",
        "amount": 1000,  # ₹10.00 in paise
        "notes": {"user_id": "42"},
    }
    with patch.object(payments, "get_razorpay_client", return_value=_client_returning(payment)):
        out = payments.fetch_captured_payment("pay_1", "order_1")

    assert out is not None
    assert out["amount_inr"] == 10.0
    assert out["status"] == "captured"
    assert out["notes"] == {"user_id": "42"}


def test_rejects_payment_from_a_different_order():
    """A genuine signature replayed with a mismatched order must not credit."""
    payment = {"id": "pay_1", "order_id": "order_OTHER", "status": "captured", "amount": 1000}
    with patch.object(payments, "get_razorpay_client", return_value=_client_returning(payment)):
        assert payments.fetch_captured_payment("pay_1", "order_1") is None


def test_returns_none_when_razorpay_unconfigured():
    with patch.object(payments, "get_razorpay_client", return_value=None):
        assert payments.fetch_captured_payment("pay_1", "order_1") is None


def test_returns_none_on_api_error():
    client = MagicMock()
    client.payment.fetch.side_effect = RuntimeError("network down")
    with patch.object(payments, "get_razorpay_client", return_value=client):
        assert payments.fetch_captured_payment("pay_1", "order_1") is None


def test_missing_notes_normalized_to_empty_dict():
    payment = {"id": "pay_1", "order_id": "order_1", "status": "authorized", "amount": 5000, "notes": None}
    with patch.object(payments, "get_razorpay_client", return_value=_client_returning(payment)):
        out = payments.fetch_captured_payment("pay_1", "order_1")

    assert out["notes"] == {}
    assert out["status"] == "authorized"  # caller decides captured-only policy


# ===========================================================================
# verify_webhook_signature — HMAC over the raw webhook body
# ===========================================================================

def test_webhook_signature_valid_hmac_accepted():
    secret = "whsec_test_123"
    body = b'{"event": "payment.captured"}'
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    with patch.object(payments, "RAZORPAY_WEBHOOK_SECRET", secret):
        assert payments.verify_webhook_signature(body, sig) is True


def test_webhook_signature_tampered_body_rejected():
    secret = "whsec_test_123"
    body = b'{"event": "payment.captured"}'
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    tampered_body = b'{"event": "payment.captured", "extra": "injected"}'
    with patch.object(payments, "RAZORPAY_WEBHOOK_SECRET", secret):
        assert payments.verify_webhook_signature(tampered_body, sig) is False


def test_webhook_signature_empty_secret_rejected():
    """An unset RAZORPAY_WEBHOOK_SECRET must fail closed, never fall back to
    accepting an unverifiable payload."""
    body = b'{"event": "payment.captured"}'
    sig = hmac.new(b"whatever", body, hashlib.sha256).hexdigest()
    with patch.object(payments, "RAZORPAY_WEBHOOK_SECRET", ""):
        assert payments.verify_webhook_signature(body, sig) is False


# ===========================================================================
# verify_payment_signature — checkout HMAC of "order_id|payment_id"
# ===========================================================================

def test_checkout_signature_valid_accepted():
    secret = "key_secret_test"
    client = razorpay.Client(auth=("key_id_test", secret))
    order_id, payment_id = "order_1", "pay_1"
    msg = f"{order_id}|{payment_id}"
    sig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    with patch.object(payments, "get_razorpay_client", return_value=client):
        assert payments.verify_payment_signature(order_id, payment_id, sig) is True


def test_checkout_signature_bad_signature_rejected():
    secret = "key_secret_test"
    client = razorpay.Client(auth=("key_id_test", secret))
    with patch.object(payments, "get_razorpay_client", return_value=client):
        assert payments.verify_payment_signature("order_1", "pay_1", "not_the_real_signature") is False


def test_checkout_signature_unconfigured_client_rejected():
    with patch.object(payments, "get_razorpay_client", return_value=None):
        assert payments.verify_payment_signature("order_1", "pay_1", "sig") is False


# ===========================================================================
# extract_payment_from_webhook — attributing a webhook payload to a credit
# ===========================================================================

def _captured_event(notes: dict | None = None, amount_paise: int = 1000) -> dict:
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_1",
                    "amount": amount_paise,
                    "notes": notes or {},
                }
            }
        },
    }


def test_extract_ignores_non_captured_event():
    event = {"event": "payment.authorized", "payload": {"payment": {"entity": {"id": "pay_1"}}}}
    assert payments.extract_payment_from_webhook(event) is None


def test_extract_returns_none_when_notes_missing_user_id():
    event = _captured_event(notes={"coins": "10"})
    assert payments.extract_payment_from_webhook(event) is None


def test_extract_prefers_notes_coins_over_derived_amount():
    """notes.coins (pre-computed at order time) wins over deriving from the
    settled amount, even when they'd disagree."""
    event = _captured_event(notes={"user_id": "42", "coins": "999"}, amount_paise=1000)
    out = payments.extract_payment_from_webhook(event)
    assert out == {"payment_id": "pay_1", "user_id": 42, "amount_inr": 10.0, "coins": 999.0}


def test_extract_falls_back_to_amount_derived_coins_when_notes_coins_absent():
    event = _captured_event(notes={"user_id": "42"}, amount_paise=1500)
    out = payments.extract_payment_from_webhook(event)
    assert out == {"payment_id": "pay_1", "user_id": 42, "amount_inr": 15.0, "coins": 15.0}


# ===========================================================================
# routers/payments.py verify_payment — signature/status/ownership gates
# ===========================================================================

def _verify_req():
    return VerifyPaymentRequest(
        razorpay_order_id="order_1",
        razorpay_payment_id="pay_1",
        razorpay_signature="sig_1",
    )


def _driver(user_id: int = 1):
    return SimpleNamespace(id=user_id, email="driver@amphive.test")


@pytest.mark.asyncio
async def test_verify_endpoint_400_on_invalid_signature():
    with patch.object(payments_router.payment_service, "verify_payment_signature", return_value=False):
        with pytest.raises(HTTPException) as exc:
            await payments_router.verify_payment(_verify_req(), _driver(), AsyncMock())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_endpoint_409_when_not_captured():
    fetched = {
        "payment_id": "pay_1", "order_id": "order_1", "status": "authorized",
        "amount_inr": 10.0, "notes": {"user_id": "1"},
    }
    with patch.object(payments_router.payment_service, "verify_payment_signature", return_value=True), \
         patch.object(payments_router.payment_service, "fetch_captured_payment", return_value=fetched):
        with pytest.raises(HTTPException) as exc:
            await payments_router.verify_payment(_verify_req(), _driver(), AsyncMock())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_verify_endpoint_403_on_order_notes_user_mismatch():
    fetched = {
        "payment_id": "pay_1", "order_id": "order_1", "status": "captured",
        "amount_inr": 10.0, "notes": {"user_id": "999"},  # order was created for a different user
    }
    with patch.object(payments_router.payment_service, "verify_payment_signature", return_value=True), \
         patch.object(payments_router.payment_service, "fetch_captured_payment", return_value=fetched):
        with pytest.raises(HTTPException) as exc:
            await payments_router.verify_payment(_verify_req(), _driver(user_id=1), AsyncMock())
    assert exc.value.status_code == 403


# ===========================================================================
# routers/payments.py razorpay_webhook — HMAC + empty-secret hard-fail
# ===========================================================================

class _FakeRequest:
    """Just enough of fastapi.Request for the webhook handler: async
    .body() and a .headers mapping with .get()."""

    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_webhook_endpoint_rejects_bad_hmac():
    request = _FakeRequest(b'{"event": "payment.captured"}', {"X-Razorpay-Signature": "bad_sig"})
    with patch.object(payments_router.payment_service, "verify_webhook_signature", return_value=False):
        with pytest.raises(HTTPException) as exc:
            await payments_router.razorpay_webhook(request, AsyncMock())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_webhook_endpoint_empty_secret_hard_fails():
    """No mocking of verify_webhook_signature here — exercises the real
    fail-closed behavior end to end through the router when the webhook
    secret isn't configured."""
    body = b'{"event": "payment.captured"}'
    sig = hmac.new(b"whatever", body, hashlib.sha256).hexdigest()
    request = _FakeRequest(body, {"X-Razorpay-Signature": sig})
    with patch.object(payments, "RAZORPAY_WEBHOOK_SECRET", ""):
        with pytest.raises(HTTPException) as exc:
            await payments_router.razorpay_webhook(request, AsyncMock())
    assert exc.value.status_code == 400
