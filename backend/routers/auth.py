"""
Auth routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import asyncio
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    PasswordResetToken, User, UserRole,
)
from backend.schemas import (
    AuthResponse, ForgotPasswordRequest, LoginRequest, RegisterRequest, ResetPasswordRequest, UserResponse,
)
from backend.services.auth import (
    create_access_token, get_current_user,
    hash_password, verify_password,
)
from backend.services import email as email_service
from backend.services.rate_limit import (
    forgot_password_rate_limiter, login_rate_limiter, rate_limit_dependency,
    register_rate_limiter, reset_password_rate_limiter,
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
    # Check if email already exists (fast path for a clean error message; the
    # unique index is the real guard — a concurrent duplicate slips past this
    # SELECT and must be caught at commit).
    result = await db.execute(select(User).where(User.email == req.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    # Create the user with hashed password
    user = User(
        email=req.email,
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
    """
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

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
    """
    result = await db.execute(select(User).where(User.email == req.email))
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


