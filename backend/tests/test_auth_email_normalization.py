"""
Email case-insensitivity across register/login/forgot-password.

SECURITY audit MEDIUM finding: users.email's unique index is byte-exact, so
without normalization `Driver@x.com` and `driver@x.com` are different
accounts — a user who types a different case than they registered with
can't log in or recover their password, and a duplicate account can be
created accidentally. Fix is code-level canonicalization (trim + lowercase,
see services/auth.normalize_email) applied before every lookup/insert; no
migration (a case-insensitive unique index would need one — see the PR/
review notes for that residual risk).

Login's own case-insensitivity coverage lives in test_login.py alongside its
other login-specific tests; this file covers normalize_email itself plus
register and forgot-password.

Mocked-db style mirrors test_registration_races.py / test_password_reset.py.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.routers.auth import forgot_password, register
from backend.schemas import ForgotPasswordRequest, RegisterRequest
from backend.services import email as email_service
from backend.services.auth import normalize_email


def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  Driver@AmpHive.Test  ") == "driver@amphive.test"
    assert normalize_email("already@lower.com") == "already@lower.com"
    assert normalize_email("MIXED@Case.COM") == "mixed@case.com"


def _db_passing_exists_check():
    """Mock AsyncSession whose exists-check SELECT finds nothing."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_register_normalizes_email_for_duplicate_check_and_storage(monkeypatch):
    # Register now mints a verification token + emails the link after creating
    # the user (no auto-login) — stub the send so nothing hits SMTP.
    monkeypatch.setattr(email_service, "send_email_verification", AsyncMock())
    db = _db_passing_exists_check()

    req = RegisterRequest(email="Driver@Example.com", password="a-valid-pw", full_name="Driver")
    await register(req, db)
    await asyncio.sleep(0)  # let the fire-and-forget verification email run

    # The duplicate-check SELECT ran against the lowercased/trimmed email —
    # so a later `driver@example.com` registration attempt would collide.
    lookup_stmt = db.execute.await_args_list[0].args[0]
    assert "driver@example.com" in str(
        lookup_stmt.compile(compile_kwargs={"literal_binds": True})
    )
    # The stored User row (the FIRST db.add — the EmailVerificationToken is the
    # second) carries the normalized email, not the as-typed mixed-case one,
    # and starts UNVERIFIED.
    stored_user = db.add.call_args_list[0].args[0]
    assert stored_user.email == "driver@example.com"
    assert stored_user.email_verified is False


@pytest.mark.asyncio
async def test_forgot_password_matches_account_regardless_of_email_case(monkeypatch):
    """A user who registered as `driver@amphive.test` must still find their
    account (and get the reset email) when they submit a different case."""
    send_spy = AsyncMock()
    monkeypatch.setattr(email_service, "send_password_reset", send_spy)

    user = MagicMock()
    user.id = 1
    user.email = "driver@amphive.test"
    user.token_version = 0

    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = user
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[scalar_result, MagicMock()])
    db.add = MagicMock()

    res = await forgot_password(ForgotPasswordRequest(email="  Driver@AmpHive.Test  "), db)
    await asyncio.sleep(0)  # let the fire-and-forget email task run

    assert res["status"] == "ok"
    db.commit.assert_awaited_once()
    lookup_stmt = db.execute.await_args_list[0].args[0]
    assert "driver@amphive.test" in str(
        lookup_stmt.compile(compile_kwargs={"literal_binds": True})
    )
    send_spy.assert_awaited_once()
    assert send_spy.await_args.args[0] == "driver@amphive.test"
