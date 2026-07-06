"""
Tests for the server-authoritative payment amount fix.

The Razorpay checkout signature covers only (order_id, payment_id) — not the
amount — so /api/payments/verify must credit the amount reported by Razorpay's
API, never the client request. These tests pin fetch_captured_payment(), the
helper the endpoint relies on for that guarantee.
"""

from unittest.mock import MagicMock, patch

import backend.services.payments as payments


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
