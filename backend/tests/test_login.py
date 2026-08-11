"""
Tests for POST /api/auth/login and expired-token rejection in
services/auth.get_current_user.

Mocked-db pattern follows backend/tests/test_token_revocation.py.
"""
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

import backend.routers.auth as auth_router
from backend.routers.auth import login
from backend.schemas import LoginRequest
from backend.services.auth import (
    _DUMMY_PASSWORD_HASH,
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    decode_access_token,
    get_current_user,
    hash_password,
    normalize_email,
    verify_password,
)
from backend.services.rate_limit import SlidingWindowRateLimiter


def _user(user_id=1, email="driver@amphive.test", password="correct-horse", token_version=0,
          role="driver", email_verified=True):
    u = MagicMock()
    u.id = user_id
    u.email = email
    u.hashed_password = hash_password(password)
    u.full_name = "Test Driver"
    u.role = MagicMock()
    u.role.value = role
    u.coin_balance = 0.0
    u.token_version = token_version
    u.is_disabled = False  # a bare MagicMock attr is truthy → spurious 403
    # Default verified (a bare MagicMock attr is truthy anyway, but be explicit
    # so the login unverified-403 gate only fires when a test asks for it).
    u.email_verified = email_verified
    return u


def _db_returning(user):
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.asyncio
async def test_login_wrong_password_rejected():
    user = _user(password="correct-horse")
    db = _db_returning(user)

    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(email=user.email, password="wrong-password"), db)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_email_rejected():
    db = _db_returning(None)

    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(email="nobody@amphive.test", password="whatever"), db)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_both_branches_run_the_password_hash_check(monkeypatch):
    """Timing side-channel defense: a nonexistent email must pay the same
    bcrypt cost as a wrong-password attempt against a real account, so
    response latency can't be used to tell the two apart. verify_password
    must be invoked on both the known-user and unknown-user paths — the
    unknown-user path against the fixed dummy hash, never short-circuited."""
    real_verify = auth_router.verify_password
    calls = []

    def spy(password, hashed):
        calls.append(hashed)
        return real_verify(password, hashed)

    monkeypatch.setattr(auth_router, "verify_password", spy)

    user = _user(password="correct-horse")
    db_known = _db_returning(user)
    with pytest.raises(HTTPException) as exc_known:
        await login(LoginRequest(email=user.email, password="wrong-password"), db_known)
    assert exc_known.value.status_code == 401

    db_unknown = _db_returning(None)
    with pytest.raises(HTTPException) as exc_unknown:
        await login(LoginRequest(email="nobody@amphive.test", password="whatever"), db_unknown)
    assert exc_unknown.value.status_code == 401

    assert len(calls) == 2, "verify_password must run on both branches"
    assert calls[0] == user.hashed_password
    assert calls[1] == _DUMMY_PASSWORD_HASH  # fixed dummy, never a real hash


@pytest.mark.asyncio
async def test_login_matches_account_regardless_of_email_case():
    """A user registered as `driver@amphive.test` must still be able to log
    in typing `Driver@AmpHive.Test` — the lookup is normalized before the
    SELECT is issued."""
    user = _user(email="driver@amphive.test", password="correct-horse")
    db = _db_returning(user)

    with patch(
        "backend.routers.auth.check_and_speed_up_active_session",
        new=AsyncMock(),
    ):
        resp = await login(
            LoginRequest(email="  Driver@AmpHive.Test  ", password="correct-horse"), db
        )

    assert resp.user["email"] == "driver@amphive.test"
    lookup_stmt = db.execute.await_args_list[0].args[0]
    assert normalize_email("  Driver@AmpHive.Test  ") in str(
        lookup_stmt.compile(compile_kwargs={"literal_binds": True})
    )


@pytest.mark.asyncio
async def test_login_success_returns_valid_token():
    user = _user(user_id=7, email="driver@amphive.test", password="correct-horse",
                 token_version=2, role="driver")
    db = _db_returning(user)

    assert verify_password("correct-horse", user.hashed_password)

    with patch(
        "backend.routers.auth.check_and_speed_up_active_session",
        new=AsyncMock(),
    ):
        resp = await login(LoginRequest(email=user.email, password="correct-horse"), db)

    payload = decode_access_token(resp.token)
    assert payload is not None
    assert payload["sub"] == str(user.id)
    assert payload["role"] == "driver"
    assert payload["tv"] == 2


# -------------------------------------------- email-verification login gate ---
# An UNVERIFIED account (a fresh signup that hasn't clicked its link) is refused
# with 403 AFTER the password check — same after-password placement as the
# disabled-account check, so it's only ever shown to the real owner (no
# verified-status oracle for a password guesser). Grandfathered (pre-feature)
# users are email_verified=True, so they're unaffected.


@pytest.mark.asyncio
async def test_login_unverified_email_rejected_403_with_correct_password():
    user = _user(password="correct-horse", email_verified=False)
    db = _db_returning(user)

    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(email=user.email, password="correct-horse"), db)

    # 403, not a token — the credential is correct but the address is unproven.
    assert exc.value.status_code == 403
    assert "verify your email" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_login_unverified_gate_is_after_password_no_oracle():
    """A WRONG password against an unverified account still gets the generic
    401, never the verify-email 403 — the gate is checked only after the
    password verifies, so it leaks no verified-status to a non-owner."""
    user = _user(password="correct-horse", email_verified=False)
    db = _db_returning(user)

    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(email=user.email, password="wrong-password"), db)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_verified_user_logs_in_normally():
    """A verified account (incl. every grandfathered pre-feature user, which
    migration 0037 set email_verified=true) logs in and gets a token."""
    user = _user(user_id=9, password="correct-horse", email_verified=True)
    db = _db_returning(user)

    with patch("backend.routers.auth.check_and_speed_up_active_session", new=AsyncMock()):
        resp = await login(LoginRequest(email=user.email, password="correct-horse"), db)

    assert resp.token
    assert decode_access_token(resp.token)["sub"] == str(user.id)


# ------------------------------- per-account FAILURE cap (targeted-lockout) ---
# The per-account login limit lives inside the handler as a FAILURE bucket, not
# as a pre-handler dependency: a correct password must never be throttled (that
# was the targeted-lockout DoS — an IP-rotating attacker flooding a victim's
# email would 429 the real owner), while distributed wrong-password brute force
# against one email is still capped.


@pytest.mark.asyncio
async def test_login_correct_password_not_locked_out_even_when_bucket_saturated(monkeypatch):
    """A correct password SUCCEEDS even after the per-account failure bucket
    for that email is already over the limit (as an IP-rotating attacker would
    leave it) — the success path never consults the bucket, it only clears it.
    This is the core targeted-lockout-DoS fix."""
    limiter = SlidingWindowRateLimiter(3, 60, clock=lambda: 1000.0)
    monkeypatch.setattr(auth_router, "login_account_rate_limiter", limiter)

    user = _user(email="driver@amphive.test", password="correct-horse")
    key = f"login:{normalize_email(user.email)}"
    # Saturate the bucket with prior failed attempts from "many IPs".
    for _ in range(10):
        limiter.record_failure(key)
    assert limiter.check(key) is not None  # bucket is over the limit

    db = _db_returning(user)
    with patch("backend.routers.auth.check_and_speed_up_active_session", new=AsyncMock()):
        resp = await login(LoginRequest(email=user.email, password="correct-horse"), db)

    assert resp.token  # logged in despite the saturated bucket — no lockout
    assert key not in limiter._hits  # success cleared the account's failures


@pytest.mark.asyncio
async def test_login_wrong_password_429s_after_account_threshold(monkeypatch):
    """Distributed brute force (wrong passwords) against ONE email is still
    capped: the first few wrong attempts get the generic 401, then the
    per-account failure bucket trips and further wrong attempts get 429 — even
    though each could arrive from a fresh IP (the per-IP limiter can't see
    that)."""
    limiter = SlidingWindowRateLimiter(3, 60, clock=lambda: 1000.0)
    monkeypatch.setattr(auth_router, "login_account_rate_limiter", limiter)

    user = _user(email="driver@amphive.test", password="correct-horse")
    db = _db_returning(user)

    for _ in range(3):  # within budget → generic 401
        with pytest.raises(HTTPException) as exc:
            await login(LoginRequest(email=user.email, password="wrong"), db)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid email or password."

    with pytest.raises(HTTPException) as exc:  # over budget → 429
        await login(LoginRequest(email=user.email, password="wrong"), db)
    assert exc.value.status_code == 429
    assert re.match(r"^Too many login attempts\. Try again in \d+ s\.$", exc.value.detail)
    assert int(exc.value.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_login_failure_cap_is_not_an_enumeration_oracle(monkeypatch):
    """The failure cap fires identically for a NON-existent email: same 429,
    same generic copy, no hint that the address is unknown — so it adds no
    account-enumeration signal beyond the pre-existing generic 401."""
    limiter = SlidingWindowRateLimiter(3, 60, clock=lambda: 1000.0)
    monkeypatch.setattr(auth_router, "login_account_rate_limiter", limiter)

    db = _db_returning(None)  # no such account
    for _ in range(3):
        with pytest.raises(HTTPException) as exc:
            await login(LoginRequest(email="nobody@amphive.test", password="x"), db)
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(email="nobody@amphive.test", password="x"), db)
    assert exc.value.status_code == 429
    assert re.match(r"^Too many login attempts\. Try again in \d+ s\.$", exc.value.detail)
    assert "nobody" not in exc.value.detail and "amphive.test" not in exc.value.detail


@pytest.mark.asyncio
async def test_expired_token_rejected():
    now = datetime.now(timezone.utc)
    expired = jwt.encode(
        {"sub": "1", "role": "driver", "email": "d@x.test", "tv": 0,
         "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
        JWT_SECRET_KEY, algorithm=JWT_ALGORITHM,
    )
    db = _db_returning(_user(token_version=0))

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_creds(expired), db)

    assert exc.value.status_code == 401
