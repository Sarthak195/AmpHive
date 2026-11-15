"""
Tests for JWT token revocation via the users.token_version epoch.

Every token carries the user's token_version at issue time (`tv` claim);
get_current_user rejects a request whose `tv` no longer matches the stored
epoch, and POST /api/auth/logout-everywhere bumps the epoch ("log out
everywhere"). POST /api/auth/logout bumps it too, but only for a legacy
token without a `sid` claim (per-device logout is in
test_per_device_logout.py). Legacy tokens minted before the tv claim existed
(no `tv`) are treated as epoch 0 for backward compatibility.
"""
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend.routers.auth import logout
from backend.services.auth import (
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    create_access_token,
    get_current_user,
)


def _user(user_id=1, token_version=0):
    u = MagicMock()
    u.id = user_id
    u.email = "driver@amphive.test"
    u.token_version = token_version
    u.is_disabled = False  # a bare MagicMock attr is truthy → spurious 403
    u.auth_provider = "password"
    u.created_at = None
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
    """A sid-less (pre-per-device) token's logout falls back to the global
    bump. The bump must be a DB-side atomic UPDATE (token_version + 1), not
    Python arithmetic on the auth-loaded instance: a stale identity-mapped
    epoch would collapse concurrent bumps (lost update)."""
    user = _user(token_version=4)
    result = MagicMock()
    result.scalar_one.return_value = 5  # RETURNING users.token_version
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    res = await logout(user, db, _creds(create_access_token(1, "driver", "d@x.test", 4)))

    assert res == {"status": "logged_out", "scope": "all"}
    stmt = str(db.execute.await_args.args[0])
    assert stmt.startswith("UPDATE users")
    assert "token_version + " in stmt
    assert "RETURNING users.token_version" in stmt
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_me_returns_renewed_token_at_current_epoch(monkeypatch):
    """GET /api/auth/me re-mints the JWT (sliding renewal) at the user's
    current epoch with a fresh exp, so an active device never hits the fixed
    expiry while revocation still applies to the new token."""
    from datetime import datetime, timedelta, timezone

    import backend.routers.auth as auth_router
    from backend.services.auth import JWT_EXPIRY_DAYS

    monkeypatch.setattr(auth_router, "check_and_speed_up_active_session", AsyncMock())
    monkeypatch.setattr(auth_router, "available_balance", AsyncMock(return_value=0))
    user = _user(user_id=9, token_version=4)
    user.full_name = "Driver"
    user.role.value = "driver"
    user.coin_balance = 10.0

    signed_in = int(datetime.now(timezone.utc).timestamp()) - 86400
    sent = create_access_token(9, "driver", user.email, 4, auth_time=signed_in)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=sent)

    resp = await auth_router.get_me(user=user, db=AsyncMock(), credentials=creds)

    payload = jwt.decode(resp.token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == "9" and payload["tv"] == 4 and payload["role"] == "driver"
    assert payload["auth_time"] == signed_in  # renewal keeps the original sign-in
    expected = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS)
    assert abs(payload["exp"] - expected.timestamp()) < 60


@pytest.mark.asyncio
async def test_me_stops_renewing_past_max_session_age(monkeypatch):
    """Past JWT_MAX_SESSION_DAYS since the original sign-in, /me returns no
    token, so the client keeps its current one until it expires."""
    from datetime import datetime, timezone

    import backend.routers.auth as auth_router
    from backend.services.auth import JWT_MAX_SESSION_DAYS

    monkeypatch.setattr(auth_router, "check_and_speed_up_active_session", AsyncMock())
    monkeypatch.setattr(auth_router, "available_balance", AsyncMock(return_value=0))
    user = _user(user_id=9, token_version=4)
    user.full_name = "Driver"
    user.role.value = "driver"
    user.coin_balance = 10.0
    old = int(datetime.now(timezone.utc).timestamp()) - (JWT_MAX_SESSION_DAYS + 1) * 86400
    sent = create_access_token(9, "driver", user.email, 4, auth_time=old)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=sent)

    resp = await auth_router.get_me(user=user, db=AsyncMock(), credentials=creds)

    assert resp.token is None


@pytest.mark.asyncio
async def test_me_returns_auth_provider_and_created_at(monkeypatch):
    """Account.jsx reads auth_provider (Google-only accounts skip the
    close-account password prompt) and created_at ("Member since") from /me."""
    from datetime import datetime, timezone

    import backend.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "check_and_speed_up_active_session", AsyncMock())
    monkeypatch.setattr(auth_router, "available_balance", AsyncMock(return_value=0))
    user = _user(user_id=9, token_version=4)
    user.full_name = "Driver"
    user.role.value = "driver"
    user.coin_balance = 10.0
    user.auth_provider = "google"
    user.created_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    sent = create_access_token(9, "driver", user.email, 4)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=sent)

    resp = await auth_router.get_me(user=user, db=AsyncMock(), credentials=creds)

    body = resp.model_dump(mode="json")
    assert body["auth_provider"] == "google"
    assert body["created_at"].startswith("2026-07-01")
