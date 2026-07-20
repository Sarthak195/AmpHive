"""
Tests for the password-reset ("forgot password") flow.

POST /api/auth/forgot-password issues a single-use, time-boxed token (only the
SHA-256 digest is stored in password_reset_tokens) and always answers the same
generic 200 — no account enumeration. POST /api/auth/reset-password consumes
the token: same 8-72 password rule as registration, bcrypt rehash, DB-side
token_version bump (revokes every JWT — see test_token_revocation.py), token
stamped used. Both endpoints sit behind the sliding-window rate limiter.

Mock-DB style mirrors test_token_revocation.py (no database needed).
"""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.routers.auth import (
    RESET_TOKEN_TTL_MIN, _hash_reset_token, forgot_password, reset_password,
)
from backend.schemas import ForgotPasswordRequest, ResetPasswordRequest
from backend.services import email as email_service
from backend.services.rate_limit import SlidingWindowRateLimiter


def _now():
    return datetime.now(timezone.utc)


def _user(user_id=1, email="driver@amphive.test", token_version=0):
    u = MagicMock()
    u.id = user_id
    u.email = email
    u.token_version = token_version
    return u


def _db(execute_results):
    """AsyncMock db whose successive execute() calls return the given results."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(execute_results))
    db.add = MagicMock()
    return db


def _scalar(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _prt(user_id=1, expires_in_min=30, used_at=None):
    prt = MagicMock()
    prt.user_id = user_id
    prt.expires_at = _now() + timedelta(minutes=expires_in_min)
    prt.used_at = used_at
    return prt


# --------------------------------------------------------------------------
# forgot-password: issuance + enumeration safety
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forgot_password_issues_hashed_token_and_emails_link(monkeypatch):
    sent = {}

    async def fake_send(to_addr, reset_link, ttl_min):
        sent.update(to=to_addr, link=reset_link, ttl=ttl_min)

    monkeypatch.setattr(email_service, "send_password_reset", fake_send)
    # execute #1: user lookup; execute #2: void outstanding tokens (UPDATE)
    db = _db([_scalar(_user()), MagicMock()])

    res = await forgot_password(ForgotPasswordRequest(email="driver@amphive.test"), db)
    await asyncio.sleep(0)  # let the fire-and-forget email task run

    assert res["status"] == "ok"
    db.commit.assert_awaited_once()

    # The stored row holds the SHA-256 of the raw token in the emailed link —
    # never the token itself.
    row = db.add.call_args.args[0]
    assert sent["to"] == "driver@amphive.test"
    assert "/reset-password?token=" in sent["link"]
    raw_token = sent["link"].split("token=", 1)[1]
    assert row.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token not in row.token_hash  # digest, not the raw token
    # Time-boxed: expiry ≈ now + TTL.
    delta = row.expires_at - _now()
    assert timedelta(minutes=RESET_TOKEN_TTL_MIN - 1) < delta <= timedelta(minutes=RESET_TOKEN_TTL_MIN)
    # Old unused tokens were voided (the UPDATE ran before the insert).
    void_stmt = str(db.execute.await_args_list[1].args[0])
    assert void_stmt.startswith("UPDATE password_reset_tokens")


@pytest.mark.asyncio
async def test_forgot_password_unknown_email_same_response_no_email(monkeypatch):
    """Enumeration-safe: unknown email → identical 200 body, nothing stored,
    nothing sent."""
    send_spy = AsyncMock()
    monkeypatch.setattr(email_service, "send_password_reset", send_spy)
    known_db = _db([_scalar(_user()), MagicMock()])
    unknown_db = _db([_scalar(None)])

    res_unknown = await forgot_password(ForgotPasswordRequest(email="nobody@amphive.test"), unknown_db)
    unknown_db.add.assert_not_called()
    unknown_db.commit.assert_not_awaited()
    send_spy.assert_not_awaited()  # nothing sent for the unknown address

    res_known = await forgot_password(ForgotPasswordRequest(email="driver@amphive.test"), known_db)
    assert res_known == res_unknown  # byte-identical bodies


# --------------------------------------------------------------------------
# reset-password: consumption, expiry, single-use, wrong token
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_password_consumes_token_and_revokes_sessions():
    prt = _prt(user_id=7)
    consumed = MagicMock()
    consumed.rowcount = 1
    db = _db([_scalar(prt), consumed, MagicMock()])

    res = await reset_password(ResetPasswordRequest(token="tok", password="newpassword1"), db)

    assert res == {"status": "password_reset"}
    # Single-use + race-safe: consumption is a conditional UPDATE guarded on
    # used_at IS NULL — not a Python-side attribute write.
    consume_stmt = str(db.execute.await_args_list[1].args[0])
    assert consume_stmt.startswith("UPDATE password_reset_tokens")
    assert "used_at IS NULL" in consume_stmt
    # DB-side atomic epoch bump + bcrypt rehash in one UPDATE.
    stmt = str(db.execute.await_args_list[2].args[0])
    assert stmt.startswith("UPDATE users")
    assert "token_version + " in stmt
    assert "hashed_password" in stmt
    db.commit.assert_awaited_once()
    # Lookup was by digest, not the raw token.
    lookup = db.execute.await_args_list[0].args[0]
    assert _hash_reset_token("tok") in str(lookup.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_reset_password_concurrent_consumption_loses_race_gets_400():
    """Two concurrent submissions both pass the SELECT check; the loser of the
    conditional UPDATE (rowcount 0) must get the same uniform 400."""
    prt = _prt(user_id=7)
    lost = MagicMock()
    lost.rowcount = 0
    db = _db([_scalar(prt), lost])
    with pytest.raises(HTTPException) as exc:
        await reset_password(ResetPasswordRequest(token="tok", password="newpassword1"), db)
    assert exc.value.status_code == 400
    assert "invalid or expired" in exc.value.detail.lower()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prt", [
    None,                                     # wrong/unknown token
    _prt(expires_in_min=-1),                  # expired
    _prt(used_at=_now() - timedelta(minutes=1)),  # already consumed
], ids=["wrong-token", "expired", "already-used"])
async def test_reset_password_rejects_bad_tokens_uniformly(prt):
    db = _db([_scalar(prt)])
    with pytest.raises(HTTPException) as exc:
        await reset_password(ResetPasswordRequest(token="tok", password="newpassword1"), db)
    assert exc.value.status_code == 400
    assert "invalid or expired" in exc.value.detail.lower()
    db.commit.assert_not_awaited()


def test_reset_password_enforces_registration_password_rule():
    """Same 8-72 rule as RegisterRequest — pydantic rejects out-of-range."""
    with pytest.raises(Exception):
        ResetPasswordRequest(token="tok", password="short")
    with pytest.raises(Exception):
        ResetPasswordRequest(token="tok", password="x" * 73)
    assert ResetPasswordRequest(token="tok", password="x" * 8).password == "x" * 8


# --------------------------------------------------------------------------
# Rate limiting + email fallback
# --------------------------------------------------------------------------

def test_password_reset_rate_limiters_block_past_the_window():
    """Same sliding-window mechanism as login/register (defaults 5/3600 and
    10/3600, env-overridable via FORGOT_PASSWORD_RATE_LIMIT /
    RESET_PASSWORD_RATE_LIMIT)."""
    fake_now = [0.0]
    limiter = SlidingWindowRateLimiter(5, 3600, clock=lambda: fake_now[0])
    for _ in range(5):
        assert limiter.check("1.2.3.4") is None
    assert limiter.check("1.2.3.4") > 0        # 6th blocked
    assert limiter.check("5.6.7.8") is None    # other IPs unaffected
    fake_now[0] = 3601.0
    assert limiter.check("1.2.3.4") is None    # window slid


def test_endpoints_are_rate_limit_wrapped():
    from backend.routers import auth as auth_router
    paths = {
        r.path: r for r in auth_router.router.routes
        if r.path in ("/api/auth/forgot-password", "/api/auth/reset-password")
    }
    assert len(paths) == 2
    for route in paths.values():
        assert route.dependencies, f"{route.path} missing rate-limit dependency"


@pytest.mark.asyncio
async def test_email_console_fallback_logs_link_at_warning(monkeypatch, caplog):
    """No SMTP_HOST → the reset link is logged at WARNING so the flow is
    testable without a provider."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with caplog.at_level("WARNING", logger="amphive.email"):
        await email_service.send_password_reset(
            "driver@amphive.test", "https://x.test/reset-password?token=abc", 30
        )
    assert any(
        "https://x.test/reset-password?token=abc" in r.getMessage()
        and r.levelname == "WARNING"
        for r in caplog.records
    )
