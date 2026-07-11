"""
Tests for JWT token revocation via the users.token_version epoch.

Every token carries the user's token_version at issue time (`tv` claim);
get_current_user rejects a request whose `tv` no longer matches the stored
epoch, and POST /api/auth/logout bumps the epoch ("log out everywhere").
Legacy tokens minted before the claim existed (no `tv`) are treated as
epoch 0 for backward compatibility.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from backend.routers.auth import logout
from backend.services import auth as auth_service
from backend.services.auth import (
    JWT_ALGORITHM, JWT_SECRET_KEY, create_access_token, get_current_user,
)


def _user(user_id=1, token_version=0):
    u = MagicMock()
    u.id = user_id
    u.email = "driver@amphive.test"
    u.token_version = token_version
    return u


def _db_returning(user):
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_token_embeds_tv_claim():
    token = create_access_token(1, "driver", "d@x.test", token_version=3)
    payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    assert payload["tv"] == 3


@pytest.mark.asyncio
async def test_current_tv_is_accepted():
    token = create_access_token(1, "driver", "d@x.test", token_version=5)
    user = await get_current_user(_creds(token), _db_returning(_user(token_version=5)))
    assert user.token_version == 5


@pytest.mark.asyncio
async def test_stale_tv_is_rejected():
    """Token issued at epoch 2, but the user has since bumped to 3 (logout)."""
    token = create_access_token(1, "driver", "d@x.test", token_version=2)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_creds(token), _db_returning(_user(token_version=3)))
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_legacy_token_without_tv_treated_as_epoch_0():
    """A token minted before the tv claim existed must still work while the
    user is at epoch 0 (no forced logout on deploy)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    legacy = jwt.encode(
        {"sub": "1", "role": "driver", "email": "d@x.test",
         "iat": now, "exp": now + timedelta(days=1)},  # no tv
        JWT_SECRET_KEY, algorithm=JWT_ALGORITHM,
    )
    user = await get_current_user(_creds(legacy), _db_returning(_user(token_version=0)))
    assert user.id == 1


@pytest.mark.asyncio
async def test_legacy_token_rejected_after_first_revoke():
    """Once the user revokes (epoch -> 1), the tv-less legacy token dies too."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    legacy = jwt.encode(
        {"sub": "1", "role": "driver", "email": "d@x.test",
         "iat": now, "exp": now + timedelta(days=1)},
        JWT_SECRET_KEY, algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_creds(legacy), _db_returning(_user(token_version=1)))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_logout_bumps_token_version():
    """The bump must be a DB-side atomic UPDATE (token_version + 1), not
    Python arithmetic on the auth-loaded instance — a stale identity-mapped
    epoch would collapse concurrent bumps (lost update)."""
    user = _user(token_version=4)
    result = MagicMock()
    result.scalar_one.return_value = 5  # RETURNING users.token_version
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    res = await logout(user, db)

    assert res == {"status": "logged_out"}
    stmt = str(db.execute.await_args.args[0])
    assert stmt.startswith("UPDATE users")
    assert "token_version + " in stmt
    assert "RETURNING users.token_version" in stmt
    db.commit.assert_awaited_once()
