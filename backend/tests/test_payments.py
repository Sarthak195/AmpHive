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
# extract_refund_from_webhook — attributing a webhook payload to a wallet
# clawback. Unlike captures, the refund entity carries no notes/user_id — the
# caller resolves the user via payment_id against our own ledger — so this
# only needs to prove refund_id/payment_id/amount extraction is correct.
# ===========================================================================

def _refund_event(
    event_name: str = "refund.processed",
    refund_id: str = "rfnd_1",
    payment_id: str = "pay_1",
    amount_paise: int = 1000,
) -> dict:
    return {
        "event": event_name,
        "payload": {
            "refund": {
                "entity": {
                    "id": refund_id,
                    "payment_id": payment_id,
                    "amount": amount_paise,
                }
            }
        },
    }


def test_extract_refund_ignores_non_refund_event():
    event = _captured_event()
    assert payments.extract_refund_from_webhook(event) is None


@pytest.mark.parametrize("event_name", ["refund.processed", "refund.created", "payment.refunded"])
def test_extract_refund_handles_every_refund_event_name(event_name):
    """All three Razorpay refund event names carry the same refund entity
    shape and must extract identically."""
    event = _refund_event(event_name=event_name, amount_paise=500)
    out = payments.extract_refund_from_webhook(event)
    assert out == {"refund_id": "rfnd_1", "payment_id": "pay_1", "amount_inr": 5.0, "coins": 5.0}


def test_extract_refund_returns_none_when_entity_missing():
    event = {"event": "refund.processed", "payload": {}}
    assert payments.extract_refund_from_webhook(event) is None


def test_extract_refund_returns_none_when_payment_id_missing():
    event = {
        "event": "refund.processed",
        "payload": {"refund": {"entity": {"id": "rfnd_1", "amount": 500}}},
    }
    assert payments.extract_refund_from_webhook(event) is None


def test_extract_refund_returns_none_when_id_missing():
    event = {
        "event": "refund.processed",
        "payload": {"refund": {"entity": {"payment_id": "pay_1", "amount": 500}}},
    }
    assert payments.extract_refund_from_webhook(event) is None


def test_extract_refund_partial_amount_derives_coins():
    """A partial refund's amount is this refund's own slice, not the full
    payment — coins derive from that slice via the same conversion as topups."""
    event = _refund_event(amount_paise=250)
    out = payments.extract_refund_from_webhook(event)
    assert out == {"refund_id": "rfnd_1", "payment_id": "pay_1", "amount_inr": 2.5, "coins": 2.5}


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


@pytest.mark.asyncio
async def test_webhook_endpoint_rejects_bad_hmac_for_refund_event():
    """The signature gate is checked before the event is even parsed, so it
    applies uniformly to refund events too — a forged refund webhook can't
    trigger a debit any more than a forged capture can trigger a credit."""
    request = _FakeRequest(b'{"event": "refund.processed"}', {"X-Razorpay-Signature": "bad_sig"})
    with patch.object(payments_router.payment_service, "verify_webhook_signature", return_value=False):
        with pytest.raises(HTTPException) as exc:
            await payments_router.razorpay_webhook(request, AsyncMock())
    assert exc.value.status_code == 400


# ===========================================================================
# routers/payments.py razorpay_webhook — refund handling (claws back coins)
#
# These isolate the router's orchestration logic by mocking the DB-touching
# helpers (_already_credited / _topup_user_for_payment / _debit_refund)
# directly, the same way the tests above isolate it from Razorpay's SDK by
# mocking payment_service — no real database involved.
# ===========================================================================

def _refund_dict(refund_id="rfnd_1", payment_id="pay_1", amount_inr=9.0, coins=9.0) -> dict:
    return {"refund_id": refund_id, "payment_id": payment_id, "amount_inr": amount_inr, "coins": coins}


@pytest.mark.asyncio
async def test_webhook_refund_debits_correct_coins():
    """A verified refund event resolves the topup's user, debits exactly the
    coin-equivalent of the refunded amount, and reports it back."""
    request = _FakeRequest(b"{}", {"X-Razorpay-Signature": "sig"})
    refund = _refund_dict(amount_inr=9.0, coins=9.0)

    with patch.object(payments_router.payment_service, "verify_webhook_signature", return_value=True), \
         patch.object(payments_router.payment_service, "extract_refund_from_webhook", return_value=refund), \
         patch.object(payments_router, "_already_credited", AsyncMock(return_value=False)), \
         patch.object(payments_router, "_topup_user_for_payment", AsyncMock(return_value=42)), \
         patch.object(payments_router, "_debit_refund", AsyncMock(return_value=91.0)) as debit_mock:
        out = await payments_router.razorpay_webhook(request, AsyncMock())

    assert out == {
        "status": "refunded",
        "refund_id": "rfnd_1",
        "payment_id": "pay_1",
        "coins_debited": 9.0,
        "new_balance": 91.0,
    }
    debit_mock.assert_awaited_once()
    _, kwargs = debit_mock.call_args
    assert kwargs["user_id"] == 42
    assert kwargs["coins"] == 9.0
    assert kwargs["refund_id"] == "rfnd_1"
    assert kwargs["payment_id"] == "pay_1"


@pytest.mark.asyncio
async def test_webhook_refund_duplicate_is_noop():
    """A redelivered refund webhook (same refund_id, per Razorpay's
    at-least-once retry policy) must not debit twice or even look up the
    user again — it's short-circuited by the idempotency guard."""
    request = _FakeRequest(b"{}", {"X-Razorpay-Signature": "sig"})
    refund = _refund_dict()

    with patch.object(payments_router.payment_service, "verify_webhook_signature", return_value=True), \
         patch.object(payments_router.payment_service, "extract_refund_from_webhook", return_value=refund), \
         patch.object(payments_router, "_already_credited", AsyncMock(return_value=True)), \
         patch.object(payments_router, "_topup_user_for_payment", AsyncMock()) as user_lookup_mock, \
         patch.object(payments_router, "_debit_refund", AsyncMock()) as debit_mock:
        out = await payments_router.razorpay_webhook(request, AsyncMock())

    assert out == {"status": "already_refunded", "refund_id": "rfnd_1"}
    user_lookup_mock.assert_not_awaited()
    debit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_refund_unknown_payment_handled_gracefully():
    """A refund whose payment_id has no matching TOPUP row in our ledger
    (e.g. a payment made before this feature shipped) is acknowledged, not
    errored — there is nothing here to claw back, and erroring would just
    make Razorpay retry the webhook forever."""
    request = _FakeRequest(b"{}", {"X-Razorpay-Signature": "sig"})
    refund = _refund_dict(payment_id="pay_ghost")

    with patch.object(payments_router.payment_service, "verify_webhook_signature", return_value=True), \
         patch.object(payments_router.payment_service, "extract_refund_from_webhook", return_value=refund), \
         patch.object(payments_router, "_already_credited", AsyncMock(return_value=False)), \
         patch.object(payments_router, "_topup_user_for_payment", AsyncMock(return_value=None)), \
         patch.object(payments_router, "_debit_refund", AsyncMock()) as debit_mock:
        out = await payments_router.razorpay_webhook(request, AsyncMock())

    assert out == {"status": "payment_not_found", "refund_id": "rfnd_1", "payment_id": "pay_ghost"}
    debit_mock.assert_not_awaited()
