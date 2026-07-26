"""
Tests for POST /api/auth/login and expired-token rejection in
services/auth.get_current_user.

Mocked-db pattern follows backend/tests/test_token_revocation.py.
"""
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


def _user(user_id=1, email="driver@amphive.test", password="correct-horse", token_version=0, role="driver"):
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
