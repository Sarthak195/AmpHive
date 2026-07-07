"""
Auth routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import async_session_factory, get_db
from backend.database.models import (
    ChargerGroup, ChargingSession, Gateway, GatewayStatus, GroupMembership,
    LedgerTransaction, Plug, PlugStatus, SessionStatus, TelemetryReading,
    Tenant, TransactionType, User, UserRole,
)
from backend.schemas import (
    AuthResponse, CpoGatewayCreateRequest, CpoGroupCreateRequest,
    CpoGroupUpdateRequest, CpoPlugCreateRequest, CpoPlugUpdateRequest,
    CpoSetupRequest, CreateOrderRequest, CreateOrderResponse,
    DirectPlugRequest, GatewayRegisterRequest, GroupResponse,
    JoinGroupRequest, LoginRequest, PlugRegisterRequest, PlugResponse,
    RegisterRequest, SessionStartRequest, SessionStopRequest, UserResponse,
    VerifyPaymentRequest,
)
from backend.services import payments as payment_service
from backend.services.auth import (
    create_access_token, decode_access_token, get_current_user,
    hash_password, verify_password,
)
from backend.services.money import ZERO_MONEY, to_money
from backend.services.rbac import require_role
from backend.services.session_lifecycle import (
    check_and_speed_up_active_session, finalize_charging_session,
    gateway_is_live, set_plug_telemetry_interval,
)
from backend.services.telemetry import COINS_PER_KWH

logger = logging.getLogger("amphive.api")
router = APIRouter()

# ===========================================================================
# Authentication Endpoints
# ===========================================================================

@router.post("/api/auth/register", response_model=AuthResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    Register a new driver account.
    Creates the user with a hashed password and returns a JWT token.
    New users start with 0 coin balance and the 'driver' role.
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
    token = create_access_token(user.id, user.role.value, user.email)
    logger.info(f"New user registered: {user.email} (id={user.id})")

    return AuthResponse(
        token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name,
              "role": user.role.value, "coin_balance": user.coin_balance},
    )


@router.post("/api/auth/login", response_model=AuthResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Authenticate a user with email and password.
    Returns a JWT token on success.
    """
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_access_token(user.id, user.role.value, user.email)
    logger.info(f"User logged in: {user.email}")

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
    )


