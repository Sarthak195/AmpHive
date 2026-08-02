"""
Tests for "Sign in with Google" (routers/auth.py google_login/google_callback)
+ its GET /api/config gate.

Backend-driven authorization-code flow, no JS SDK: GET /api/auth/google/login
redirects to Google with a CSRF-nonce cookie; GET /api/auth/google/callback
validates that state, exchanges the code, verifies the ID token, then
finds-or-creates/links the account and redirects with an app JWT in the URL
fragment. No network I/O in these tests — the token exchange
(auth_router.requests.post) and ID-token verification
(auth_router.google_id_token.verify_oauth2_token) are both monkeypatched out
at the seam google_callback calls them through.

Mock-DB style mirrors test_login.py/test_password_reset.py (no database
needed) — google_callback does at most one SELECT (by email) then either an
INSERT (new account) or an UPDATE-in-place (link), same shape as
register()/login().
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

import backend.routers.auth as auth_router
from backend.routers.auth import google_callback, google_login
from backend.services.auth import decode_access_token, hash_password, verify_password

GOOGLE_ENV_VARS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI")


def _set_google_env(
    monkeypatch,
    client_id="test-client-id",
    client_secret="test-client-secret",
    redirect_uri="https://amphive.test/api/auth/google/callback",
):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", client_id)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", client_secret)
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", redirect_uri)


def _clear_google_env(monkeypatch):
    for var in GOOGLE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class _FakeRequest:
    """Just enough of fastapi.Request for google_callback: a .cookies dict
    and a .query_params mapping with .get() (mirrors test_payments.py's
    _FakeRequest for the razorpay_webhook handler)."""

    def __init__(self, cookies=None, query_params=None):
        self.cookies = cookies or {}
        self.query_params = query_params or {}


def _user(
    user_id=1,
    email="driver@amphive.test",
    role="driver",
    token_version=0,
    auth_provider="password",
    google_sub=None,
    is_disabled=False,
    hashed_password=None,
):
    u = MagicMock()
    u.id = user_id
    u.email = email
    u.full_name = "Test Driver"
    u.role = MagicMock()
    u.role.value = role
    u.coin_balance = 0.0
    u.token_version = token_version
    u.is_disabled = is_disabled
    u.auth_provider = auth_provider
    u.google_sub = google_sub
    u.hashed_password = hashed_password or hash_password("some-real-password")
    return u


def _db_returning(user):
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.refresh = AsyncMock()
    return db


def _mock_token_exchange(monkeypatch, id_token="fake-id-token"):
    """Stub the blocking POST to Google's token endpoint (never actually
    called — google_callback only reads .raise_for_status()/.json())."""
    class _FakeTokenResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id_token": id_token}

    mock_post = MagicMock(return_value=_FakeTokenResponse())
    monkeypatch.setattr(auth_router.requests, "post", mock_post)
    return mock_post


def _mock_verify_claims(monkeypatch, claims):
    """Stub ID-token verification (never fetches Google's JWKS)."""
    monkeypatch.setattr(
        auth_router.google_id_token, "verify_oauth2_token", MagicMock(return_value=claims)
    )


def _claims(email="newdriver@amphive.test", sub="google-sub-1", email_verified=True, name="New Driver"):
    return {"email": email, "sub": sub, "email_verified": email_verified, "name": name}


# --------------------------------------------------------------------------
# Unset config: feature hidden everywhere
# --------------------------------------------------------------------------

def test_config_flag_false_when_unconfigured(monkeypatch):
    # Import BEFORE clearing: backend.main's import-time load_dotenv() would
    # otherwise re-inject GOOGLE_CLIENT_ID from a developer's local .env
    # right after the delenv (public_config reads os.getenv per call).
    from backend.main import public_config

    _clear_google_env(monkeypatch)
    assert public_config()["google_login_enabled"] is False


def test_config_flag_true_when_client_id_set(monkeypatch):
    _clear_google_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    from backend.main import public_config
    assert public_config()["google_login_enabled"] is True


@pytest.mark.asyncio
async def test_google_login_503_when_unconfigured(monkeypatch):
    _clear_google_env(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await google_login()
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_google_callback_503_when_unconfigured(monkeypatch):
    _clear_google_env(monkeypatch)
    resp = await google_callback(_FakeRequest(), AsyncMock())
    assert resp.status_code == 503


# --------------------------------------------------------------------------
# google_login: redirect + state cookie
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_google_login_redirects_to_google_with_state_cookie(monkeypatch):
    _set_google_env(monkeypatch, client_id="cid123", redirect_uri="https://amphive.test/api/auth/google/callback")

    resp = await google_login()

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid123" in location
    assert "redirect_uri=https%3A%2F%2Famphive.test%2Fapi%2Fauth%2Fgoogle%2Fcallback" in location
    assert "scope=openid+email+profile" in location
    assert "state=" in location

    set_cookie = resp.headers["set-cookie"]
    assert "google_oauth_state=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "Max-Age=600" in set_cookie


# --------------------------------------------------------------------------
# google_callback: CSRF state check
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_state_mismatch_rejected(monkeypatch):
    _set_google_env(monkeypatch)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "different", "code": "x"})

    resp = await google_callback(req, AsyncMock())

    assert resp.status_code == 400
    assert "set-cookie" in resp.headers  # cookie cleared either way
    assert "google_oauth_state=" in resp.headers["set-cookie"]


@pytest.mark.asyncio
async def test_callback_missing_state_cookie_rejected(monkeypatch):
    _set_google_env(monkeypatch)
    req = _FakeRequest(cookies={}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, AsyncMock())

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_missing_state_query_rejected(monkeypatch):
    _set_google_env(monkeypatch)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"code": "x"})

    resp = await google_callback(req, AsyncMock())

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_missing_code_rejected(monkeypatch):
    """User hit "Cancel" on Google's consent screen: state matches (Google
    echoes it back even on ?error=access_denied) but there's no code."""
    _set_google_env(monkeypatch)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc"})

    resp = await google_callback(req, AsyncMock())

    assert resp.status_code == 400


# --------------------------------------------------------------------------
# google_callback: ID-token verification
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_unverified_email_rejected(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email_verified=False))
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, AsyncMock())

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_token_exchange_failure_maps_to_400_not_500(monkeypatch):
    _set_google_env(monkeypatch)

    def _raise(*a, **kw):
        raise ConnectionError("boom")

    monkeypatch.setattr(auth_router.requests, "post", _raise)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, AsyncMock())

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_id_token_verification_failure_maps_to_400_not_500(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)

    def _raise(*a, **kw):
        raise ValueError("bad signature")

    monkeypatch.setattr(auth_router.google_id_token, "verify_oauth2_token", _raise)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, AsyncMock())

    assert resp.status_code == 400


# --------------------------------------------------------------------------
# google_callback: account creation
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_creates_new_driver_account(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email="newdriver@amphive.test", sub="google-sub-1", name="New Driver"))
    db = _db_returning(None)  # no existing account
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://amphive.app/auth/google/callback#token=")
    db.commit.assert_awaited_once()

    created = db.add.call_args.args[0]
    assert created.email == "newdriver@amphive.test"
    assert created.auth_provider == "google"
    assert created.google_sub == "google-sub-1"
    assert created.full_name == "New Driver"
    # Unusable password: services/auth._DUMMY_PASSWORD_HASH trick — a random
    # bcrypt hash nothing will ever verify against.
    assert not verify_password("password123", created.hashed_password)
    assert not verify_password("", created.hashed_password)


@pytest.mark.asyncio
async def test_callback_new_account_role_is_driver(monkeypatch):
    from backend.database.models import UserRole

    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims())
    db = _db_returning(None)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    await google_callback(req, db)

    created = db.add.call_args.args[0]
    assert created.role == UserRole.DRIVER


@pytest.mark.asyncio
async def test_callback_new_account_full_name_falls_back_to_email_local_part(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email="janedoe@amphive.test", name=None))
    db = _db_returning(None)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    await google_callback(req, db)

    created = db.add.call_args.args[0]
    assert created.full_name == "janedoe"


@pytest.mark.asyncio
async def test_callback_new_account_duplicate_race_maps_to_400(monkeypatch):
    """Concurrent signup with the same email slips past the SELECT and hits
    the unique index at commit — same pattern as /api/auth/register."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims())
    db = _db_returning(None)
    db.commit = AsyncMock(side_effect=IntegrityError("INSERT ...", {}, Exception("duplicate key")))
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    assert resp.status_code == 400
    db.rollback.assert_awaited_once()


# --------------------------------------------------------------------------
# google_callback: linking an existing password account
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_links_google_to_existing_password_account(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email="driver@amphive.test", sub="google-sub-2"))
    existing = _user(email="driver@amphive.test", auth_provider="password", google_sub=None)
    db = _db_returning(existing)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    assert resp.status_code == 302
    assert existing.google_sub == "google-sub-2"
    # Linking adds a sign-in method — it doesn't rewrite how the account
    # originated.
    assert existing.auth_provider == "password"
    db.commit.assert_awaited_once()
    db.add.assert_not_called()  # no new row — this is an UPDATE, not an INSERT


@pytest.mark.asyncio
async def test_callback_link_race_maps_to_400(monkeypatch):
    """The google_sub got linked to a DIFFERENT account in the window between
    our SELECT and this commit — the partial unique index is the
    authoritative guard."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(sub="google-sub-2"))
    existing = _user(auth_provider="password", google_sub=None)
    db = _db_returning(existing)
    db.commit = AsyncMock(side_effect=IntegrityError("UPDATE ...", {}, Exception("duplicate key")))
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    assert resp.status_code == 400
    db.rollback.assert_awaited_once()


# --------------------------------------------------------------------------
# google_callback: already-linked account
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_matching_sub_signs_in_without_mutation(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(sub="google-sub-3"))
    existing = _user(auth_provider="google", google_sub="google-sub-3")
    db = _db_returning(existing)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://amphive.app/auth/google/callback#token=")
    db.commit.assert_not_awaited()  # nothing to persist — pure sign-in


@pytest.mark.asyncio
async def test_callback_sub_mismatch_rejected_403(monkeypatch):
    """The email is already linked to a DIFFERENT Google account than the
    one that just signed in — never silently switch it."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(sub="attacker-sub"))
    existing = _user(auth_provider="google", google_sub="original-sub")
    db = _db_returning(existing)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    assert resp.status_code == 403
    assert existing.google_sub == "original-sub"  # never overwritten


# --------------------------------------------------------------------------
# google_callback: admin kill switch
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_disabled_account_rejected_403(monkeypatch):
    """An admin-disabled account must not be able to bypass disablement by
    signing in with Google — same kill switch as password login."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(sub="google-sub-4"))
    existing = _user(auth_provider="google", google_sub="google-sub-4", is_disabled=True)
    db = _db_returning(existing)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    assert resp.status_code == 403
    assert b"account_disabled" in resp.body


# --------------------------------------------------------------------------
# Google-only accounts and the (unchanged) password-login route
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_google_only_account_password_login_fails_cleanly():
    """A Google-only account's hashed_password is an unusable random hash
    (the _DUMMY_PASSWORD_HASH trick) — /api/auth/login must refuse ANY
    password for it with the normal 401, never a 500, and with zero special
    casing in the login route itself."""
    import secrets as secrets_module

    from backend.routers.auth import login
    from backend.schemas import LoginRequest

    google_hash = hash_password(secrets_module.token_urlsafe(32))
    user = _user(email="googleonly@amphive.test", auth_provider="google", google_sub="sub-x",
                 hashed_password=google_hash)
    db = _db_returning(user)

    for attempted_password in ("password123", "", google_hash, "correct horse battery staple"):
        with pytest.raises(HTTPException) as exc:
            await login(LoginRequest(email=user.email, password=attempted_password), db)
        assert exc.value.status_code == 401


def test_google_only_dummy_hash_never_verifies():
    """Sanity-check the unusable-hash trick in isolation, independent of the
    login route: hash_password(random) must not verify against anything a
    human would plausibly type, nor against its own source randomness
    reused as a "guess"."""
    import secrets as secrets_module

    random_hash = hash_password(secrets_module.token_urlsafe(32))
    assert not verify_password("", random_hash)
    assert not verify_password("password", random_hash)
    assert not verify_password("12345678", random_hash)


# --------------------------------------------------------------------------
# Redirect target + token shape
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_success_redirect_carries_valid_jwt_in_fragment(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(sub="google-sub-5"))
    existing = _user(user_id=42, email="driver@amphive.test", role="driver", token_version=3,
                      auth_provider="google", google_sub="google-sub-5")
    db = _db_returning(existing)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    location = resp.headers["location"]
    assert "#token=" in location
    assert "?token=" not in location  # fragment, never a query string
    token = location.split("#token=", 1)[1]
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "42"
    assert payload["role"] == "driver"
    assert payload["tv"] == 3
    # Single-use state cookie cleared on the success path too.
    assert "google_oauth_state=" in resp.headers["set-cookie"]
