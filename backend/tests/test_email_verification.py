"""
Tests for the email-verification-at-registration flow.

POST /api/auth/register now creates an UNVERIFIED account and emails a
single-use, time-boxed token (only the SHA-256 digest is stored in
email_verification_tokens) — it no longer auto-logs-in / returns a JWT.
POST /api/auth/verify-email consumes the token (flips users.email_verified,
bumps token_version, issues a JWT). POST /api/auth/resend-verification always
answers the same generic 200 — no account enumeration — and is capped per
submitted email. POST /api/auth/login 403s an unverified account (covered in
test_login.py).

Mock-DB style mirrors test_password_reset.py (no database needed).
"""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import backend.routers.auth as auth_router
from backend.routers.auth import (
    _RESEND_VERIFICATION_RESPONSE,
    EMAIL_VERIFICATION_TTL_MIN,
    _hash_email_verification_token,
    register,
    resend_verification,
    verify_email,
)
from backend.schemas import (
    AuthResponse,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    VerifyEmailRequest,
)
from backend.services import email as email_service
from backend.services.auth import decode_access_token
from backend.services.rate_limit import SlidingWindowRateLimiter


def _now():
    return datetime.now(timezone.utc)


def _scalar(value):
    """A result whose scalar_one()/scalar_one_or_none() both yield `value`."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _db(execute_results):
    """AsyncMock db whose successive execute() calls return the given results."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(execute_results))
    db.add = MagicMock()
    db.refresh = AsyncMock()
    return db


def _evt(evt_id=1, user_id=7, expires_in_min=EMAIL_VERIFICATION_TTL_MIN, used_at=None):
    evt = MagicMock()
    evt.id = evt_id
    evt.user_id = user_id
    evt.expires_at = _now() + timedelta(minutes=expires_in_min)
    evt.used_at = used_at
    return evt


def _user(user_id=7, email="newdriver@amphive.test", token_version=1,
          role="driver", email_verified=False):
    u = MagicMock()
    u.id = user_id
    u.email = email
    u.full_name = "New Driver"
    u.role = MagicMock()
    u.role.value = role
    u.coin_balance = 0.0
    u.token_version = token_version
    u.email_verified = email_verified
    return u


@pytest.fixture(autouse=True)
def _reset_resend_email_limiter():
    """The per-email resend limiter is a module global — clear its state around
    every test so buckets never leak (and can't tip a test into a spurious
    429)."""
    auth_router.resend_verification_email_rate_limiter._hits.clear()
    yield
    auth_router.resend_verification_email_rate_limiter._hits.clear()


# --------------------------------------------------------------------------
# register: creates UNVERIFIED, no JWT, a token row, an email attempt
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_creates_unverified_no_token_and_issues_verification(monkeypatch):
    sent = {}

    async def fake_send(to_addr, verify_link):
        sent.update(to=to_addr, link=verify_link)

    monkeypatch.setattr(email_service, "send_email_verification", fake_send)
    # execute #1: duplicate-check lookup (None); execute #2: void outstanding
    # tokens (UPDATE) inside _issue_email_verification.
    db = _db([_scalar(None), MagicMock()])

    # RegisterRequest uses pydantic EmailStr, which rejects reserved TLDs like
    # `.test` (the @amphive.test gotcha, CI 2026-07-21) — use a real TLD here.
    res = await register(
        RegisterRequest(email="newdriver@example.com", password="a-valid-pw", full_name="New Driver"),
        db,
    )
    await asyncio.sleep(0)  # let the fire-and-forget email task run

    # Response is the new no-JWT shape — never AuthResponse.
    assert isinstance(res, RegisterResponse)
    assert not isinstance(res, AuthResponse)
    assert res.status == "verification_sent"
    assert res.email == "newdriver@example.com"
    assert not hasattr(res, "token")

    # The stored User row is UNVERIFIED.
    created_user = db.add.call_args_list[0].args[0]
    assert created_user.email_verified is False

    # A verification token row was stored holding the SHA-256 of the raw token
    # in the emailed link — never the raw token itself.
    token_row = db.add.call_args_list[1].args[0]
    assert sent["to"] == "newdriver@example.com"
    assert "/verify-email?token=" in sent["link"]
    raw_token = sent["link"].split("token=", 1)[1]
    assert token_row.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token not in token_row.token_hash  # digest, not the raw token
    # Time-boxed: expiry ≈ now + TTL.
    delta = token_row.expires_at - _now()
    assert timedelta(minutes=EMAIL_VERIFICATION_TTL_MIN - 1) < delta <= timedelta(minutes=EMAIL_VERIFICATION_TTL_MIN)


@pytest.mark.asyncio
async def test_register_duplicate_email_still_400_before_any_token(monkeypatch):
    send_spy = AsyncMock()
    monkeypatch.setattr(email_service, "send_email_verification", send_spy)
    db = _db([_scalar(_user())])  # duplicate found on the lookup

    with pytest.raises(HTTPException) as exc:
        await register(
            RegisterRequest(email="taken@example.com", password="a-valid-pw", full_name="X"),
            db,
        )
    assert exc.value.status_code == 400
    assert "already exists" in exc.value.detail
    db.add.assert_not_called()
    send_spy.assert_not_awaited()


# --------------------------------------------------------------------------
# verify-email: happy path + uniform rejection of bad tokens
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_email_consumes_token_marks_verified_and_logs_in():
    evt = _evt(user_id=7)
    consumed = MagicMock()
    consumed.rowcount = 1
    verified_user = _user(user_id=7, token_version=1, email_verified=True)
    # execute #1 token lookup, #2 consume UPDATE, #3 user UPDATE, #4 reload user.
    db = _db([_scalar(evt), consumed, MagicMock(), _scalar(verified_user)])

    res = await verify_email(VerifyEmailRequest(token="tok"), db)

    # Returns an AuthResponse (the click logs the user in).
    assert isinstance(res, AuthResponse)
    payload = decode_access_token(res.token)
    assert payload is not None
    assert payload["sub"] == "7"
    assert payload["tv"] == 1  # carries the bumped epoch

    # Single-use, race-safe: consumption is a conditional UPDATE guarded on
    # used_at IS NULL.
    consume_stmt = str(db.execute.await_args_list[1].args[0])
    assert consume_stmt.startswith("UPDATE email_verification_tokens")
    assert "used_at IS NULL" in consume_stmt
    # The user UPDATE flips email_verified and bumps token_version.
    user_stmt = str(db.execute.await_args_list[2].args[0])
    assert user_stmt.startswith("UPDATE users")
    assert "email_verified" in user_stmt
    assert "token_version + " in user_stmt
    db.commit.assert_awaited_once()
    # Lookup was by digest, not the raw token.
    lookup = db.execute.await_args_list[0].args[0]
    assert _hash_email_verification_token("tok") in str(
        lookup.compile(compile_kwargs={"literal_binds": True})
    )


@pytest.mark.asyncio
async def test_verify_email_concurrent_consumption_loses_race_gets_400():
    """Two concurrent submissions both pass the SELECT; the loser of the
    conditional UPDATE (rowcount 0) gets the uniform 400."""
    evt = _evt(user_id=7)
    lost = MagicMock()
    lost.rowcount = 0
    db = _db([_scalar(evt), lost])

    with pytest.raises(HTTPException) as exc:
        await verify_email(VerifyEmailRequest(token="tok"), db)
    assert exc.value.status_code == 400
    assert "invalid or expired" in exc.value.detail.lower()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("evt", [
    None,                                        # wrong/unknown token
    _evt(expires_in_min=-1),                     # expired
    _evt(used_at=_now() - timedelta(minutes=1)),  # already consumed
], ids=["wrong-token", "expired", "already-used"])
async def test_verify_email_rejects_bad_tokens_uniformly(evt):
    db = _db([_scalar(evt)])
    with pytest.raises(HTTPException) as exc:
        await verify_email(VerifyEmailRequest(token="tok"), db)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid or expired verification link."
    db.commit.assert_not_awaited()


# --------------------------------------------------------------------------
# resend-verification: generic 200, enumeration-safe, per-email cap
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resend_verification_existing_unverified_issues_new_token(monkeypatch):
    send_spy = AsyncMock()
    monkeypatch.setattr(email_service, "send_email_verification", send_spy)
    user = _user(email="unverified@amphive.test", email_verified=False)
    db = _db([_scalar(user), MagicMock()])  # lookup, then void UPDATE

    res = await resend_verification(ResendVerificationRequest(email="unverified@amphive.test"), db)
    await asyncio.sleep(0)

    assert res == _RESEND_VERIFICATION_RESPONSE == {"status": "ok"}
    db.add.assert_called_once()  # a fresh token row
    send_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_resend_verification_generic_200_for_all_account_states(monkeypatch):
    """Same body for existing-unverified, already-verified, and non-existent —
    and no email/token for the latter two (no enumeration signal)."""
    send_spy = AsyncMock()
    monkeypatch.setattr(email_service, "send_email_verification", send_spy)

    # already verified: only the lookup runs, nothing issued.
    verified_db = _db([_scalar(_user(email_verified=True))])
    res_verified = await resend_verification(
        ResendVerificationRequest(email="verified@amphive.test"), verified_db
    )
    verified_db.add.assert_not_called()

    # non-existent: only the lookup runs, nothing issued.
    ghost_db = _db([_scalar(None)])
    res_ghost = await resend_verification(
        ResendVerificationRequest(email="ghost@amphive.test"), ghost_db
    )
    ghost_db.add.assert_not_called()

    # existing-unverified: same body.
    unverified_db = _db([_scalar(_user(email_verified=False)), MagicMock()])
    res_unverified = await resend_verification(
        ResendVerificationRequest(email="unverified@amphive.test"), unverified_db
    )
    await asyncio.sleep(0)

    assert res_verified == res_ghost == res_unverified == {"status": "ok"}
    # Only the existing-unverified account triggered an email; verified/ghost
    # sent nothing (and added no token row, asserted above) — no enumeration.
    assert send_spy.await_count == 1


@pytest.mark.asyncio
async def test_resend_verification_per_email_cap_trips_regardless_of_ip(monkeypatch):
    """The per-email cap is keyed on the SUBMITTED email and never reads the
    request/IP, so an IP-rotating attacker can't push more than N verification
    mails into one inbox."""
    monkeypatch.setattr(
        auth_router, "resend_verification_email_rate_limiter",
        SlidingWindowRateLimiter(3, 3600, clock=lambda: 1000.0),
    )
    monkeypatch.setattr(email_service, "send_email_verification", AsyncMock())

    for _ in range(3):  # within budget → generic 200
        db = _db([_scalar(_user(email_verified=False)), MagicMock()])
        res = await resend_verification(ResendVerificationRequest(email="victim@amphive.test"), db)
        assert res == {"status": "ok"}

    with pytest.raises(HTTPException) as exc:  # over budget → 429, same inbox
        await resend_verification(ResendVerificationRequest(email="victim@amphive.test"), _db([]))
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_resend_verification_per_email_cap_is_not_an_enumeration_oracle(monkeypatch):
    """The cap is checked BEFORE the account lookup and keyed on the submitted
    string, so it fires IDENTICALLY for an existing and a non-existing email —
    same 429, byte-identical copy — leaking nothing about account existence."""
    monkeypatch.setattr(email_service, "send_email_verification", AsyncMock())

    async def _second_call_exc(email, existing):
        monkeypatch.setattr(
            auth_router, "resend_verification_email_rate_limiter",
            SlidingWindowRateLimiter(1, 3600, clock=lambda: 1000.0),
        )
        first_db = _db([_scalar(_user(email_verified=False)), MagicMock()]) if existing else _db([_scalar(None)])
        await resend_verification(ResendVerificationRequest(email=email), first_db)  # 1 allowed
        with pytest.raises(HTTPException) as exc:  # 2nd trips the cap pre-lookup
            await resend_verification(ResendVerificationRequest(email=email), _db([]))
        return exc.value

    exist_exc = await _second_call_exc("real@amphive.test", existing=True)
    ghost_exc = await _second_call_exc("ghost@amphive.test", existing=False)

    assert exist_exc.status_code == ghost_exc.status_code == 429
    assert exist_exc.detail == ghost_exc.detail  # identical copy, no existence signal


# --------------------------------------------------------------------------
# Rate-limit wiring + email fallback
# --------------------------------------------------------------------------

def test_verify_and_resend_endpoints_are_rate_limit_wrapped():
    paths = {
        r.path: r for r in auth_router.router.routes
        if r.path in ("/api/auth/verify-email", "/api/auth/resend-verification")
    }
    assert len(paths) == 2
    for route in paths.values():
        assert route.dependencies, f"{route.path} missing rate-limit dependency"


@pytest.mark.asyncio
async def test_email_verification_console_fallback_logs_link_at_warning(monkeypatch, caplog):
    """No SMTP_HOST → the verification link is logged at WARNING so the flow is
    testable without a provider."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with caplog.at_level("WARNING", logger="amphive.email"):
        await email_service.send_email_verification(
            "newdriver@amphive.test", "https://x.test/verify-email?token=abc"
        )
    assert any(
        "https://x.test/verify-email?token=abc" in r.getMessage() and r.levelname == "WARNING"
        for r in caplog.records
    )
