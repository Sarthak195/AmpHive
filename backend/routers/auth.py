"""
Auth routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import google.auth.transport.requests as google_auth_requests
import google.oauth2.id_token as google_id_token
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    EmailVerificationToken,
    PasswordResetToken,
    User,
    UserRole,
)
from backend.schemas import (
    AuthResponse,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    ResetPasswordRequest,
    UserResponse,
    VerifyEmailRequest,
)
from backend.services import email as email_service
from backend.services.account_closure import (
    AccountClosureRefused,
    close_account,
)
from backend.services.audit import try_record_audit
from backend.services.auth import (
    _DUMMY_PASSWORD_HASH,
    create_access_token,
    get_current_user,
    hash_password,
    normalize_email,
    verify_password,
)
from backend.services.data_export import build_export
from backend.services.rate_limit import (
    account_rate_limit_dependency,
    data_export_account_rate_limiter,
    delete_account_account_rate_limiter,
    forgot_password_email_rate_limiter,
    forgot_password_rate_limiter,
    login_account_rate_limiter,
    login_rate_limiter,
    rate_limit_dependency,
    register_rate_limiter,
    resend_verification_email_rate_limiter,
    resend_verification_rate_limiter,
    reset_password_rate_limiter,
    verify_email_rate_limiter,
)
from backend.services.session_lifecycle import (
    check_and_speed_up_active_session,
)
from backend.services.wallet import available_balance

logger = logging.getLogger("amphive.api")
router = APIRouter()

# ===========================================================================
# Email verification (address ownership proven at registration)
# ===========================================================================
#
# Closes the account-PRE-hijacking class at its root: register no longer
# auto-logs-in, it creates an UNVERIFIED account and emails a single-use link;
# /api/auth/login 403s until the address is verified. Mirrors the
# password-reset token machinery exactly (SHA-256-digest-only, single-use,
# TTL-boxed, at most one live link per account).

# How long an emailed verification link stays valid. Longer than the reset TTL
# (24 h default) on purpose — a new user may not click immediately.
EMAIL_VERIFICATION_TTL_MIN = int(os.getenv("EMAIL_VERIFICATION_TTL_MIN", "1440"))


def _hash_email_verification_token(token: str) -> str:
    """SHA-256 hex digest — what email_verification_tokens.token_hash stores
    (same scheme as _hash_reset_token)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _issue_email_verification(db: AsyncSession, user: User) -> None:
    """Void the user's outstanding unused verification tokens, mint a fresh
    single-use token, persist ONLY its SHA-256 digest (EmailVerificationToken,
    EMAIL_VERIFICATION_TTL_MIN expiry), commit, and email the link.

    Mirrors forgot-password issuance: at most one live link per account, and
    the email is dispatched fire-and-forget (send_email_verification never
    raises — SMTP failures are logged) so the request never blocks on SMTP and
    resend-verification stays timing-uniform. Shared by register + resend.
    """
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    # Supersede any outstanding unused links — at most one live link per user.
    await db.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.used_at.is_(None),
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    db.add(EmailVerificationToken(
        user_id=user.id,
        token_hash=_hash_email_verification_token(token),
        expires_at=now + timedelta(minutes=EMAIL_VERIFICATION_TTL_MIN),
    ))
    await db.commit()

    verify_link = f"{email_service.frontend_origin()}/verify-email?token={token}"
    asyncio.get_running_loop().create_task(
        email_service.send_email_verification(user.email, verify_link)
    )


# ===========================================================================
# Authentication Endpoints
# ===========================================================================

@router.post(
    "/api/auth/register",
    response_model=RegisterResponse,
    dependencies=[Depends(rate_limit_dependency(register_rate_limiter, "registration"))],
)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    Register a new driver account — UNVERIFIED, and it does NOT log the user in.

    Creates the user (0 coin balance, 'driver' role, hashed password) with
    email_verified=False, then mints a single-use verification token and emails
    the link (services/email.py — SMTP if configured, else the link is logged
    at WARNING). The caller gets `{status:"verification_sent", email}` and NO
    JWT: /api/auth/login refuses the account with a 403 until the address is
    verified via /api/auth/verify-email. This is what closes the
    account-PRE-hijacking class — an unverified email can no longer hold a
    usable credential.

    Rate-limited per client IP (REGISTER_RATE_LIMIT) against bulk account
    creation / email enumeration.
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

    # Create the user with hashed password — UNVERIFIED (login is gated on it).
    user = User(
        email=email,
        hashed_password=hash_password(req.password),
        full_name=req.full_name,
        role=UserRole.DRIVER,
        coin_balance=0.0,
        email_verified=False,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race to a concurrent registration with the same email.
        await db.rollback()
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    await db.refresh(user)

    # Mint + email a single-use verification token (voids any outstanding ones
    # first — none yet for a brand-new account, but keep the pattern). No JWT is
    # issued: the user proves ownership via /api/auth/verify-email.
    await _issue_email_verification(db, user)
    logger.info(
        "New user registered (verification email sent)",
        extra={"user_id": user.id, "email": user.email},
    )

    return RegisterResponse(status="verification_sent", email=user.email)


@router.post(
    "/api/auth/verify-email",
    response_model=AuthResponse,
    dependencies=[Depends(rate_limit_dependency(verify_email_rate_limiter, "email verification"))],
)
async def verify_email(req: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    """
    Consume an email-verification token: flip users.email_verified true, stamp
    the token used_at, bump users.token_version (belt-and-braces), and issue a
    JWT so clicking the link logs the user in (returns AuthResponse).

    Unknown, expired, and already-used tokens all get the same generic 400 (no
    oracle on which it was) — same shape as reset-password. Rate-limited per
    client IP (VERIFY_EMAIL_RATE_LIMIT); the 256-bit token makes online brute
    force academic, but this matches reset-password.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == _hash_email_verification_token(req.token)
        )
    )
    evt = result.scalar_one_or_none()
    if evt is None or evt.used_at is not None or evt.expires_at <= now:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired verification link.",
        )

    # Single-use, race-safe: the conditional UPDATE is the consumption point —
    # two concurrent submissions of the same token both pass the SELECT above,
    # but only one can win this row (WHERE used_at IS NULL). Mirrors
    # reset-password exactly.
    consumed = await db.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.id == evt.id,
            EmailVerificationToken.used_at.is_(None),
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if consumed.rowcount == 0:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired verification link.",
        )
    # Mark verified + revoke any pre-verification tokens (belt-and-braces),
    # DB-side atomic epoch bump (same lost-update rationale as /logout).
    await db.execute(
        update(User)
        .where(User.id == evt.user_id)
        .values(email_verified=True, token_version=User.token_version + 1)
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    # Reload the now-verified user (fresh token_version) to mint a JWT carrying
    # the new epoch — the click logs them in.
    result = await db.execute(select(User).where(User.id == evt.user_id))
    user = result.scalar_one()
    token = create_access_token(user.id, user.role.value, user.email, user.token_version)
    logger.info(
        "Email verified (user logged in)",
        extra={"user_id": user.id, "email": user.email},
    )
    return AuthResponse(
        token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name,
              "role": user.role.value, "coin_balance": user.coin_balance},
    )


# The generic body resend-verification always returns (enumeration-safe).
_RESEND_VERIFICATION_RESPONSE = {"status": "ok"}


@router.post(
    "/api/auth/resend-verification",
    dependencies=[
        Depends(rate_limit_dependency(resend_verification_rate_limiter, "verification resend")),
    ],
)
async def resend_verification(req: ResendVerificationRequest, db: AsyncSession = Depends(get_db)):
    """
    Re-send a verification link. ALWAYS returns the same generic 200
    `{status:"ok"}`, whether or not the email matches an account and whether or
    not it's already verified — no account enumeration.

    Rate-limited per client IP (RESEND_VERIFICATION_RATE_LIMIT) since each
    allowed call with a real unverified account triggers an outbound email, AND
    per submitted email (RESEND_VERIFICATION_EMAIL_RATE_LIMIT) so an attacker
    with an IP pool can't mailbomb one victim's inbox by rotating source
    addresses past the per-IP gate. The per-email cap is keyed on the
    normalized email and checked BEFORE the account lookup, so it fires
    identically whether or not the account exists — no enumeration oracle
    (mirrors forgot-password exactly).
    """
    email = normalize_email(req.email)

    retry_after = resend_verification_email_rate_limiter.check(f"resend_verify:{email}")
    if retry_after is not None:
        seconds = int(retry_after) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Too many verification requests for this email. Try again in {seconds} s.",
            headers={"Retry-After": str(seconds)},
        )

    result = await db.execute(select(User).where(func.lower(User.email) == email))
    user = result.scalar_one_or_none()
    # Only existing AND still-unverified accounts get a new link. A missing or
    # already-verified account does nothing but still returns the same 200.
    if user is not None and not user.email_verified:
        await _issue_email_verification(db, user)
        logger.info(
            "Verification email re-sent",
            extra={"user_id": user.id, "email": user.email},
        )
    return _RESEND_VERIFICATION_RESPONSE


@router.post(
    "/api/auth/login",
    response_model=AuthResponse,
    dependencies=[
        Depends(rate_limit_dependency(login_rate_limiter, "login")),
    ],
)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Authenticate a user with email and password.
    Returns a JWT token on success.
    Rate-limited per client IP (LOGIN_RATE_LIMIT) against single-source brute
    force, layered with a per-account FAILURE cap keyed on the normalized email
    (LOGIN_ACCOUNT_RATE_LIMIT) so an IP-rotating attacker can't brute-force one
    account past the per-IP gate. That per-account cap counts ONLY failed
    attempts and is enforced INSIDE this handler (never as a pre-handler
    dependency): a correct password is verified first, clears the bucket, and
    is never rate-limited — so an attacker flooding a victim's email with wrong
    passwords can't lock the real owner out (targeted-lockout DoS).
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

    # Per-account FAILURE cap, keyed on the SUBMITTED email — applied
    # identically whether or not the account exists, so it adds no enumeration
    # oracle (the 429 copy matches the per-IP limiter's, and record_failure is
    # called the same way in both branches). A correct password never reaches
    # here, so a legitimate owner is never throttled regardless of how many
    # failures an IP-rotating attacker piled up. (In-process/per-worker — see
    # rate_limit.py; the effective cap is per worker in a multi-worker deploy.)
    account_key = f"login:{email}"
    if not user or not password_ok:
        retry_after = login_account_rate_limiter.record_failure(account_key)
        if retry_after is not None:
            seconds = int(retry_after) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many login attempts. Try again in {seconds} s.",
                headers={"Retry-After": str(seconds)},
            )
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    # Correct password → clear the account's failure bucket. This is what
    # guarantees a legitimate user is never locked out: a successful login
    # wipes any failures an IP-rotating attacker accumulated against this email.
    login_account_rate_limiter.reset(account_key)

    # [Admin] Disabled accounts can't sign in. Checked AFTER the password so
    # this response is only ever shown to the account's real owner (no
    # disabled-account oracle for someone guessing passwords). The machine
    # detail is deliberate — the frontend maps it to friendly copy.
    if user.is_disabled:
        raise HTTPException(status_code=403, detail="account_disabled")

    # [Email verification] Unverified accounts can't sign in. Checked AFTER the
    # password (same placement rationale as the disabled-account check above) so
    # verified-status is only ever revealed to the account's real owner — never
    # a verified-account oracle for someone guessing passwords. Grandfathered
    # (pre-feature) users are email_verified=True, so they're unaffected; only a
    # freshly-registered account that hasn't clicked its link lands here.
    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Please verify your email address. Check your inbox for the verification link.",
        )

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
# Self-service data rights (export + closure)
# ===========================================================================
#
# The platform stores a person's name, email, charging history, payment
# references and location-bearing session records. Both of the rights that
# implies — get a copy, and close the account — are self-service here rather
# than a "email us and we'll do it" promise.

# The exact phrase the client must send to close an account. Typed, not a
# checkbox: closure is irreversible and forfeits any remaining credit.
DELETE_ACCOUNT_CONFIRM_PHRASE = "DELETE MY ACCOUNT"


@router.get(
    "/api/auth/me/export",
    dependencies=[
        Depends(account_rate_limit_dependency(data_export_account_rate_limiter, "data export"))
    ],
)
async def export_my_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download everything AmpHive holds about the calling account, as JSON.

    Strictly self-scoped: services/data_export.py filters every collection on
    the authenticated user's own id, excludes credentials (password hash, token
    digests, push keys), and caps each collection with an explicit
    `truncated_collections` list rather than silently cutting rows.
    """
    document = await build_export(db, user)
    filename = f"amphive-data-export-{user.id}.json"
    logger.info("User data export generated", extra={"user_id": user.id})
    return JSONResponse(
        content=document,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/api/auth/me",
    dependencies=[
        Depends(account_rate_limit_dependency(delete_account_account_rate_limiter, "account closure"))
    ],
)
async def delete_my_account(
    req: DeleteAccountRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Close the calling account: purge personal rows, forfeit any remaining
    charging credit, anonymise the account row, revoke every token.

    Financial records (charging sessions, wallet ledger, GST invoices) are
    RETAINED against an anonymised tombstone — they are the operator's tax
    records and feed the CPO's earnings and payout watermark, and every
    `user_id` FK is ON DELETE CASCADE so a real row delete would destroy them.
    See services/account_closure.py for the full rationale.
    """
    if req.confirm.strip() != DELETE_ACCOUNT_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f'To close your account, send confirm="{DELETE_ACCOUNT_CONFIRM_PHRASE}".',
        )

    # Re-authenticate password accounts. A Google-only account's stored hash is
    # a deliberately unusable random value (routers/auth.py google_callback), so
    # no password could ever verify — the bearer token is the proof of identity
    # there, as it is on every other authenticated route.
    if user.auth_provider != "google":
        if not req.password or not verify_password(req.password, user.hashed_password):
            raise HTTPException(status_code=403, detail="Password is incorrect.")

    try:
        summary = await close_account(db, user)
    except AccountClosureRefused as refused:
        raise HTTPException(status_code=409, detail=refused.reason) from refused

    # Audited AFTER the commit and best-effort (try_record_audit never breaks
    # its caller): the closure itself must not be undone by an audit failure.
    await try_record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=None,  # the actor no longer exists as an identifiable person
        action="account.close",
        target_type="user",
        target_id=str(summary["user_id"]),
        detail=f"forfeited_coins={summary['forfeited_coins']}",
    )

    return {
        "status": "closed",
        "forfeited_coins": summary["forfeited_coins"],
        "detail": (
            "Your account is closed and your personal details have been removed. "
            "Billing records for past charging sessions are kept as required for "
            "tax and accounting."
        ),
    }


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

# --- Single-use, browser-bound OAuth exchange codes (M5 hardening) ---------
#
# Once google_callback has finished the server-side Google round-trip it must
# get the freshly-minted app JWT to the SPA. Handing the JWT straight to the
# browser in the redirect's URL fragment (`#token=...`) is a login-CSRF /
# session-fixation vector: the token is an UNBOUND bearer credential sitting
# in a shareable URL, so an attacker who completes their OWN Google sign-in
# can mail the resulting `.../callback#token=<attacker JWT>` link to a victim
# and silently drop the victim into the ATTACKER's account (victim's added
# credit, PII, etc. land on the attacker's account).
#
# Instead the callback mints a random, SINGLE-USE, short-lived CODE, stashes
# the real JWT server-side keyed by that code alongside a fresh per-flow
# NONCE, and sets the nonce in an httpOnly cookie on the redirect. The SPA
# reads `#code=...` from the fragment and POSTs it to /api/auth/google/exchange
# (which sends the nonce cookie back); only the browser that actually finished
# the flow — and therefore holds the httpOnly nonce cookie — can trade the
# code for the JWT. A planted code carried by a different browser has no
# matching nonce cookie and is refused. The code still travels in the fragment
# (never hits server/proxy access logs); the nonce never leaves the browser.
#
# In-memory, single-process store — matches the single-backend deployment
# (see docs). A restart just forces any in-flight sign-in to retry the (fast)
# redirect; nothing durable is lost.
GOOGLE_EXCHANGE_COOKIE = "google_oauth_nonce"
GOOGLE_EXCHANGE_TTL_SECONDS = 120  # the SPA redeems the code immediately on land
_google_exchange_store: dict[str, tuple[str, str, float]] = {}  # code -> (jwt, nonce, expires_at)


def _prune_expired_exchange_codes(now: float) -> None:
    """Drop timed-out codes so the store can't grow unbounded from abandoned
    sign-ins (browser closed before the SPA redeemed the code)."""
    for code in [c for c, (_jwt, _nonce, exp) in _google_exchange_store.items() if exp <= now]:
        _google_exchange_store.pop(code, None)


def _store_exchange_code(token: str, nonce: str) -> str:
    """Stash `token` under a fresh random code bound to `nonce`; return the
    code. No await between here and consumption, so the plain-dict mutation is
    safe under the single-process event loop (no lock needed)."""
    now = time.monotonic()
    _prune_expired_exchange_codes(now)
    code = secrets.token_urlsafe(32)
    _google_exchange_store[code] = (token, nonce, now + GOOGLE_EXCHANGE_TTL_SECONDS)
    return code


def _consume_exchange_code(code: str, nonce: str | None) -> str | None:
    """Redeem `code` for its JWT iff `nonce` matches the one it was bound to
    and it hasn't expired. The code is popped UNCONDITIONALLY (strictly
    single-use — a replay, or a wrong-nonce attempt, both burn it), then the
    nonce is checked in constant time. Returns None on any failure."""
    entry = _google_exchange_store.pop(code, None)
    if entry is None:
        return None
    token, stored_nonce, expires_at = entry
    if time.monotonic() > expires_at:
        return None
    if not nonce or not hmac.compare_digest(stored_nonce, nonce):
        return None
    return token


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
            # Google cryptographically asserted this email (email_verified claim
            # checked above), so the account is verified from birth — no
            # separate verify-email round-trip.
            email_verified=True,
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
        # First-time Google link to a PRE-EXISTING local/password account.
        # Google has cryptographically verified (email_verified, above) that
        # the person signing in owns this email RIGHT NOW, so we securely TAKE
        # OVER the credential instead of merely bolting Google on beside the
        # existing password:
        #
        #   * Invalidate the stored password. users.hashed_password is NOT
        #     NULL, so we can't null it — overwrite it with a fresh random
        #     unusable hash (valid shape, unmatchable; the same trick used for
        #     Google-only signups above and services.auth._DUMMY_PASSWORD_HASH).
        #     verify_password can never match it, so /api/auth/login refuses
        #     the old password forever after.
        #   * Bump token_version to revoke every JWT/session minted before the
        #     link (get_current_user and the socket layer both gate on tv).
        #   * Flip auth_provider to 'google' — Google is now the only working
        #     sign-in method, so the field stays consistent.
        #
        # Together these defeat the account-PRE-hijacking class: an attacker
        # who pre-registered the victim's (unverified) email with their OWN
        # password is fully evicted the instant the real owner links via
        # Google — the attacker's password stops working AND their live
        # sessions die. The owner can still set a password later via the normal
        # forgot/reset-password flow (which writes a fresh hash by email and
        # never checks the old one).
        #
        # NOTE: the COMPLETE fix for this class is email-ownership verification
        # AT REGISTRATION (so an unverified email can't hold a usable
        # credential in the first place). That is out of scope here and tracked
        # separately — this take-over closes the federated-merge half only.
        #
        # ORM-level mutations (not a DB-side UPDATE) mirror the google_sub
        # write this branch already did, so the whole take-over lands in the
        # single commit below. First-time linking is not a high-concurrency
        # path; even if two concurrent links collapsed the tv bump to +1
        # instead of +2, the pre-existing epoch is still invalidated (the
        # security goal holds).
        user.google_sub = google_sub
        user.hashed_password = hash_password(secrets.token_urlsafe(32))
        user.token_version = user.token_version + 1
        user.auth_provider = "google"
        # Google-verified owner is now the account holder — mark the email
        # verified (an attacker's pre-registered unverified account is taken
        # over AND becomes verified in the same step).
        user.email_verified = True
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
            "Linked Google identity to existing account "
            "(pre-existing password credential taken over, prior sessions revoked)",
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

    # [M5] Don't hand the raw JWT to the browser in the fragment (an unbound
    # bearer token in a shareable URL = login-CSRF / session-fixation). Mint a
    # single-use code bound to a fresh nonce, stash the JWT server-side, and
    # set the nonce httpOnly cookie so ONLY this browser can redeem the code at
    # /api/auth/google/exchange. See the exchange-store block above.
    nonce = secrets.token_urlsafe(24)
    code = _store_exchange_code(token, nonce)
    redirect_url = f"{email_service.frontend_origin()}/auth/google/callback#code={code}"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(GOOGLE_STATE_COOKIE)
    response.set_cookie(
        GOOGLE_EXCHANGE_COOKIE,
        nonce,
        max_age=GOOGLE_EXCHANGE_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


class GoogleExchangeRequest(BaseModel):
    """Body for POST /api/auth/google/exchange — just the single-use code the
    SPA pulled out of the callback redirect's URL fragment. Defined inline
    (not in schemas.py) since it's specific to this one browser-binding step."""

    code: str


@router.post("/api/auth/google/exchange")
async def google_exchange(req: GoogleExchangeRequest, request: Request):
    """
    Trade a single-use Google sign-in code for the real app JWT.

    The code was delivered to the SPA in the callback redirect's URL fragment;
    the matching nonce rides in the httpOnly `google_oauth_nonce` cookie set on
    that same redirect. Redemption succeeds only when BOTH arrive together from
    the same browser — that binding is what defeats the M5 login-CSRF /
    session-fixation attack (a planted `#code=...` link opened in a victim's
    browser has no matching nonce cookie, so the code is worthless there).

    The code is consumed on first lookup (single-use); replay, wrong nonce,
    expiry, and unknown code all get the same generic 400 (no oracle). The
    nonce cookie is cleared on every exit — the code is spent regardless.
    """
    nonce = request.cookies.get(GOOGLE_EXCHANGE_COOKIE)
    token = _consume_exchange_code(req.code, nonce)
    if token is None:
        response = JSONResponse(
            status_code=400,
            content={"detail": "Invalid or expired sign-in. Please try again."},
        )
        response.delete_cookie(GOOGLE_EXCHANGE_COOKIE)
        return response

    response = JSONResponse(status_code=200, content={"token": token})
    response.delete_cookie(GOOGLE_EXCHANGE_COOKIE)
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
    call with a real account triggers an outbound email, AND per submitted
    email (FORGOT_PASSWORD_EMAIL_RATE_LIMIT) so an attacker with an IP pool
    can't mailbomb one victim's inbox by rotating source addresses past the
    per-IP gate.

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

    # Per-EMAIL cap (layered on top of the per-IP dependency): keyed on the
    # SUBMITTED email and checked BEFORE the account lookup, so it fires
    # identically whether or not the account exists — it adds NO enumeration
    # oracle (the transition to 429 depends only on how many times THIS email
    # string was submitted, which is attacker-controlled, never on existence).
    # This bounds how many reset mails any single inbox can be made to receive
    # from all sources combined. In-process/per-worker, same caveat as the
    # rest of rate_limit.py.
    retry_after = forgot_password_email_rate_limiter.check(f"forgot:{email}")
    if retry_after is not None:
        seconds = int(retry_after) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Too many password reset requests for this email. Try again in {seconds} s.",
            headers={"Retry-After": str(seconds)},
        )

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


