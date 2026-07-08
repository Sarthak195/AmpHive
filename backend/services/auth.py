"""
AmpHive Authentication Service
===============================
Handles password hashing (bcrypt), JWT token generation/validation, and
provides a FastAPI dependency to extract the authenticated user from requests.

Design decisions:
- JWT tokens use HS256 (symmetric) with a server-side secret. This is simpler
  than RS256 asymmetric keys and sufficient for a single-backend deployment.
- Token expiry: 7 days. Long-lived because this is a mobile-first web app
  and frequent re-login hurts UX. Can be shortened for the CPO admin portal.
- Password hashing uses bcrypt via passlib — industry standard, resistant to
  GPU-based brute force attacks.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import User

# --- Configuration ---

# Secret key for signing JWT tokens. MUST be set in production via env var.
# Generate a strong key with: python -c "import secrets; print(secrets.token_urlsafe(64))"
#
# If the env var is missing (or still set to the old, publicly-known dev
# default), we refuse to use a guessable key: a random ephemeral secret is
# generated instead. Tokens then won't survive a restart — a visible symptom
# that forces the misconfiguration to be fixed, instead of silently leaving
# every JWT forgeable by anyone who has read this repo.
_KNOWN_INSECURE_SECRETS = {
    "amphive-dev-secret-change-in-production",  # old hardcoded default
    "change-me-in-production",                  # deploy/config/.env.template placeholder
}
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
if not JWT_SECRET_KEY or JWT_SECRET_KEY in _KNOWN_INSECURE_SECRETS:
    JWT_SECRET_KEY = secrets.token_urlsafe(64)
    logging.getLogger("amphive.auth").critical(
        "JWT_SECRET_KEY is unset or uses a known-insecure default. Generated an "
        "EPHEMERAL signing key — all sessions will be invalidated on restart. "
        "Set a strong JWT_SECRET_KEY in the environment/.env to fix this."
    )
JWT_ALGORITHM = "HS256"
# Env-configurable so an environment can shorten token lifetime. Kept at 7d
# by default (mobile-first UX, no refresh flow); per-user revocation via the
# token_version epoch — see create_access_token / get_current_user — is the
# substantive control that makes a long-lived token killable on demand.
JWT_EXPIRY_DAYS = int(os.getenv("JWT_EXPIRY_DAYS", "7"))

# --- Password Hashing ---

# bcrypt context for hashing and verifying passwords.
# "auto" scheme enables automatic hash migration if we ever switch algorithms.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """Hash a plain-text password using bcrypt."""
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


# --- JWT Token Management ---

def create_access_token(user_id: int, role: str, email: str, token_version: int = 0) -> str:
    """
    Create a signed JWT access token containing user claims.

    Claims embedded in the token:
    - sub: user ID (string)
    - role: user role (admin/cpo/driver)
    - email: user email (for display purposes in frontend)
    - tv: token version (the user's token_version epoch at issue time; a
      request is rejected once the stored epoch moves past it)
    - exp: expiration timestamp
    - iat: issued-at timestamp
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "tv": token_version,
        "iat": now,
        "exp": now + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT token. Returns the payload dict if valid,
    or None if the token is expired/tampered/malformed.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


# --- FastAPI Authentication Dependency ---

# HTTPBearer extracts the token from the "Authorization: Bearer <token>" header.
security_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency that extracts and validates the JWT from the request,
    then loads the corresponding User from the database.

    Raises HTTP 401 if:
    - No Authorization header is present
    - The token is expired or invalid
    - The user_id in the token doesn't match any user in the database

    Usage:
        @app.get("/api/protected")
        async def protected_route(user: User = Depends(get_current_user)):
            return {"email": user.email}
    """
    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is missing user identity.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Load the user from the database to get fresh data (balance, role, etc.)
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reject tokens issued before the user's current token epoch (logout /
    # password change / admin revoke bumps token_version). Tokens minted
    # before this claim existed carry no `tv`; treat them as epoch 0 so a
    # deploy doesn't force-log-out everyone — the first revoke still kills them
    # (epoch moves to 1). This is a per-user "log out everywhere", not a
    # per-token blacklist.
    if payload.get("tv", 0) != user.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been revoked. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user
