"""
AmpHive FastAPI Backend Server
==============================
Central REST API orchestrating user authentication, charging sessions,
wallet management, group access control, Razorpay payments, and real-time
SSE telemetry streaming.

Phase 2 additions:
- JWT authentication (register, login, me)
- Charger group management (join via access code, list groups)
- Plug access control (public/private group gating)
- Real-time SSE telemetry endpoint
- Razorpay payment endpoints (create order, verify payment)
- Session history endpoint
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from backend.database.db import get_db
from backend.database.models import (
    User, UserRole, Plug, PlugStatus, Gateway,
    ChargingSession, SessionStatus, LedgerTransaction, TransactionType,
    ChargerGroup, GroupMembership,
)
from backend.services.auth import (
    hash_password, verify_password, create_access_token, get_current_user,
)
from backend.services.mqtt_manager import MQTTManager
from backend.services.telemetry import TelemetryStore
from backend.services import payments as payment_service

# Load environment variables from .env file (for local development)
load_dotenv()

logger = logging.getLogger("amphive.api")
logging.basicConfig(level=logging.INFO)

# --- MQTT Configuration ---
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", None)
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", None)

# --- App Lifespan ---
mqtt_manager = None
telemetry_store = TelemetryStore()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage MQTT connection lifecycle with the FastAPI application."""
    global mqtt_manager
    # Initialize and start the MQTT connection
    mqtt_manager = MQTTManager(
        broker_host=MQTT_BROKER_HOST,
        broker_port=MQTT_BROKER_PORT,
        username=MQTT_USERNAME,
        password=MQTT_PASSWORD,
    )
    mqtt_manager.start()
    yield
    # Stop the MQTT connection during shutdown
    if mqtt_manager:
        mqtt_manager.stop()


app = FastAPI(
    title="AmpHive Shared EV Charging API",
    description="Backend PaaS control layer orchestrating ESP32 gateways, smart plugs, and headscale security policies.",
    version="2.0.0",
    lifespan=lifespan,
)

# --- CORS Middleware ---
# Allow the frontend (running on a different port/domain) to make API requests.
# In production, restrict origins to the actual frontend domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to amphive.duckdns.org in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# Pydantic Request/Response Schemas
# ===========================================================================

# --- Auth Schemas ---

class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str

class LoginRequest(BaseModel):
    email: str
    password: str

class AuthResponse(BaseModel):
    token: str
    user: dict

class UserResponse(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    coin_balance: float


# --- Session Schemas ---

class SessionStartRequest(BaseModel):
    plug_id: int
    max_duration_seconds: int = 14400  # 4 hours default
    max_kwh: float = 30.0             # 30 kWh limit default

class SessionStopRequest(BaseModel):
    session_id: int


# --- Gateway & Plug Schemas ---

class GatewayRegisterRequest(BaseModel):
    gateway_id: str  # MAC/UUID
    name: str
    vpn_ip: str
    tenant_id: int

class PlugRegisterRequest(BaseModel):
    gateway_id: str
    name: str
    local_ip: str
    plug_model: str = "tapo_p110"
    group_id: Optional[int] = None  # [P2] Optional charger group assignment


# --- Group Schemas ---

class JoinGroupRequest(BaseModel):
    access_code: str

class GroupResponse(BaseModel):
    id: int
    name: str
    is_public: bool
    plug_count: int

class PlugResponse(BaseModel):
    id: int
    name: str
    status: str
    current_power_w: float
    plug_model: str
    group_name: Optional[str] = None


# --- Payment Schemas ---

class CreateOrderRequest(BaseModel):
    amount_inr: int  # Amount in Rupees (e.g. 100 for ₹100)

class CreateOrderResponse(BaseModel):
    order_id: str
    amount: int       # Amount in paise
    currency: str
    key_id: str       # Razorpay Key ID (needed by frontend checkout)

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    amount_inr: int


# ===========================================================================
# Health Check
# ===========================================================================

@app.get("/api/health")
def health_check():
    """Service health check endpoint."""
    return {"status": "healthy", "service": "amphive-backend", "version": "2.0.0"}


# ===========================================================================
# Authentication Endpoints
# ===========================================================================

@app.post("/api/auth/register", response_model=AuthResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    Register a new driver account.
    Creates the user with a hashed password and returns a JWT token.
    New users start with 0 coin balance and the 'driver' role.
    """
    # Check if email already exists
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
    await db.commit()
    await db.refresh(user)

    # Generate JWT token
    token = create_access_token(user.id, user.role.value, user.email)
    logger.info(f"New user registered: {user.email} (id={user.id})")

    return AuthResponse(
        token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name,
              "role": user.role.value, "coin_balance": user.coin_balance},
    )


@app.post("/api/auth/login", response_model=AuthResponse)
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

    return AuthResponse(
        token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name,
              "role": user.role.value, "coin_balance": user.coin_balance},
    )


@app.get("/api/auth/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    """
    Return the current authenticated user's profile.
    Used by the frontend on app load to restore the session from a stored JWT.
    """
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        coin_balance=user.coin_balance,
    )


# ===========================================================================
# Charger Group Endpoints
# ===========================================================================

@app.post("/api/groups/join")
async def join_group(
    req: JoinGroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Join a private charger group using an access code.
    Once joined, the user can see and use all plugs in that group.
    """
    # Find the group by access code
    result = await db.execute(
        select(ChargerGroup).where(ChargerGroup.access_code == req.access_code)
    )
    group = result.scalar_one_or_none()

    if not group:
        raise HTTPException(status_code=404, detail="Invalid access code. No group found.")

    if group.is_public:
        raise HTTPException(status_code=400, detail="This group is public. No access code needed.")

    # Check if already a member
    result = await db.execute(
        select(GroupMembership).where(
            and_(GroupMembership.user_id == user.id, GroupMembership.group_id == group.id)
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="You are already a member of this group.")

    # Create membership
    membership = GroupMembership(user_id=user.id, group_id=group.id)
    db.add(membership)
    await db.commit()
    logger.info(f"User {user.email} joined group '{group.name}' (id={group.id})")

    return {"status": "joined", "group_id": group.id, "group_name": group.name}


@app.get("/api/groups/my", response_model=List[GroupResponse])
async def get_my_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List all charger groups the current user has access to:
    - All public groups
    - All private groups the user has joined
    """
    # Get all public groups
    public_result = await db.execute(
        select(ChargerGroup).where(ChargerGroup.is_public == True)
    )
    public_groups = list(public_result.scalars().all())

    # Get all private groups the user has joined
    joined_result = await db.execute(
        select(ChargerGroup)
        .join(GroupMembership, GroupMembership.group_id == ChargerGroup.id)
        .where(GroupMembership.user_id == user.id)
    )
    joined_groups = list(joined_result.scalars().all())

    # Merge and deduplicate
    all_groups = {g.id: g for g in public_groups + joined_groups}

    # Count plugs per group
    response = []
    for group in all_groups.values():
        plug_count_result = await db.execute(
            select(Plug).where(Plug.group_id == group.id)
        )
        plug_count = len(list(plug_count_result.scalars().all()))
        response.append(GroupResponse(
            id=group.id,
            name=group.name,
            is_public=group.is_public,
            plug_count=plug_count,
        ))

    return response


# ===========================================================================
# Plug Endpoints
# ===========================================================================

@app.get("/api/plugs/available", response_model=List[PlugResponse])
async def get_available_plugs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all plugs the current user can access:
    - Plugs in public groups
    - Plugs in private groups the user has joined
    - Ungrouped plugs (group_id = NULL, treated as public/legacy)
    """
    # Get IDs of private groups the user has joined
    membership_result = await db.execute(
        select(GroupMembership.group_id).where(GroupMembership.user_id == user.id)
    )
    joined_group_ids = [row[0] for row in membership_result.all()]

    # Get IDs of all public groups
    public_result = await db.execute(
        select(ChargerGroup.id).where(ChargerGroup.is_public == True)
    )
    public_group_ids = [row[0] for row in public_result.all()]

    # Combine accessible group IDs
    accessible_group_ids = list(set(joined_group_ids + public_group_ids))

    # Query plugs: in accessible groups OR ungrouped (group_id is NULL)
    if accessible_group_ids:
        plugs_result = await db.execute(
            select(Plug).where(
                or_(
                    Plug.group_id.in_(accessible_group_ids),
                    Plug.group_id.is_(None),
                )
            )
        )
    else:
        # User has no groups — only show ungrouped plugs
        plugs_result = await db.execute(
            select(Plug).where(Plug.group_id.is_(None))
        )

    plugs = list(plugs_result.scalars().all())

    response = []
    for plug in plugs:
        # Get group name if the plug belongs to a group
        group_name = None
        if plug.group_id:
            group_result = await db.execute(
                select(ChargerGroup.name).where(ChargerGroup.id == plug.group_id)
            )
            row = group_result.first()
            group_name = row[0] if row else None

        response.append(PlugResponse(
            id=plug.id,
            name=plug.name,
            status=plug.status.value,
            current_power_w=plug.current_power_w,
            plug_model=plug.plug_model,
            group_name=group_name,
        ))

    return response


@app.get("/api/plugs/{plug_id}", response_model=PlugResponse)
async def get_plug(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Look up a single plug by ID. Verifies the user has access to it.
    This is called when a driver manually enters a Plug ID in the app.
    """
    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()

    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    # Access check: verify user can see this plug
    if plug.group_id:
        group_result = await db.execute(
            select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
        )
        group = group_result.scalar_one_or_none()

        if group and not group.is_public:
            # Private group — check membership
            membership_result = await db.execute(
                select(GroupMembership).where(
                    and_(
                        GroupMembership.user_id == user.id,
                        GroupMembership.group_id == group.id,
                    )
                )
            )
            if not membership_result.scalar_one_or_none():
                raise HTTPException(
                    status_code=403,
                    detail="This plug belongs to a private group. Join the group first using an access code.",
                )

    # Get group name
    group_name = None
    if plug.group_id:
        gn_result = await db.execute(
            select(ChargerGroup.name).where(ChargerGroup.id == plug.group_id)
        )
        row = gn_result.first()
        group_name = row[0] if row else None

    return PlugResponse(
        id=plug.id,
        name=plug.name,
        status=plug.status.value,
        current_power_w=plug.current_power_w,
        plug_model=plug.plug_model,
        group_name=group_name,
    )


# ===========================================================================
# Gateway & Plug Registration Endpoints
# ===========================================================================

@app.post("/api/gateways/register")
async def register_gateway(
    req: GatewayRegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """Register a new ESP32 gateway in the database."""
    gateway = Gateway(
        id=req.gateway_id,
        tenant_id=req.tenant_id,
        name=req.name,
        vpn_ip=req.vpn_ip,
    )
    db.add(gateway)
    await db.commit()
    await db.refresh(gateway)
    logger.info(f"Gateway registered: {gateway.id} ({gateway.name})")

    return {
        "status": "registered",
        "gateway_id": gateway.id,
        "name": gateway.name,
        "vpn_ip": gateway.vpn_ip,
    }


@app.post("/api/plugs/register")
async def register_plug(
    req: PlugRegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """Register a new smart plug on a specific gateway's local VLAN subnet."""
    plug = Plug(
        gateway_id=req.gateway_id,
        name=req.name,
        local_ip=req.local_ip,
        plug_model=req.plug_model,
        group_id=req.group_id,
    )
    db.add(plug)
    await db.commit()
    await db.refresh(plug)
    logger.info(f"Plug registered: {plug.id} ({plug.name}) on gateway {plug.gateway_id}")

    return {
        "status": "registered",
        "plug_id": plug.id,
        "gateway_id": req.gateway_id,
        "name": plug.name,
        "local_ip": plug.local_ip,
        "plug_model": plug.plug_model,
    }


# ===========================================================================
# Charging Session Endpoints
# ===========================================================================

@app.post("/api/sessions/start")
async def start_charging_session(
    req: SessionStartRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Start a charging session on a specific plug.
    1. Verify user has access to the plug (group check).
    2. Check user has sufficient wallet balance (minimum ₹50).
    3. Look up the plug's gateway for MQTT routing.
    4. Send the ON command to the ESP32 gateway via MQTT.
    5. Create a session record in the database.
    6. Start the telemetry stream.
    """
    # 1. Verify plug exists and user has access
    result = await db.execute(select(Plug).where(Plug.id == req.plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug {req.plug_id} not found.")

    # Access check for private groups
    if plug.group_id:
        group_result = await db.execute(
            select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
        )
        group = group_result.scalar_one_or_none()
        if group and not group.is_public:
            membership_result = await db.execute(
                select(GroupMembership).where(
                    and_(
                        GroupMembership.user_id == user.id,
                        GroupMembership.group_id == group.id,
                    )
                )
            )
            if not membership_result.scalar_one_or_none():
                raise HTTPException(
                    status_code=403,
                    detail="You don't have access to this plug. Join the group first.",
                )

    # 2. Check wallet balance (minimum 50 coins to start)
    if user.coin_balance < 50:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient balance. You have {user.coin_balance} coins. Minimum 50 required.",
        )

    # 3. Check plug is available
    if plug.status == PlugStatus.OCCUPIED:
        raise HTTPException(status_code=409, detail="This plug is currently in use.")

    # 4. Send MQTT command to the gateway
    success = mqtt_manager.send_plug_command(
        gateway_id=plug.gateway_id,
        plug_id=plug.id,
        action="ON",
        max_duration=req.max_duration_seconds,
        max_kwh=req.max_kwh,
    )

    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to publish start command to the gateway. The gateway may be offline.",
        )

    # 5. Create session record in the database
    # Get the tenant_id from the plug's gateway
    gw_result = await db.execute(select(Gateway).where(Gateway.id == plug.gateway_id))
    gateway = gw_result.scalar_one()

    session = ChargingSession(
        tenant_id=gateway.tenant_id,
        user_id=user.id,
        plug_id=plug.id,
        status=SessionStatus.ACTIVE,
    )
    db.add(session)

    # Update plug status to occupied
    plug.status = PlugStatus.OCCUPIED
    await db.commit()
    await db.refresh(session)

    # 6. Initialize the telemetry stream for this plug
    telemetry_store.start_session(plug.id)

    logger.info(f"Session {session.id} started: user={user.email}, plug={plug.id}")

    return {
        "status": "started",
        "session_id": session.id,
        "plug_id": plug.id,
        "plug_name": plug.name,
        "message": f"Charging started on {plug.name}.",
    }


@app.post("/api/sessions/stop")
async def stop_charging_session(
    req: SessionStopRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Stop an active charging session.
    1. Send the OFF command to the ESP32 gateway via MQTT.
    2. Finalize the session record (end time, total energy, total cost).
    3. Create a ledger debit transaction.
    4. End the telemetry stream.
    """
    # Load the session
    result = await db.execute(
        select(ChargingSession).where(
            and_(
                ChargingSession.id == req.session_id,
                ChargingSession.user_id == user.id,
            )
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    if session.status != SessionStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="This session is not active.")

    # Load the plug
    plug_result = await db.execute(select(Plug).where(Plug.id == session.plug_id))
    plug = plug_result.scalar_one()

    # 1. Send MQTT OFF command
    success = mqtt_manager.send_plug_command(
        gateway_id=plug.gateway_id,
        plug_id=plug.id,
        action="OFF",
    )

    if not success:
        logger.warning(f"Failed to send OFF command for session {session.id}, but proceeding with DB cleanup.")

    # 2. Get final telemetry data
    latest = telemetry_store.get_latest(plug.id)
    final_energy = latest.energy_kwh if latest else session.energy_kwh
    final_cost = latest.cost_coins if latest else session.coins_spent

    # 3. Finalize session
    session.status = SessionStatus.COMPLETED
    session.ended_at = datetime.now(timezone.utc)
    session.energy_kwh = final_energy
    session.coins_spent = final_cost

    # 4. Deduct coins from user wallet and create ledger entry
    user.coin_balance = max(0, user.coin_balance - final_cost)

    ledger_entry = LedgerTransaction(
        user_id=user.id,
        session_id=session.id,
        amount=-final_cost,  # Negative = debit
        transaction_type=TransactionType.SESSION_DEBIT,
        description=f"Charging session on {plug.name}: {final_energy:.3f} kWh",
        balance_after=user.coin_balance,
    )
    db.add(ledger_entry)

    # 5. Update plug status back to available
    plug.status = PlugStatus.AVAILABLE
    await db.commit()

    # 6. End telemetry stream
    telemetry_store.end_session(plug.id)

    logger.info(f"Session {session.id} stopped: {final_energy:.3f} kWh, {final_cost:.2f} coins")

    return {
        "status": "completed",
        "session_id": session.id,
        "energy_kwh": round(final_energy, 3),
        "coins_spent": round(final_cost, 2),
        "balance_remaining": round(user.coin_balance, 2),
    }


@app.get("/api/sessions/live/{session_id}")
async def live_session_telemetry(
    session_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Server-Sent Events (SSE) endpoint streaming real-time charging telemetry
    to the frontend. Yields a JSON event every ~1 second containing power,
    current, energy, duration, and cost data.
    """
    # Verify session exists and belongs to the user
    result = await db.execute(
        select(ChargingSession).where(
            and_(
                ChargingSession.id == session_id,
                ChargingSession.user_id == user.id,
            )
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    async def event_generator():
        async for snapshot in telemetry_store.stream(session.plug_id):
            yield {"event": "telemetry", "data": json.dumps(snapshot)}

    return EventSourceResponse(event_generator())


@app.get("/api/sessions/history")
async def get_session_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return past charging sessions for the current user, most recent first."""
    result = await db.execute(
        select(ChargingSession)
        .where(ChargingSession.user_id == user.id)
        .order_by(ChargingSession.started_at.desc())
        .limit(50)
    )
    sessions = list(result.scalars().all())

    return [
        {
            "id": s.id,
            "plug_id": s.plug_id,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "energy_kwh": round(s.energy_kwh, 3),
            "coins_spent": round(s.coins_spent, 2),
            "status": s.status.value,
        }
        for s in sessions
    ]


# ===========================================================================
# Payment Endpoints (Razorpay)
# ===========================================================================

@app.post("/api/payments/create-order", response_model=CreateOrderResponse)
async def create_payment_order(
    req: CreateOrderRequest,
    user: User = Depends(get_current_user),
):
    """
    Create a Razorpay order for a wallet top-up.
    The frontend will use the returned order_id to open the Razorpay Checkout modal.
    """
    # Validate amount (minimum ₹10, maximum ₹10,000)
    if req.amount_inr < 10:
        raise HTTPException(status_code=400, detail="Minimum top-up amount is ₹10.")
    if req.amount_inr > 10000:
        raise HTTPException(status_code=400, detail="Maximum top-up amount is ₹10,000.")

    order = payment_service.create_order(req.amount_inr, user.id)
    if order is None:
        raise HTTPException(
            status_code=503,
            detail="Payment service is not configured. Contact support.",
        )

    return CreateOrderResponse(
        order_id=order["id"],
        amount=order["amount"],
        currency=order["currency"],
        key_id=payment_service.RAZORPAY_KEY_ID,
    )


@app.post("/api/payments/verify")
async def verify_payment(
    req: VerifyPaymentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Verify a Razorpay payment after the user completes checkout.
    If the signature is valid, credit coins to the user's wallet and
    create a ledger top-up transaction.
    """
    # Verify the payment signature (HMAC SHA256)
    is_valid = payment_service.verify_payment_signature(
        order_id=req.razorpay_order_id,
        payment_id=req.razorpay_payment_id,
        signature=req.razorpay_signature,
    )

    if not is_valid:
        raise HTTPException(status_code=400, detail="Payment verification failed. Invalid signature.")

    # Calculate coins to credit
    coins = payment_service.calculate_coins(req.amount_inr)

    # Credit coins to user wallet (thread-safe via database transaction)
    user.coin_balance += coins

    # Create ledger top-up transaction
    ledger_entry = LedgerTransaction(
        user_id=user.id,
        amount=coins,  # Positive = credit
        transaction_type=TransactionType.TOPUP,
        description=f"Wallet top-up: ₹{req.amount_inr} → {coins} coins (Razorpay: {req.razorpay_payment_id})",
        balance_after=user.coin_balance,
    )
    db.add(ledger_entry)
    await db.commit()

    logger.info(f"Payment verified: user={user.email}, ₹{req.amount_inr} → {coins} coins")

    return {
        "status": "success",
        "coins_credited": coins,
        "new_balance": round(user.coin_balance, 2),
    }


@app.post("/api/payments/webhook")
async def razorpay_webhook(request: Request):
    """
    Razorpay server-to-server webhook (backup verification).
    Razorpay calls this endpoint when a payment event occurs.
    This is a fallback — the primary flow is client-side verify above.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not payment_service.verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    # Parse the event
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    event_type = event.get("event", "")
    logger.info(f"Razorpay webhook received: {event_type}")

    # Handle payment.captured event
    # In production, this would credit coins if the client-side verify was missed.
    # For MVP, we log it and rely on the client-side flow.

    return {"status": "ok"}
