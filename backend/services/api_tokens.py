"""
Personal read-only API tokens (feature F7).

A user mints a named token in Account (routers/tokens.py) and hands it to
Home Assistant or evcc, which present it as "Authorization: Bearer amph_<secret>".

Read-only by construction
-------------------------
The ONLY dependency that accepts a token is get_meter_user below, and the
only route that uses it is GET /api/me/meter. Every other route
authenticates through services/auth.py get_current_user, which decodes a
JWT; an "amph_..." string is not a JWT, so it gets the normal 401 there.
There is no write scope, no switch and no MQTT path.

Secrets
-------
The raw token is returned once by POST /api/me/tokens and never stored or
logged: the table keeps its SHA-256 hex digest (lookup is one indexed
equality) plus a short display prefix. Never log a token, its hash or an
Authorization header.

Revocation
----------
DELETE /api/me/tokens/{id} revokes one. revoke_user_api_tokens revokes them
all and is called wherever every session of the user is revoked: log out
everywhere, a password reset, a Google first-link takeover and account
closure. A disabled account's tokens are refused at use time.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import ApiToken, User
from backend.services.auth import get_current_user, optional_security_scheme
from backend.services.rate_limit import api_token_rate_limiter

logger = logging.getLogger("amphive.api_tokens")

TOKEN_PREFIX = "amph_"
TOKEN_SECRET_BYTES = 32
# Characters of the secret kept after "amph_" in the display prefix: enough
# to tell a handful of tokens apart, far too few to help guess one.
DISPLAY_SECRET_CHARS = 6
MAX_ACTIVE_TOKENS = 5
TOKEN_SCOPE_READ = "read"
# last_used_at is stamped at most this often per token, so a 30 s poll costs
# one UPDATE every other request at worst, not one per request.
LAST_USED_RESOLUTION = timedelta(minutes=1)

INVALID_TOKEN_DETAIL = "Invalid or revoked API token."


def generate_token() -> str:
    """A fresh raw token: "amph_" + 32 random bytes, url-safe."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_SECRET_BYTES)


def hash_token(raw: str) -> str:
    """SHA-256 hex digest of the full raw token (what the table stores)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def display_prefix(raw: str) -> str:
    """The non-secret stub shown in the token list, e.g. "amph_AbC123"."""
    return raw[: len(TOKEN_PREFIX) + DISPLAY_SECRET_CHARS]


def is_api_token(raw: Optional[str]) -> bool:
    return raw is not None and raw.startswith(TOKEN_PREFIX)


async def revoke_user_api_tokens(db: AsyncSession, user_id: int) -> int:
    """Revoke every live token of this user. Caller commits (so it lands in
    the same transaction as the session revocation it accompanies)."""
    result = await db.execute(
        update(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    return getattr(result, "rowcount", 0) or 0


def _unauthorized(detail: str = INVALID_TOKEN_DETAIL) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _stamp_last_used(db: AsyncSession, token: ApiToken, now: datetime) -> None:
    """Best-effort last_used_at stamp, at most once per LAST_USED_RESOLUTION.
    The WHERE repeats the age check so concurrent polls write once. A failure
    must never cost the read it accompanies."""
    last = token.last_used_at
    if last is not None and now - last < LAST_USED_RESOLUTION:
        return
    try:
        await db.execute(
            update(ApiToken)
            .where(
                ApiToken.id == token.id,
                or_(
                    ApiToken.last_used_at.is_(None),
                    ApiToken.last_used_at < now - LAST_USED_RESOLUTION,
                ),
            )
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning(
            "api token last_used_at stamp failed", extra={"token_id": token.id}, exc_info=True
        )


async def authenticate_api_token(db: AsyncSession, raw: str) -> User:
    """Resolve a raw "amph_..." token to its live, enabled owner, or raise
    401 (unknown, revoked, disabled owner) or 429 (per-token rate limit)."""
    row = (
        await db.execute(
            select(ApiToken, User)
            .join(User, User.id == ApiToken.user_id)
            .where(
                ApiToken.token_hash == hash_token(raw),
                ApiToken.revoked_at.is_(None),
                ApiToken.scope == TOKEN_SCOPE_READ,
            )
        )
    ).one_or_none()
    if row is None:
        raise _unauthorized()
    token, user = row
    if user.is_disabled:
        raise _unauthorized()

    retry_after = api_token_rate_limiter.check(f"token:{token.id}")
    if retry_after is not None:
        seconds = int(retry_after) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests with this API token. Try again in {seconds} s.",
            headers={"Retry-After": str(seconds)},
        )

    await _stamp_last_used(db, token, datetime.now(timezone.utc))
    return user


async def get_meter_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Auth for GET /api/me/meter, and ONLY that route: a personal read-only
    token, or the normal JWT (with every get_current_user check, including
    the cpo_viewer read-only guard). A missing or non-Bearer header is 401."""
    if credentials is None:
        raise _unauthorized("Not authenticated.")
    if is_api_token(credentials.credentials):
        return await authenticate_api_token(db, credentials.credentials)
    return await get_current_user(credentials, db, request)
