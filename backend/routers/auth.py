"""
Auth routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import google.auth.transport.requests as google_auth_requests
import google.oauth2.id_token as google_id_token
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    PasswordResetToken,
    User,
    UserRole,
)
from backend.schemas import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    UserResponse,
)
from backend.services import email as email_service
from backend.services.auth import (
    _DUMMY_PASSWORD_HASH,
    create_access_token,
    get_current_user,
    hash_password,
    normalize_email,
    verify_password,
)
from backend.services.rate_limit import (
    forgot_password_rate_limiter,
    login_rate_limiter,
    rate_limit_dependency,
    register_rate_limiter,
    reset_password_rate_limiter,
)
from backend.services.session_lifecycle import (
    check_and_speed_up_active_session,
)
from backend.services.wallet import available_balance

logger = logging.getLogger("amphive.api")
router = APIRouter()

# ===========================================================================
# Authentication Endpoints
# ===========================================================================

@router.post(
    "/api/auth/register",
    response_model=AuthResponse,
    dependencies=[Depends(rate_limit_dependency(register_rate_limiter, "registration"))],
)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    Register a new driver account.
    Creates the user with a hashed password and returns a JWT token.
    New users start with 0 coin balance and the 'driver' role.
    Rate-limited per client IP (REGISTER_RATE_LIMIT) against bulk
    account creation / email enumeration.
    """
    # Canonicalize (trim + lowercase) so `Driver@x.com` and `driver@x.com`
    # are the same account — both for the duplicate check below and for what
    # gets stored.
    email = normalize_email(req.email)

    # Check if email already exists (fast path for a clean error message; the
    # unique index is the real guard — a concurrent duplicate slips past this
    # SELECT and must be caught at commit).
    result = await db.execute(select(User).where(func.lower(User.email) == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    # Create the user with hashed password
    user = User(
        email=email,
        hashed_password=hash_password(req.password),
        full_name=req.full_name,
        role=UserRole.DRIVER,
        coin_balance=0.0,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race to a concurrent registration with the same email.
        await db.rollback()
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    await db.refresh(user)

    # Generate JWT token
    token = create_access_token(user.id, user.role.value, user.email, user.token_version)
    logger.info(
        "New user registered",
        extra={"user_id": user.id, "email": user.email},
    )

    return AuthResponse(
        token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name,
              "role": user.role.value, "coin_balance": user.coin_balance},
    )


@router.post(
    "/api/auth/login",
    response_model=AuthResponse,
    dependencies=[Depends(rate_limit_dependency(login_rate_limiter, "login"))],
)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Authenticate a user with email and password.
    Returns a JWT token on success.
    Rate-limited per client IP (LOGIN_RATE_LIMIT) against brute force;
    attempts count regardless of outcome.
    Email lookup is case-insensitive (canonicalized to lowercase — see
    normalize_email) so an account registered as `Driver@x.com` still
    matches a login attempt for `driver@x.com`.
    """
    email = normalize_email(req.email)
    result = await db.execute(select(User).where(func.lower(User.email) == email))
    user = result.scalar_one_or_none()

    if user is not None:
        password_ok = verify_password(req.password, user.hashed_password)
    else:
        # Timing side-channel defense: a nonexistent email must not return
        # faster than a wrong-password attempt against a real one. Run the
        # same bcrypt-cost verification against a fixed dummy hash so this
        # branch pays the same cost as the branch above.
        verify_password(req.password, _DUMMY_PASSWORD_HASH)
        password_ok = False

    if not user or not password_ok:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    # [Admin] Disabled accounts can't sign in. Checked AFTER the password so
    # this response is only ever shown to the account's real owner (no
    # disabled-account oracle for someone guessing passwords). The machine
    # detail is deliberate — the frontend maps it to friendly copy.
    if user.is_disabled:
        raise HTTPException(status_code=403, detail="account_disabled")

    token = create_access_token(user.id, user.role.value, user.email, user.token_version)
    logger.info(
        "User logged in",
        extra={"user_id": user.id, "email": user.email},
    )

    await check_and_speed_up_active_session(db, user.id)

    return AuthResponse(
        token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name,
              "role": user.role.value, "coin_balance": user.coin_balance},
    )


@router.get("/api/auth/me", response_model=UserResponse)
async def get_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the current authenticated user's profile.
    Used by the frontend on app load to restore the session from a stored JWT.
    """
    await check_and_speed_up_active_session(db, user.id)

    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        coin_balance=user.coin_balance,
        # [Auth holds] coin_balance minus what this user's OTHER active
        # sessions already hold (services/wallet.py available_balance) — the
        # figure a NEW session-start would actually be sized against.
        # Additive field: coin_balance is untouched for existing clients.
        available_balance=float(await available_balance(db, user.id)),
    )


@router.post("/api/auth/logout")
async def logout(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Revoke every outstanding token for this user by bumping token_version
    (server-side "log out everywhere"). The current token — and any issued on
    other devices — is rejected on its next request. The bump happens DB-side:
    incrementing `user.token_version` in Python would reuse the auth-loaded
    identity-mapped instance, whose stale epoch can collapse concurrent bumps
    (same lost-update class as the wallet — see services/wallet.py).
    """
    result = await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(token_version=User.token_version + 1)
        .returning(User.token_version)
        .execution_options(synchronize_session=False)
    )
    new_epoch = result.scalar_one()
    await db.commit()
    logger.info(
        "User logged out (tokens revoked)",
        extra={"user_id": user.id, "email": user.email, "token_version": new_epoch},
    )
    return {"status": "logged_out"}


# ===========================================================================
# "Sign in with Google" (backend-driven authorization-code flow, no JS SDK)
# ===========================================================================
#
# GET /api/auth/google/login redirects the browser to Google; Google redirects
# back to GET /api/auth/google/callback with a `code`, which this backend
# exchanges server-side and turns into the SAME app JWT the password flow
# issues (create_access_token) — handed to the frontend via a URL fragment on
# the final redirect (never hits server access logs, unlike a query string).
#
# No separate identities table: a Google-only signup gets hashed_password set
# to a random unusable hash (secrets.token_urlsafe(32) through hash_password
# — the same trick as services.auth._DUMMY_PASSWORD_HASH), so the existing
# /api/auth/login route refuses it with zero changes. A password-created
# account can additionally link a google_sub without losing its password.
#
# Unset GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_OAUTH_REDIRECT_URI ⇒ the
# feature is hidden everywhere: GET /api/config's google_login_enabled is
# false (frontend hides the button) and /google/login 503s defensively even
# if it's hit directly.

GOOGLE_STATE_COOKIE = "google_oauth_state"
GOOGLE_STATE_COOKIE_MAX_AGE = 600  # 10 minutes — this leg of the flow is fast
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _google_oauth_config():
    """Read the three Google OAuth env vars at CALL time, not import time, so
    tests can monkeypatch os.environ (mirrors services/email.py's
    frontend_origin()/SMTP env reads). Returns None — feature fully hidden —
    unless all three are set; a half-configured deploy must fail closed, not
    accept sign-ins it can't correctly redirect or verify."""
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
    if not (client_id and client_secret and redirect_uri):
        return None
    return client_id, client_secret, redirect_uri


def _google_error_response(status_code: int, detail: str) -> JSONResponse:
    """Build a callback-error response with the single-use state cookie
    cleared. Every exit out of google_callback past the state check —
    success or failure — clears the cookie the same way, so this is the one
    place that shape is defined instead of repeating .delete_cookie() at each
    call site."""
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    response.delete_cookie(GOOGLE_STATE_COOKIE)
    return response


@router.get("/api/auth/google/login")
async def google_login():
    """
    Start the Google sign-in redirect. 503s if Google OAuth isn't configured
    (mirrors payments.create_payment_order's "service not configured" 503).

    Sets `google_oauth_state`: a random CSRF nonce, httpOnly + Secure +
    SameSite=Lax, 10-minute max-age. This is the app's FIRST cookie — it is
    NOT a session mechanism (the app stays bearer-JWT-only for actual auth);
    it exists solely so google_callback can confirm the code it receives
    really came from a redirect this backend initiated, not a forged request.
    SameSite=Lax (not Strict) is required: the cookie must still be sent when
    Google's callback navigates the browser back to us, which is a top-level
    cross-site GET — Lax allows that; Strict would drop it.
    """
    config = _google_oauth_config()
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in is not configured. Contact support.",
        )
    client_id, _client_secret, redirect_uri = config

    state = secrets.token_urlsafe(24)
    authorize_url = f"{GOOGLE_AUTHORIZE_URL}?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    })

    response = RedirectResponse(url=authorize_url, status_code=302)
    response.set_cookie(
        GOOGLE_STATE_COOKIE,
        state,
        max_age=GOOGLE_STATE_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.get("/api/auth/google/callback")
async def google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Finish the Google sign-in redirect: verify the CSRF state, exchange the
    authorization code, verify the ID token, then find-or-create/link the
    AmpHive account and redirect to the frontend with a normal app JWT in the
    URL fragment (`#token=...` — a fragment never leaves the browser, so it
    never hits this server's or any proxy's access logs, unlike a query
    string).

    Every branch below returns an explicit Response (JSONResponse for errors,
    RedirectResponse on success) instead of raising HTTPException, so the
    single-use state cookie can be cleared on the SAME response that answers
    the request — raising would hand the exception middleware a fresh
    Response of its own and silently drop that cookie mutation.
    """
    config = _google_oauth_config()
    if config is None:
        # Not normally reachable (the /login leg already 503s first), but
        # config could in principle change between the two legs.
        return _google_error_response(503, "Google sign-in is not configured.")
    client_id, client_secret, redirect_uri = config

    # --- CSRF state check (constant-time; state is a secret nonce) ---------
    cookie_state = request.cookies.get(GOOGLE_STATE_COOKIE)
    query_state = request.query_params.get("state")
    if not cookie_state or not query_state or not hmac.compare_digest(cookie_state, query_state):
        return _google_error_response(
            400, "Invalid or expired sign-in attempt. Please try again."
        )

    code = request.query_params.get("code")
    if not code:
        # e.g. the user hit "Cancel" on Google's consent screen
        # (?error=access_denied&state=... with no code).
        return _google_error_response(
            400, "Google sign-in was cancelled or did not return an authorization code."
        )

    # --- Exchange the code for tokens ---------------------------------------
    # Blocking HTTP call — run off the event loop (mirrors services/email.py's
    # asyncio.to_thread(send_email, ...) pattern for smtplib).
    def _exchange_code():
        return requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )

    try:
        token_response = await asyncio.to_thread(_exchange_code)
        token_response.raise_for_status()
        token_payload = token_response.json()
    except Exception:
        logger.exception("Google token exchange failed")
        return _google_error_response(400, "Could not complete Google sign-in. Please try again.")

    id_tok = token_payload.get("id_token")
    if not id_tok:
        return _google_error_response(400, "Google did not return an identity token.")

    # --- Verify the ID token -------------------------------------------------
    # verify_oauth2_token fetches Google's JWKS to check the signature (also
    # blocking network I/O) and validates issuer/audience/expiry itself.
    try:
        claims = await asyncio.to_thread(
            google_id_token.verify_oauth2_token,
            id_tok,
            google_auth_requests.Request(),
            client_id,
        )
    except Exception:
        logger.exception("Google id_token verification failed")
        return _google_error_response(400, "Could not verify Google identity token.")

    if not claims.get("email_verified"):
        return _google_error_response(400, "Google account email is not verified.")
    email_claim = claims.get("email")
    if not email_claim:
        return _google_error_response(400, "Google did not return an email address.")
    google_sub = claims.get("sub")
    if not google_sub:
        return _google_error_response(400, "Google did not return an account identifier.")

    email = normalize_email(email_claim)
    result = await db.execute(select(User).where(func.lower(User.email) == email))
    user = result.scalar_one_or_none()

    if user is None:
        # New signup — DRIVER role, dummy unusable password hash (same trick
        # as services.auth._DUMMY_PASSWORD_HASH), no tenant (mirrors
        # /api/auth/register exactly — tenants are assigned later via
        # /api/cpo/setup, not at signup).
        full_name = claims.get("name") or email.split("@", 1)[0]
        user = User(
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            full_name=full_name,
            role=UserRole.DRIVER,
            coin_balance=0.0,
            auth_provider="google",
            google_sub=google_sub,
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            # Lost the race to a concurrent signup/link with the same email
            # (or, vanishingly unlikely, google_sub) — same pattern as
            # /api/auth/register's duplicate-email catch.
            await db.rollback()
            return _google_error_response(400, "An account with this email already exists.")
        await db.refresh(user)
        logger.info(
            "New user registered via Google",
            extra={"user_id": user.id, "email": user.email},
        )
    elif user.google_sub is None:
        # Existing password account, first-time Google link. auth_provider
        # stays 'password' — linking adds a sign-in method, it doesn't
        # rewrite how the account originated.
        user.google_sub = google_sub
        try:
            await db.commit()
        except IntegrityError:
            # This google_sub got linked to a DIFFERENT account in the
            # window between our SELECT and this commit (the partial unique
            # index is the authoritative guard) — same duplicate-race
            # pattern as the branch above.
            await db.rollback()
            return _google_error_response(
                400, "This Google account is already linked to a different AmpHive account."
            )
        await db.refresh(user)
        logger.info(
            "Linked Google identity to existing account",
            extra={"user_id": user.id, "email": user.email},
        )
    elif user.google_sub != google_sub:
        # This email is already linked to a DIFFERENT Google account than
        # the one that just signed in — never silently switch it.
        return _google_error_response(
            403, "This email is linked to a different Google account."
        )

    # [Admin] Same kill switch as password login — an admin-disabled account
    # must not be able to bypass disablement by signing in with Google.
    if user.is_disabled:
        return _google_error_response(403, "account_disabled")

    token = create_access_token(user.id, user.role.value, user.email, user.token_version)
    logger.info(
        "User logged in via Google",
        extra={"user_id": user.id, "email": user.email},
    )
    await check_and_speed_up_active_session(db, user.id)

    redirect_url = f"{email_service.frontend_origin()}/auth/google/callback#token={token}"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(GOOGLE_STATE_COOKIE)
    return response


# ===========================================================================
# Password reset ("forgot password")
# ===========================================================================

# How long an emailed reset link stays valid. Short on purpose — the token is
# a bearer credential sitting in an inbox.
RESET_TOKEN_TTL_MIN = int(os.getenv("RESET_TOKEN_TTL_MIN", "30"))

# The response body both outcomes of forgot-password share (enumeration-safe).
_FORGOT_RESPONSE = {
    "status": "ok",
    "detail": "If an account exists for that email, a password reset link has been sent.",
}


def _hash_reset_token(token: str) -> str:
    """SHA-256 hex digest — what password_reset_tokens.token_hash stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post(
    "/api/auth/forgot-password",
    dependencies=[Depends(rate_limit_dependency(forgot_password_rate_limiter, "password reset"))],
)
async def forgot_password(req: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """
    Issue a single-use, time-boxed password-reset token and email the link.

    ALWAYS returns the same generic 200, whether or not the email matches an
    account — no account enumeration (unlike /register, which legitimately
    reveals duplicates, this endpoint takes arbitrary input from anyone).
    Rate-limited per client IP (FORGOT_PASSWORD_RATE_LIMIT) since each allowed
    call with a real account triggers an outbound email.

    Only the SHA-256 digest of the token is stored (PasswordResetToken);
    outstanding unused tokens for the user are voided first, so at most one
    live link exists per account. Delivery goes through services/email.py:
    SMTP when SMTP_HOST is configured, otherwise the link is logged at
    WARNING (console fallback).

    Email lookup is case-insensitive (see normalize_email), matching
    register/login, so a user who types a different case than they
    registered with still finds their account.
    """
    email = normalize_email(req.email)
    result = await db.execute(select(User).where(func.lower(User.email) == email))
    user = result.scalar_one_or_none()
    if user is None:
        return _FORGOT_RESPONSE

    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    # Void any outstanding unused tokens — a re-request supersedes old links.
    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_reset_token(token),
        expires_at=now + timedelta(minutes=RESET_TOKEN_TTL_MIN),
    ))
    await db.commit()

    reset_link = f"{email_service.frontend_origin()}/reset-password?token={token}"
    # Fire-and-forget: awaiting SMTP here would make known-account requests
    # measurably slower than the early return above (timing enumeration
    # oracle). send_password_reset never raises (SMTP failures are logged).
    asyncio.get_running_loop().create_task(
        email_service.send_password_reset(user.email, reset_link, RESET_TOKEN_TTL_MIN)
    )
    logger.info(
        "Password reset token issued",
        extra={"user_id": user.id, "email": user.email},
    )
    return _FORGOT_RESPONSE


@router.post(
    "/api/auth/reset-password",
    dependencies=[Depends(rate_limit_dependency(reset_password_rate_limiter, "password reset"))],
)
async def reset_password(req: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """
    Consume a reset token: set the new password (same 8-72 rule as
    registration, enforced by ResetPasswordRequest) and revoke every existing
    session by bumping users.token_version (DB-side atomic increment — same
    lost-update rationale as /logout). The token row is stamped used_at, so a
    second submission of the same link fails. Unknown, expired, and
    already-used tokens all get the same 400 (no oracle on which it was).
    Rate-limited per client IP (RESET_PASSWORD_RATE_LIMIT).
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == _hash_reset_token(req.token)
        )
    )
    prt = result.scalar_one_or_none()
    if prt is None or prt.used_at is not None or prt.expires_at <= now:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset link. Please request a new one.",
        )

    # Single-use, race-safe: the conditional UPDATE is the consumption point —
    # two concurrent submissions of the same token both pass the SELECT check
    # above, but only one can win this row (WHERE used_at IS NULL).
    consumed = await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.id == prt.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if consumed.rowcount == 0:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset link. Please request a new one.",
        )
    await db.execute(
        update(User)
        .where(User.id == prt.user_id)
        .values(
            hashed_password=hash_password(req.password),
            token_version=User.token_version + 1,  # revoke all sessions
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    logger.info(
        "Password reset completed (all sessions revoked)",
        extra={"user_id": prt.user_id},
    )
    return {"status": "password_reset"}


