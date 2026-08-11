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
import json
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import IntegrityError

import backend.routers.auth as auth_router
from backend.routers.auth import (
    GoogleExchangeRequest,
    google_callback,
    google_exchange,
    google_login,
    login,
)
from backend.schemas import LoginRequest
from backend.services.auth import (
    create_access_token,
    decode_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from backend.services.rate_limit import SlidingWindowRateLimiter

GOOGLE_ENV_VARS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI")


@pytest.fixture(autouse=True)
def _clear_exchange_store():
    """Keep the module-global single-use code store from leaking codes across
    tests (each test that needs a code mints its own via a real callback)."""
    auth_router._google_exchange_store.clear()
    yield
    auth_router._google_exchange_store.clear()


def _set_cookie_value(response, name):
    """Pull one Set-Cookie value by name off a Starlette response — the success
    callback emits SEVERAL Set-Cookie headers (clear google_oauth_state + set
    google_oauth_nonce), so index-by-name rather than taking the first."""
    for key, value in response.raw_headers:
        if key.decode().lower() == "set-cookie":
            jar = SimpleCookie()
            jar.load(value.decode())
            if name in jar and jar[name].value:
                return jar[name].value
    return None


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


async def _run_successful_callback(monkeypatch, db, *, sub="google-sub-x", email="driver@amphive.test"):
    """Drive google_callback through to its success redirect and return
    (response, code-from-fragment, nonce-from-cookie) — the two halves the SPA
    later re-presents to /api/auth/google/exchange."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email=email, sub=sub))
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})
    resp = await google_callback(req, db)
    assert resp.status_code == 302
    code = resp.headers["location"].split("#code=", 1)[1]
    nonce = _set_cookie_value(resp, auth_router.GOOGLE_EXCHANGE_COOKIE)
    return resp, code, nonce


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
    # [M5] The fragment now carries a single-use CODE, never the raw JWT.
    assert resp.headers["location"].startswith("https://amphive.app/auth/google/callback#code=")
    assert "#token=" not in resp.headers["location"]
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
    """SECURE take-over (was: account-pre-hijacking). Linking Google to a
    pre-existing password account must EVICT the old credential, not sit
    beside it: the stored password is replaced with an unusable hash, the
    token epoch is bumped (kills prior sessions), and auth_provider flips to
    'google'. This is what stops an attacker who pre-registered the victim's
    email from keeping access after the real owner links via Google."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email="driver@amphive.test", sub="google-sub-2"))
    existing = _user(
        email="driver@amphive.test",
        auth_provider="password",
        google_sub=None,
        token_version=0,
        hashed_password=hash_password("attacker-preregistered-pw"),
    )
    db = _db_returning(existing)
    req = _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"})

    resp = await google_callback(req, db)

    assert resp.status_code == 302
    assert existing.google_sub == "google-sub-2"
    # The pre-existing password no longer verifies — it was overwritten with a
    # fresh unusable hash, so /api/auth/login can never accept it again.
    assert not verify_password("attacker-preregistered-pw", existing.hashed_password)
    # Token epoch bumped: every JWT/session minted before the link is revoked.
    assert existing.token_version == 1
    # auth_provider now reflects that Google is the only working sign-in method.
    assert existing.auth_provider == "google"
    db.commit.assert_awaited_once()
    db.add.assert_not_called()  # no new row — this is an UPDATE, not an INSERT


@pytest.mark.asyncio
async def test_callback_link_invalidates_old_password_at_login(monkeypatch):
    """After the take-over, the attacker's pre-registered password is refused
    by the real /api/auth/login handler (401) — proving the credential was
    genuinely invalidated end-to-end, not merely mutated on the ORM object."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email="driver@amphive.test", sub="google-sub-2"))
    existing = _user(
        email="driver@amphive.test",
        google_sub=None,
        token_version=0,
        hashed_password=hash_password("attacker-preregistered-pw"),
    )
    await google_callback(
        _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"}),
        _db_returning(existing),
    )

    # Isolate the shared per-account login limiter so this failed attempt
    # doesn't leak into other login tests (and vice versa).
    monkeypatch.setattr(
        auth_router, "login_account_rate_limiter", SlidingWindowRateLimiter(10, 60)
    )
    login_db = _db_returning(existing)  # the same (now taken-over) user row
    with pytest.raises(HTTPException) as exc:
        await login(
            LoginRequest(email="driver@amphive.test", password="attacker-preregistered-pw"),
            login_db,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_callback_link_bumps_token_version_revoking_prior_jwt(monkeypatch):
    """A JWT the attacker minted BEFORE the link (carrying the old epoch tv=0)
    is rejected by get_current_user once the owner links via Google (epoch
    moves to 1) — their live sessions die immediately."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email="driver@amphive.test", sub="google-sub-2"))
    existing = _user(email="driver@amphive.test", google_sub=None, token_version=0)

    pre_link_jwt = create_access_token(existing.id, existing.role.value, existing.email, 0)
    # Sanity: valid before the link.
    assert decode_access_token(pre_link_jwt)["tv"] == 0

    await google_callback(
        _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"}),
        _db_returning(existing),
    )
    assert existing.token_version == 1

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=pre_link_jwt)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(creds, _db_returning(existing))
    assert exc.value.status_code == 401  # "Session has been revoked."


@pytest.mark.asyncio
async def test_callback_link_google_sign_in_still_yields_a_working_token(monkeypatch):
    """The Google sign-in that performed the take-over must still succeed and
    hand back a working token — one carrying the NEW epoch, so get_current_user
    accepts it while rejecting the attacker's pre-link JWT (test above)."""
    _set_google_env(monkeypatch)
    _mock_token_exchange(monkeypatch)
    _mock_verify_claims(monkeypatch, _claims(email="driver@amphive.test", sub="google-sub-2"))
    existing = _user(email="driver@amphive.test", google_sub=None, token_version=0)
    db = _db_returning(existing)

    resp = await google_callback(
        _FakeRequest(cookies={"google_oauth_state": "abc"}, query_params={"state": "abc", "code": "x"}),
        db,
    )
    code = resp.headers["location"].split("#code=", 1)[1]
    nonce = _set_cookie_value(resp, auth_router.GOOGLE_EXCHANGE_COOKIE)

    exch = await google_exchange(
        GoogleExchangeRequest(code=code),
        _FakeRequest(cookies={auth_router.GOOGLE_EXCHANGE_COOKIE: nonce}),
    )
    token = json.loads(bytes(exch.body).decode())["token"]
    payload = decode_access_token(token)
    assert payload["sub"] == str(existing.id)
    assert payload["tv"] == existing.token_version == 1

    # The freshly-minted Google token is accepted by get_current_user.
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    assert await get_current_user(creds, _db_returning(existing)) is existing


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
    assert resp.headers["location"].startswith("https://amphive.app/auth/google/callback#code=")
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
# Redirect target + code/nonce shape (M5: browser-bound single-use code)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_redirect_carries_code_not_raw_jwt_and_sets_nonce_cookie(monkeypatch):
    """The callback must hand the SPA a single-use CODE in the fragment (not a
    usable JWT) and bind it to the browser with an httpOnly/Secure/Lax nonce
    cookie."""
    existing = _user(user_id=42, email="driver@amphive.test", role="driver", token_version=3,
                     auth_provider="google", google_sub="google-sub-5")
    db = _db_returning(existing)

    resp, code, nonce = await _run_successful_callback(
        monkeypatch, db, sub="google-sub-5", email="driver@amphive.test"
    )

    location = resp.headers["location"]
    assert "#code=" in location
    assert "#token=" not in location  # the raw JWT never rides in the URL anymore
    assert "?code=" not in location   # the code is a fragment, never a query string
    # The fragment value is an opaque code, NOT a decodable app JWT.
    assert decode_access_token(code) is None
    assert code

    # Nonce cookie bound the flow to this browser; state cookie cleared.
    assert nonce
    set_cookies = "; ".join(
        v.decode() for k, v in resp.raw_headers if k.decode().lower() == "set-cookie"
    )
    assert "google_oauth_nonce=" in set_cookies
    assert "HttpOnly" in set_cookies
    assert "Secure" in set_cookies
    assert "samesite=lax" in set_cookies.lower()
    assert "google_oauth_state=" in set_cookies  # single-use state cookie cleared too


# --------------------------------------------------------------------------
# google_exchange: trade the browser-bound code for the real app JWT
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exchange_returns_jwt_with_matching_nonce_and_unused_code(monkeypatch):
    existing = _user(user_id=42, email="driver@amphive.test", role="driver", token_version=3,
                     auth_provider="google", google_sub="google-sub-5")
    db = _db_returning(existing)
    _resp, code, nonce = await _run_successful_callback(monkeypatch, db, sub="google-sub-5")

    exch = _FakeRequest(cookies={auth_router.GOOGLE_EXCHANGE_COOKIE: nonce})
    result = await google_exchange(GoogleExchangeRequest(code=code), exch)

    assert result.status_code == 200
    token = json.loads(result.body)["token"]
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "42"
    assert payload["role"] == "driver"
    assert payload["tv"] == 3
    # Nonce cookie cleared once the code is spent.
    set_cookies = "; ".join(
        v.decode() for k, v in result.raw_headers if k.decode().lower() == "set-cookie"
    )
    assert "google_oauth_nonce=" in set_cookies


@pytest.mark.asyncio
async def test_exchange_replay_of_consumed_code_fails(monkeypatch):
    """Single-use: the second redemption of the same code is refused (400)."""
    db = _db_returning(_user(auth_provider="google", google_sub="google-sub-6"))
    _resp, code, nonce = await _run_successful_callback(monkeypatch, db, sub="google-sub-6")
    exch = _FakeRequest(cookies={auth_router.GOOGLE_EXCHANGE_COOKIE: nonce})

    first = await google_exchange(GoogleExchangeRequest(code=code), exch)
    assert first.status_code == 200

    second = await google_exchange(GoogleExchangeRequest(code=code), exch)
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_exchange_missing_nonce_cookie_fails(monkeypatch):
    """A code presented WITHOUT the bound nonce cookie (e.g. a planted link
    opened in a different browser) is worthless — this is the core M5 fix."""
    db = _db_returning(_user(auth_provider="google", google_sub="google-sub-7"))
    _resp, code, _nonce = await _run_successful_callback(monkeypatch, db, sub="google-sub-7")

    exch = _FakeRequest(cookies={})  # no nonce cookie
    result = await google_exchange(GoogleExchangeRequest(code=code), exch)

    assert result.status_code == 400


@pytest.mark.asyncio
async def test_exchange_mismatched_nonce_fails(monkeypatch):
    """A wrong nonce cookie can't redeem the code (and burns it — strictly
    single-use)."""
    db = _db_returning(_user(auth_provider="google", google_sub="google-sub-8"))
    _resp, code, nonce = await _run_successful_callback(monkeypatch, db, sub="google-sub-8")

    exch = _FakeRequest(cookies={auth_router.GOOGLE_EXCHANGE_COOKIE: nonce + "-tampered"})
    result = await google_exchange(GoogleExchangeRequest(code=code), exch)
    assert result.status_code == 400

    # Even the correct nonce now fails — the wrong-nonce attempt already
    # consumed the code.
    good = _FakeRequest(cookies={auth_router.GOOGLE_EXCHANGE_COOKIE: nonce})
    replay = await google_exchange(GoogleExchangeRequest(code=code), good)
    assert replay.status_code == 400


@pytest.mark.asyncio
async def test_exchange_unknown_code_fails(monkeypatch):
    exch = _FakeRequest(cookies={auth_router.GOOGLE_EXCHANGE_COOKIE: "whatever"})
    result = await google_exchange(GoogleExchangeRequest(code="never-issued"), exch)
    assert result.status_code == 400


@pytest.mark.asyncio
async def test_exchange_expired_code_fails(monkeypatch):
    """A code past its TTL is refused even with the right nonce cookie."""
    db = _db_returning(_user(auth_provider="google", google_sub="google-sub-9"))
    _resp, code, nonce = await _run_successful_callback(monkeypatch, db, sub="google-sub-9")

    # Rewind the stored entry's expiry into the past (simulates TTL lapse).
    token, stored_nonce, _exp = auth_router._google_exchange_store[code]
    auth_router._google_exchange_store[code] = (token, stored_nonce, 0.0)

    exch = _FakeRequest(cookies={auth_router.GOOGLE_EXCHANGE_COOKIE: nonce})
    result = await google_exchange(GoogleExchangeRequest(code=code), exch)
    assert result.status_code == 400
