"""
Tests for registration input validation (TD#30).

RegisterRequest now enforces a syntactically valid email (pydantic EmailStr)
and an 8-72 character password (72: bcrypt's silent truncation boundary).
LoginRequest deliberately stays unvalidated so accounts created before this
rule can still sign in.

Schema-level and DB-free.
"""
import pytest
from pydantic import ValidationError

from backend.schemas import LoginRequest, RegisterRequest


def test_valid_registration_accepted():
    req = RegisterRequest(
        email="driver@example.com", password="s3cure-pw", full_name="A Driver"
    )
    assert req.email == "driver@example.com"


@pytest.mark.parametrize("bad_email", [
    "not-an-email",
    "missing-domain@",
    "@missing-local.com",
    "spaces in@example.com",
    "",
])
def test_malformed_email_rejected(bad_email):
    with pytest.raises(ValidationError) as exc_info:
        RegisterRequest(email=bad_email, password="s3cure-pw", full_name="X")
    assert any(e["loc"] == ("email",) for e in exc_info.value.errors())


@pytest.mark.parametrize("bad_password", [
    "",          # empty
    "short",     # < 8 chars
    "1234567",   # boundary: 7 chars
    "x" * 73,    # > 72 chars (bcrypt truncation boundary)
])
def test_weak_password_rejected(bad_password):
    with pytest.raises(ValidationError) as exc_info:
        RegisterRequest(email="a@example.com", password=bad_password, full_name="X")
    assert any(e["loc"] == ("password",) for e in exc_info.value.errors())


def test_password_boundaries_accepted():
    RegisterRequest(email="a@example.com", password="12345678", full_name="X")   # exactly 8
    RegisterRequest(email="a@example.com", password="x" * 72, full_name="X")     # exactly 72


def test_login_still_accepts_legacy_credentials():
    """Pre-rule accounts (short password / odd email) must still be able to
    log in — LoginRequest is intentionally unvalidated."""
    req = LoginRequest(email="weird@local", password="pw")
    assert req.password == "pw"
