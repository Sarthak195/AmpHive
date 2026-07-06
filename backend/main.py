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

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, or_, and_, func, cast, Date
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from backend.database.db import get_db, init_db, async_session_factory
from backend.database.models import (
    User, UserRole, Plug, PlugStatus, Gateway, GatewayStatus, Tenant,
    ChargingSession, SessionStatus, LedgerTransaction, TransactionType,
    ChargerGroup, GroupMembership, TelemetryReading,
)
from backend.services.auth import (
    hash_password, verify_password, create_access_token, get_current_user, decode_access_token
)
from backend.services.rbac import require_role
from backend.services.mqtt_manager import MQTTManager
from backend.services.telemetry import TelemetryStore, COINS_PER_KWH
from backend.services.money import to_money, ZERO_MONEY
from backend.services.telemetry_persistence import TelemetryPersistenceService
# [Direct Mode] Import the Tapo direct driver for ESP32-bypass plug control
from backend.services.tapo_direct import TapoDirectDriver
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

# A gateway counts as live only if it reported (status or telemetry) within
# this window. The DB status flag alone is not enough: seeded/mock rows can
# say ONLINE with no MQTT client behind them, and a missed LWT leaves a dead
# gateway ONLINE forever. Telemetry arrives every ~10 s from a healthy
# gateway (bumped into last_seen_at at most once a minute), so 120 s gives
# comfortable slack without letting sessions start against dead hardware.
GATEWAY_LIVENESS_WINDOW_SEC = int(os.getenv("GATEWAY_LIVENESS_WINDOW_SEC", "120"))

# --- [Direct Mode] Tapo P110 Configuration ---
# When DIRECT_MODE=true, the backend can control the plug directly via the
# `tapo` Python library, bypassing the ESP32 gateway and MQTT broker.
# This is used for development/testing before the ESP32 board is available.
DIRECT_MODE = os.getenv("DIRECT_MODE", "false").lower() == "true"
TAPO_USERNAME = os.getenv("TAPO_USERNAME", "")
TAPO_PASSWORD = os.getenv("TAPO_PASSWORD", "")
TAPO_PLUG_IP = os.getenv("TAPO_PLUG_IP", "")

# --- App Lifespan ---
mqtt_manager = None
# [Direct Mode] Global reference to the Tapo direct driver (initialized in lifespan)
tapo_driver: Optional[TapoDirectDriver] = None
telemetry_store = TelemetryStore()
# Buffered batch-flush service for time-series telemetry persistence (lifespan-owned)
telemetry_persistence: Optional[TelemetryPersistenceService] = None

async def set_plug_telemetry_interval(db: AsyncSession, plug_id: int, interval_ms: int):
    """
    Update the polling interval for a specific plug in telemetry_store
    and publish the SET_INTERVAL MQTT command to the gateway.
    """
    if telemetry_store.get_interval(plug_id) == interval_ms:
        return

    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        logger.warning(f"Plug {plug_id} not found when trying to set telemetry interval.")
        return

    telemetry_store.set_interval(plug_id, interval_ms)

    from backend.services.mqtt_manager import MQTTManager
    manager = MQTTManager()
    if hasattr(manager, "client") and manager.client:
        manager.send_plug_interval(plug.gateway_id, plug_id, interval_ms)

def gateway_is_live(gateway: Gateway, now: Optional[datetime] = None) -> bool:
    """
    Whether a gateway is actually reachable, not just flagged ONLINE in the DB.
    Requires both the ONLINE status and a last_seen_at within
    GATEWAY_LIVENESS_WINDOW_SEC (status messages and telemetry both refresh it).
    """
    if gateway.status != GatewayStatus.ONLINE:
        return False
    if gateway.last_seen_at is None:
        return False
    last_seen = gateway.last_seen_at
    if last_seen.tzinfo is None:
        # Legacy rows written by the old naive-datetime onupdate hook.
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - last_seen).total_seconds() <= GATEWAY_LIVENESS_WINDOW_SEC


async def check_and_speed_up_active_session(db: AsyncSession, user_id: int):
    """
    Check if the user has active charging sessions, and if so, speed up the
    telemetry interval for each corresponding plug to 1000ms.

    A user can hold more than one ACTIVE session (nothing limits starts to a
    single plug), so this must not assume a single row — scalar_one_or_none()
    here raised MultipleResultsFound, which broke /api/auth/login and
    /api/auth/me for that user.
    """
    result = await db.execute(
        select(ChargingSession).where(
            and_(
                ChargingSession.user_id == user_id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
    )
    for active_session in result.scalars().all():
        await set_plug_telemetry_interval(db, active_session.plug_id, 1000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage MQTT and Tapo Direct connection lifecycles with the FastAPI application."""
    global mqtt_manager, tapo_driver, telemetry_persistence

    # Initialize the database tables
    await init_db()

    import asyncio
    loop = asyncio.get_running_loop()

    # Start the telemetry persistence flush loop before MQTT so the buffer is
    # ready to receive enqueued readings as soon as messages arrive.
    telemetry_persistence = TelemetryPersistenceService(
        db_session_factory=async_session_factory,
    )
    telemetry_persistence.start(loop)

    # Initialize and start the MQTT connection.
    # Pass telemetry_store so inbound MQTT data feeds the SSE stream,
    # db_session_factory so session totals are persisted, and
    # telemetry_persistence so raw samples are buffered into telemetry_readings.
    mqtt_manager = MQTTManager(
        broker_host=MQTT_BROKER_HOST,
        broker_port=MQTT_BROKER_PORT,
        username=MQTT_USERNAME,
        password=MQTT_PASSWORD,
        telemetry_store=telemetry_store,
        db_session_factory=async_session_factory,
        event_loop=loop,
        telemetry_persistence=telemetry_persistence,
    )
    mqtt_manager.start()

    # [Direct Mode] Initialize the Tapo direct driver if enabled.
    # This allows controlling the plug without an ESP32 gateway, useful for
    # development/testing. The plug is reached via a WireGuard tunnel to
    # the developer's home network.
    if DIRECT_MODE and TAPO_USERNAME and TAPO_PASSWORD:
        tapo_driver = TapoDirectDriver(
            tapo_email=TAPO_USERNAME,
            tapo_password=TAPO_PASSWORD,
        )
        logger.info(f"🔌 Direct Mode ENABLED — Tapo plug target: {TAPO_PLUG_IP}")
    else:
        logger.info("Direct Mode DISABLED — using standard ESP32/MQTT path")

    yield
    # Stop MQTT first (no new enqueues), then drain + stop the flush loop so the
    # buffered tail is persisted.
    if mqtt_manager:
        mqtt_manager.stop()
    if telemetry_persistence:
        await telemetry_persistence.stop()


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
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://amphive.duckdns.org",
        "https://amphive.duckdns.org",
        "http://8.231.81.12",
        "https://8.231.81.12",
    ],
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
    # Bounded so a client can't disable the firmware safety watchdog by sending
    # an absurd limit. 1 s .. 24 h, and 0.1 .. 100 kWh.
    max_duration_seconds: int = Field(default=14400, gt=0, le=86400)  # 4 h default, 24 h cap
    max_kwh: float = Field(default=30.0, gt=0, le=100.0)              # 30 kWh default, 100 kWh cap

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
    # Effective map coordinates: the plug's own, else its gateway's, else None.
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# --- Payment Schemas ---

class CreateOrderRequest(BaseModel):
    amount_inr: float  # Amount in Rupees (e.g. 100 for ₹100)

class CreateOrderResponse(BaseModel):
    order_id: str
    amount: int       # Amount in paise
    currency: str
    key_id: str       # Razorpay Key ID (needed by frontend checkout)

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    # Deprecated and IGNORED: the credited amount is always fetched from
    # Razorpay's API server-side. Kept optional so older clients that still
    # send it don't get a 422.
    amount_inr: Optional[float] = None


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

    await check_and_speed_up_active_session(db, user.id)

    return AuthResponse(
        token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name,
              "role": user.role.value, "coin_balance": user.coin_balance},
    )


@app.get("/api/auth/me", response_model=UserResponse)
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

    # Batch-load gateway coordinates once: a plug with no coords of its own
    # falls back to its gateway's location (avoids an N+1 lookup per plug).
    gateway_ids = {p.gateway_id for p in plugs}
    gateway_coords = {}
    if gateway_ids:
        gw_rows = await db.execute(
            select(Gateway.id, Gateway.latitude, Gateway.longitude)
            .where(Gateway.id.in_(gateway_ids))
        )
        gateway_coords = {gid: (lat, lng) for gid, lat, lng in gw_rows.all()}

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

        gw_lat, gw_lng = gateway_coords.get(plug.gateway_id, (None, None))
        response.append(PlugResponse(
            id=plug.id,
            name=plug.name,
            status=plug.status.value,
            current_power_w=plug.current_power_w,
            plug_model=plug.plug_model,
            group_name=group_name,
            latitude=plug.latitude if plug.latitude is not None else gw_lat,
            longitude=plug.longitude if plug.longitude is not None else gw_lng,
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

    # Effective coords: the plug's own, else its gateway's site location.
    gw_row = await db.execute(
        select(Gateway.latitude, Gateway.longitude).where(Gateway.id == plug.gateway_id)
    )
    gw_coords = gw_row.first()
    gw_lat, gw_lng = (gw_coords[0], gw_coords[1]) if gw_coords else (None, None)

    return PlugResponse(
        id=plug.id,
        name=plug.name,
        status=plug.status.value,
        current_power_w=plug.current_power_w,
        plug_model=plug.plug_model,
        group_name=group_name,
        latitude=plug.latitude if plug.latitude is not None else gw_lat,
        longitude=plug.longitude if plug.longitude is not None else gw_lng,
    )



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
    3. Lock the plug row and claim it (avoids two concurrent starts on one plug).
    4. Commit the session + OCCUPIED status FIRST, then publish MQTT ON.
       (Publishing first could leave the plug live with no session billing it
       if the DB write then fails. If the publish fails we roll the claim back.)
    5. Start the telemetry stream.
    """
    # 1. Verify plug exists and lock the row for the duration of this txn, so a
    #    concurrent start blocks here and then sees OCCUPIED (closes the TOCTOU
    #    between the availability check and the claim below).
    result = await db.execute(
        select(Plug).where(Plug.id == req.plug_id).with_for_update()
    )
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

    # 3. Claim the plug (still holding the row lock). Only OCCUPIED blocks a
    #    start; offline/maintenance plugs are handled by the gateway/telemetry.
    if plug.status == PlugStatus.OCCUPIED:
        raise HTTPException(status_code=409, detail="This plug is currently in use.")

    gw_result = await db.execute(select(Gateway).where(Gateway.id == plug.gateway_id))
    gateway = gw_result.scalar_one()

    # Refuse to start against a gateway that isn't demonstrably alive: the MQTT
    # publish below only confirms the *broker* accepted the command, so without
    # this check a session on a dead gateway starts "successfully" and then
    # sits ACTIVE forever with the plug pinned OCCUPIED and no telemetry.
    if not gateway_is_live(gateway):
        raise HTTPException(
            status_code=409,
            detail="This charger's gateway is offline. Try again once it reconnects.",
        )

    session = ChargingSession(
        tenant_id=gateway.tenant_id,
        user_id=user.id,
        plug_id=plug.id,
        status=SessionStatus.ACTIVE,
    )
    db.add(session)
    plug.status = PlugStatus.OCCUPIED
    # Commit the claim + session BEFORE touching hardware, releasing the lock.
    await db.commit()
    await db.refresh(session)

    # Broadcast the claim so other clients' plug lists flip to OCCUPIED live.
    from backend.services.socketio_manager import emit_plug_status
    await emit_plug_status(plug.id, PlugStatus.OCCUPIED.value)

    # 4. Now command the gateway. If this fails, undo the claim so the plug
    #    doesn't stay OCCUPIED with a live ACTIVE session nobody can drive.
    success = mqtt_manager.send_plug_command(
        gateway_id=plug.gateway_id,
        plug_id=plug.id,
        action="ON",
        max_duration=req.max_duration_seconds,
        max_kwh=req.max_kwh,
        session_id=session.id,
    )
    if not success:
        session.status = SessionStatus.CANCELLED
        session.ended_at = datetime.now(timezone.utc)
        plug.status = PlugStatus.AVAILABLE
        await db.commit()
        await emit_plug_status(plug.id, PlugStatus.AVAILABLE.value)
        raise HTTPException(
            status_code=500,
            detail="Failed to publish start command to the gateway. The gateway may be offline.",
        )

    # 5. Initialize the telemetry stream for this plug
    telemetry_store.start_session(plug.id)
    await set_plug_telemetry_interval(db, plug.id, 1000)

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

    # 2. Determine final energy, then derive cost from it.
    #    Prefer the live in-memory snapshot, but fall back to the energy
    #    persisted on the session row (updated from inbound MQTT telemetry by
    #    MQTTManager._persist_telemetry). This matters after a backend restart:
    #    TelemetryStore is empty, and session.coins_spent is NEVER written
    #    mid-session — so the old `latest.cost_coins if latest else
    #    session.coins_spent` billed 0 for any session that outlived a restart.
    #    Take the max so a stale/empty store can't bill LESS than what was
    #    already recorded, and always compute cost from energy * COINS_PER_KWH
    #    (the same formula TelemetryStore uses) for a single source of truth.
    latest = telemetry_store.get_latest(plug.id)
    persisted_energy = session.energy_kwh or 0.0
    live_energy = latest.energy_kwh if latest else 0.0
    final_energy = max(live_energy, persisted_energy)
    final_cost = to_money(final_energy * COINS_PER_KWH)  # Decimal, 2 dp

    # 3. Finalize session
    session.status = SessionStatus.COMPLETED
    session.ended_at = datetime.now(timezone.utc)
    session.energy_kwh = final_energy

    # 4. Deduct coins from user wallet and create ledger entry (Atomic)
    user_result = await db.execute(
        select(User).where(User.id == user.id).with_for_update()
    )
    locked_user = user_result.scalar_one()

    # Debit only what the wallet actually holds. Writing a ledger row whose
    # `amount` disagrees with the real balance delta (as `max(0, ...)` used to,
    # while still recording -final_cost) breaks reconciliation: the running
    # balance can no longer be derived by summing `amount`. If the bill exceeds
    # the balance, the shortfall is forgiven but recorded for observability.
    prev_balance = locked_user.coin_balance if locked_user.coin_balance > 0 else ZERO_MONEY
    actual_debit = min(final_cost, prev_balance)  # both Decimal
    shortfall = final_cost - actual_debit
    locked_user.coin_balance = prev_balance - actual_debit
    session.coins_spent = actual_debit  # what was actually collected from the wallet

    description = f"Charging session on {plug.name}: {final_energy:.3f} kWh"
    if shortfall > 0:
        description += f" (shortfall {shortfall:.2f} coins uncollected)"
        logger.warning(
            f"Session {session.id}: billed {final_cost:.2f} coins but wallet held "
            f"only {prev_balance:.2f}; {shortfall:.2f} coins uncollected"
        )

    ledger_entry = LedgerTransaction(
        user_id=locked_user.id,
        session_id=session.id,
        amount=-actual_debit,  # Negative = debit; matches the real balance delta
        transaction_type=TransactionType.SESSION_DEBIT,
        description=description,
        balance_after=locked_user.coin_balance,
    )
    db.add(ledger_entry)

    # 5. Update plug status back to available
    plug.status = PlugStatus.AVAILABLE
    await db.commit()

    # Broadcast so other clients' plug lists flip back to AVAILABLE live.
    from backend.services.socketio_manager import emit_plug_status
    await emit_plug_status(plug.id, PlugStatus.AVAILABLE.value)

    # 6. End telemetry stream
    telemetry_store.end_session(plug.id)
    await set_plug_telemetry_interval(db, plug.id, 10000)

    logger.info(f"Session {session.id} stopped: {final_energy:.3f} kWh, {actual_debit:.2f} coins")

    return {
        "status": "completed",
        "session_id": session.id,
        "energy_kwh": round(final_energy, 3),
        "coins_spent": round(actual_debit, 2),
        "balance_remaining": round(locked_user.coin_balance, 2),
    }

@app.get("/api/sessions/active")
async def get_active_session(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current active charging session for the logged-in user, if any."""
    result = await db.execute(
        select(ChargingSession)
        .where(
            and_(
                ChargingSession.user_id == user.id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
        .order_by(ChargingSession.started_at.desc())
    )
    sessions = list(result.scalars().all())
    if not sessions:
        return {"active": False}
    session = sessions[0]

    plug_result = await db.execute(select(Plug).where(Plug.id == session.plug_id))
    plug = plug_result.scalar_one()

    return {
        "active": True,
        "session_id": session.id,
        "plug_id": session.plug_id,
        "plug_name": plug.name,
        "started_at": session.started_at.isoformat() if session.started_at else None,
    }


@app.get("/api/sessions/live/{session_id}")
async def live_session_telemetry(
    session_id: int,
    token: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Server-Sent Events (SSE) endpoint streaming real-time charging telemetry
    to the frontend. Yields a JSON event every ~1 second containing power,
    current, energy, duration, and cost data.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = int(payload.get("sub"))

    # Verify session exists and belongs to the user
    result = await db.execute(
        select(ChargingSession).where(
            and_(
                ChargingSession.id == session_id,
                ChargingSession.user_id == user_id,
            )
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    # Increment active listeners in telemetry store
    telemetry_store.increment_listeners(session.plug_id)
    await set_plug_telemetry_interval(db, session.plug_id, 1000)

    async def event_generator():
        try:
            async for snapshot in telemetry_store.stream(session.plug_id):
                yield {"event": "telemetry", "data": json.dumps(snapshot)}
        finally:
            listeners = telemetry_store.decrement_listeners(session.plug_id)
            if listeners == 0:
                async with async_session_factory() as local_db:
                    await set_plug_telemetry_interval(local_db, session.plug_id, 10000)

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

async def _already_credited(db: AsyncSession, payment_id: str) -> bool:
    """True if a TOPUP ledger row already exists for this razorpay_payment_id."""
    existing = await db.execute(
        select(LedgerTransaction.id).where(
            LedgerTransaction.razorpay_payment_id == payment_id
        )
    )
    return existing.first() is not None


async def _credit_topup(
    db: AsyncSession,
    *,
    user_id: int,
    coins: float,
    payment_id: str,
    description: str,
) -> Optional[float]:
    """
    Atomically credit `coins` to a user and write the TOPUP ledger row keyed by
    `payment_id`. The UNIQUE constraint on razorpay_payment_id makes this
    idempotent even under a concurrent /verify + webhook race: the loser's
    INSERT raises IntegrityError, we roll back (undoing the balance bump), and
    return None so the caller reports an idempotent no-op.

    Returns the new balance on success, or None if this payment was already
    credited by a concurrent request.
    """
    user_result = await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    locked_user = user_result.scalar_one_or_none()
    if locked_user is None:
        return None

    credit = to_money(coins)  # normalise the float from the payment service
    locked_user.coin_balance = locked_user.coin_balance + credit
    db.add(LedgerTransaction(
        user_id=locked_user.id,
        amount=credit,  # Positive = credit
        transaction_type=TransactionType.TOPUP,
        description=description,
        razorpay_payment_id=payment_id,
        balance_after=locked_user.coin_balance,
    ))
    try:
        await db.commit()
    except IntegrityError:
        # Another request (webhook vs verify) already inserted this payment_id.
        await db.rollback()
        return None
    return locked_user.coin_balance


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

    # --- Authoritative amount ----------------------------------------------
    # The checkout signature only proves (order_id, payment_id) are genuine —
    # it does NOT cover the amount, so the client-sent amount must never be
    # trusted. Fetch the payment from Razorpay and credit what was actually
    # paid. (razorpay SDK is sync/blocking → offload to a thread.)
    payment = await asyncio.to_thread(
        payment_service.fetch_captured_payment,
        req.razorpay_payment_id,
        req.razorpay_order_id,
    )
    if payment is None:
        raise HTTPException(
            status_code=502,
            detail="Could not confirm the payment with Razorpay. If you were "
                   "charged, your wallet will be credited automatically shortly.",
        )

    # Only credit money that has actually settled. Authorized-but-uncaptured
    # payments are credited by the webhook once capture happens.
    if payment["status"] != "captured":
        raise HTTPException(
            status_code=409,
            detail="Payment not captured yet. Your wallet will be credited "
                   "automatically once the payment settles.",
        )

    # The order's notes carry the user it was created for — refuse to credit
    # this caller with a payment made for a different account.
    notes_user_id = payment["notes"].get("user_id")
    if notes_user_id is not None and str(notes_user_id) != str(user.id):
        raise HTTPException(
            status_code=403,
            detail="This payment belongs to a different account.",
        )

    amount_inr = payment["amount_inr"]

    # --- Idempotency (fast path) -------------------------------------------
    # Both the webhook and the client-side /verify path credit the same
    # payment. A prior TOPUP row for this razorpay_payment_id means it's
    # already done. The UNIQUE constraint on razorpay_payment_id is the
    # authoritative guard for the concurrent case (handled below); this check
    # just avoids the wasted work in the common already-credited case.
    already = await _already_credited(db, req.razorpay_payment_id)
    if already:
        logger.info(f"Payment {req.razorpay_payment_id} already credited — skipping verify (idempotent).")
        return {"status": "success", "coins_credited": 0, "new_balance": round(user.coin_balance, 2)}

    # Calculate coins to credit from the Razorpay-confirmed amount
    coins = payment_service.calculate_coins(amount_inr)

    credited = await _credit_topup(
        db,
        user_id=user.id,
        coins=coins,
        payment_id=req.razorpay_payment_id,
        description=f"Wallet top-up: ₹{amount_inr:.2f} → {coins:.2f} coins (Razorpay: {req.razorpay_payment_id})",
    )
    if credited is None:
        # Lost the race to the webhook (or a duplicate submit). Not an error.
        logger.info(f"Payment {req.razorpay_payment_id} credited concurrently — treating verify as idempotent.")
        fresh = await db.execute(select(User.coin_balance).where(User.id == user.id))
        return {"status": "success", "coins_credited": 0, "new_balance": round(fresh.scalar_one(), 2)}

    logger.info(f"Payment verified: user={user.email}, ₹{amount_inr:.2f} → {coins:.2f} coins")
    return {
        "status": "success",
        "coins_credited": coins,
        "new_balance": round(credited, 2),
    }


@app.post("/api/payments/webhook")
async def razorpay_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Razorpay server-to-server webhook (server-authoritative credit path).

    Razorpay calls this endpoint when a payment event occurs. Unlike the
    client-side /verify path (which depends on the browser round-tripping the
    signature back to us), this callback comes straight from Razorpay's
    servers, so it credits the wallet even if the user closes the tab right
    after paying.

    Flow:
      1. Verify the HMAC signature (X-Razorpay-Signature) against the raw body.
      2. Parse the event and extract the settled payment (payment.captured).
      3. Idempotency guard: if a ledger TOPUP row already references this
         razorpay_payment_id (created here OR by /verify), do nothing. This
         prevents double-crediting when both paths fire for the same payment.
      4. Atomically credit coins (row-locked user) and write a ledger entry.
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

    # Extract the creditable payment. Returns None for non-payment events
    # (e.g. order.paid, refund.*) or captures we can't attribute to a user.
    payment = payment_service.extract_payment_from_webhook(event)
    if payment is None:
        # Not a creditable event — acknowledge so Razorpay stops retrying.
        return {"status": "ignored", "event": event_type}

    payment_id = payment["payment_id"]

    # --- Idempotency (fast path) -------------------------------------------
    # A prior TOPUP row for this razorpay_payment_id means it was already
    # credited (by an earlier webhook retry or by /verify). The authoritative
    # guard for the concurrent case is the UNIQUE constraint, handled by
    # _credit_topup below.
    if await _already_credited(db, payment_id):
        logger.info(f"Webhook payment {payment_id} already credited — skipping (idempotent).")
        return {"status": "already_credited", "payment_id": payment_id}

    coins = payment["coins"]
    new_balance = await _credit_topup(
        db,
        user_id=payment["user_id"],
        coins=coins,
        payment_id=payment_id,
        description=f"Wallet top-up (webhook): ₹{payment['amount_inr']:.2f} → {coins:.2f} coins (Razorpay: {payment_id})",
    )
    if new_balance is None:
        # Either the user doesn't exist, or /verify won the race. Distinguish
        # so Razorpay gets a truthful ack and stops retrying in both cases.
        if await _already_credited(db, payment_id):
            return {"status": "already_credited", "payment_id": payment_id}
        logger.warning(f"Webhook payment {payment_id} references unknown user {payment['user_id']}.")
        return {"status": "user_not_found", "payment_id": payment_id}

    logger.info(
        f"Webhook credited user={payment['user_id']}: ₹{payment['amount_inr']:.2f} → {coins:.2f} coins "
        f"(payment={payment_id})"
    )
    return {
        "status": "credited",
        "payment_id": payment_id,
        "coins_credited": coins,
        "new_balance": round(new_balance, 2),
    }


# ===========================================================================
# [Direct Mode] Tapo P110 Direct Control Endpoints
# ===========================================================================
# These endpoints bypass the ESP32 gateway and MQTT broker, controlling the
# Tapo P110 smart plug directly via the `tapo` Python library through a
# WireGuard tunnel to the developer's home network.
#
# Architecture:
#   Cloud Backend (GCP VM) → WireGuard Tunnel → Dev PC → Home LAN → Tapo P110
#
# These are development/testing-only endpoints. In production, the normal
# ESP32/MQTT session flow is used instead.
#
# All endpoints require JWT authentication.
# ===========================================================================


class DirectPlugRequest(BaseModel):
    """Optional request body for direct plug control. If plug_ip is not provided,
    falls back to the TAPO_PLUG_IP environment variable."""
    plug_ip: Optional[str] = None


def _get_plug_ip(req_ip: Optional[str] = None) -> str:
    """
    Resolve the target plug IP address.
    Priority: request body plug_ip > TAPO_PLUG_IP env var.
    Raises 400 if neither is set.
    """
    ip = req_ip or TAPO_PLUG_IP
    if not ip:
        raise HTTPException(
            status_code=400,
            detail="No plug IP specified. Set TAPO_PLUG_IP env var or pass plug_ip in the request body.",
        )
    return ip


@app.post("/api/direct/plug/on")
async def direct_plug_on(
    req: DirectPlugRequest = DirectPlugRequest(),
    user: User = Depends(get_current_user),
):
    """
    [Direct Mode] Turn the Tapo P110 plug ON.
    Bypasses ESP32/MQTT and sends the command directly to the plug via
    the WireGuard tunnel. Requires DIRECT_MODE=true in environment.
    """
    if not DIRECT_MODE or not tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    plug_ip = _get_plug_ip(req.plug_ip)
    success = await tapo_driver.turn_on(plug_ip)

    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to turn ON plug at {plug_ip}. Check WireGuard tunnel and plug connectivity.",
        )

    return {
        "status": "on",
        "plug_ip": plug_ip,
        "message": f"Plug at {plug_ip} turned ON via direct mode.",
        "mode": "direct",
    }


@app.post("/api/direct/plug/off")
async def direct_plug_off(
    req: DirectPlugRequest = DirectPlugRequest(),
    user: User = Depends(get_current_user),
):
    """
    [Direct Mode] Turn the Tapo P110 plug OFF.
    Bypasses ESP32/MQTT and sends the command directly to the plug via
    the WireGuard tunnel. Requires DIRECT_MODE=true in environment.
    """
    if not DIRECT_MODE or not tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    plug_ip = _get_plug_ip(req.plug_ip)
    success = await tapo_driver.turn_off(plug_ip)

    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to turn OFF plug at {plug_ip}. Check WireGuard tunnel and plug connectivity.",
        )

    return {
        "status": "off",
        "plug_ip": plug_ip,
        "message": f"Plug at {plug_ip} turned OFF via direct mode.",
        "mode": "direct",
    }


@app.get("/api/direct/plug/info")
async def direct_plug_info(
    plug_ip: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    """
    [Direct Mode] Get device information from the Tapo P110 plug.
    Returns power state, model, nickname, firmware version, etc.
    """
    if not DIRECT_MODE or not tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    target_ip = _get_plug_ip(plug_ip)
    info = await tapo_driver.get_device_info(target_ip)

    if info is None:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to get device info from {target_ip}. Check WireGuard tunnel and plug connectivity.",
        )

    return {
        "plug_ip": target_ip,
        "device_info": info,
        "mode": "direct",
    }


@app.get("/api/direct/plug/energy")
async def direct_plug_energy(
    plug_ip: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    """
    [Direct Mode] Get energy usage data from the Tapo P110 plug.
    Returns current power draw, today's energy consumption, monthly stats.
    """
    if not DIRECT_MODE or not tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    target_ip = _get_plug_ip(plug_ip)
    usage = await tapo_driver.get_energy_usage(target_ip)

    if usage is None:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to get energy data from {target_ip}. Check WireGuard tunnel and plug connectivity.",
        )

    return {
        "plug_ip": target_ip,
        "energy_usage": usage,
        "mode": "direct",
    }


@app.get("/api/direct/plug/health")
async def direct_plug_health(
    plug_ip: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    """
    [Direct Mode] Health check — verify the plug is reachable through
    the WireGuard tunnel and responding to commands.
    """
    if not DIRECT_MODE or not tapo_driver:
        raise HTTPException(
            status_code=503,
            detail="Direct mode is not enabled. Set DIRECT_MODE=true and provide Tapo credentials.",
        )

    target_ip = _get_plug_ip(plug_ip)
    health = await tapo_driver.health_check(target_ip)

    return {
        "plug_ip": target_ip,
        "health": health,
        "mode": "direct",
    }


# ===========================================================================
# CPO (Charge Point Operator) Admin Endpoints
# ===========================================================================
# These endpoints power the CPO admin dashboard. All endpoints (except setup)
# require the user to have the 'cpo' role, enforced via the require_role()
# RBAC dependency.
#
# The CPO dashboard allows property owners/managers to:
# - Register and manage ESP32 gateways and smart plugs
# - Create and manage charger groups (public/private with access codes)
# - View analytics: session history, revenue, energy consumption
# ===========================================================================


# --- CPO Pydantic Schemas ---

class CpoSetupRequest(BaseModel):
    """Request body for CPO onboarding — creates a new tenant."""
    tenant_name: str


class CpoGatewayCreateRequest(BaseModel):
    """Register a new gateway under the CPO's tenant."""
    gateway_id: str   # MAC address or hardware UUID
    name: str
    vpn_ip: str


class CpoPlugCreateRequest(BaseModel):
    """Register a new plug on one of the CPO's gateways."""
    gateway_id: str
    name: str
    local_ip: str
    plug_model: str = "tapo_p110"
    group_id: Optional[int] = None
    # Optional geolocation; when omitted the plug inherits its gateway's coords.
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)


class CpoPlugUpdateRequest(BaseModel):
    """Update an existing plug's details."""
    name: Optional[str] = None
    group_id: Optional[int] = None
    # Status string matching PlugStatus enum values: available, occupied, offline, maintenance
    status: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)


class CpoGroupCreateRequest(BaseModel):
    """Create a new charger group."""
    name: str
    is_public: bool = False


class CpoGroupUpdateRequest(BaseModel):
    """Update an existing charger group."""
    name: Optional[str] = None
    is_public: Optional[bool] = None
    regenerate_access_code: bool = False


# --- CPO Setup & Profile ---

@app.post("/api/cpo/setup")
async def cpo_setup(
    req: CpoSetupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    CPO onboarding: create a new tenant (organization) and promote the
    current user to the 'cpo' role. This is a one-time operation — users
    who already have a tenant_id cannot call this again.

    This is the entry point for any driver who wants to become a Charge
    Point Operator and start managing their own plugs and groups.
    """
    # Prevent double-setup: if user already has a tenant, reject
    if user.tenant_id is not None:
        raise HTTPException(
            status_code=400,
            detail="You are already associated with a tenant. Cannot create another.",
        )

    # Check tenant name uniqueness
    existing = await db.execute(
        select(Tenant).where(Tenant.name == req.tenant_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail=f"A tenant with the name '{req.tenant_name}' already exists.",
        )

    # Create the tenant
    tenant = Tenant(name=req.tenant_name)
    db.add(tenant)
    try:
        await db.flush()  # Get the tenant.id before committing

        # Promote user to CPO role and link to the new tenant
        user.role = UserRole.CPO
        user.tenant_id = tenant.id
        await db.commit()
    except IntegrityError:
        # Lost the race to a concurrent setup with the same tenant name (the
        # name-uniqueness SELECT above can't see an uncommitted twin).
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"A tenant with the name '{req.tenant_name}' already exists.",
        )
    await db.refresh(user)
    await db.refresh(tenant)

    logger.info(f"CPO setup complete: user={user.email} → tenant='{tenant.name}' (id={tenant.id})")

    return {
        "status": "success",
        "tenant_id": tenant.id,
        "tenant_name": tenant.name,
        "user_role": user.role.value,
        "message": f"Welcome, CPO! Your organization '{tenant.name}' has been created.",
    }


@app.get("/api/cpo/profile")
async def cpo_profile(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Get the CPO's profile including tenant information and summary stats.
    """
    # Load tenant
    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == user.tenant_id)
    )
    tenant = tenant_result.scalar_one_or_none()

    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    # Count gateways and plugs
    gw_result = await db.execute(
        select(func.count(Gateway.id)).where(Gateway.tenant_id == tenant.id)
    )
    gateway_count = gw_result.scalar() or 0

    # Count plugs across all gateways owned by this tenant
    plug_result = await db.execute(
        select(func.count(Plug.id))
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(Gateway.tenant_id == tenant.id)
    )
    plug_count = plug_result.scalar() or 0

    # Count groups
    group_result = await db.execute(
        select(func.count(ChargerGroup.id)).where(ChargerGroup.tenant_id == tenant.id)
    )
    group_count = group_result.scalar() or 0

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role.value,
        },
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        },
        "stats": {
            "gateway_count": gateway_count,
            "plug_count": plug_count,
            "group_count": group_count,
        },
    }


# --- CPO Gateway Management ---

@app.get("/api/cpo/gateways")
async def cpo_list_gateways(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all gateways owned by the CPO's tenant."""
    result = await db.execute(
        select(Gateway).where(Gateway.tenant_id == user.tenant_id)
    )
    gateways = list(result.scalars().all())

    response = []
    for gw in gateways:
        # Count plugs on this gateway
        plug_count_result = await db.execute(
            select(func.count(Plug.id)).where(Plug.gateway_id == gw.id)
        )
        plug_count = plug_count_result.scalar() or 0

        response.append({
            "id": gw.id,
            "name": gw.name,
            "vpn_ip": gw.vpn_ip,
            "status": gw.status.value,
            "last_seen_at": gw.last_seen_at.isoformat() if gw.last_seen_at else None,
            "created_at": gw.created_at.isoformat() if gw.created_at else None,
            "plug_count": plug_count,
        })

    return response


@app.post("/api/cpo/gateways")
async def cpo_create_gateway(
    req: CpoGatewayCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new ESP32 gateway under the CPO's tenant.
    This is the authenticated version of the legacy /api/gateways/register
    endpoint — CPOs should use this instead.
    """
    # Check for duplicate gateway ID
    existing = await db.execute(select(Gateway).where(Gateway.id == req.gateway_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Gateway '{req.gateway_id}' already exists.")

    gateway = Gateway(
        id=req.gateway_id,
        tenant_id=user.tenant_id,
        name=req.name,
        vpn_ip=req.vpn_ip,
    )
    db.add(gateway)
    await db.commit()
    await db.refresh(gateway)

    logger.info(f"CPO gateway registered: {gateway.id} ({gateway.name}) by {user.email}")

    return {
        "status": "registered",
        "gateway_id": gateway.id,
        "name": gateway.name,
        "vpn_ip": gateway.vpn_ip,
    }


# --- CPO Plug Management ---

@app.get("/api/cpo/plugs")
async def cpo_list_plugs(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all plugs across the CPO's gateways with status and group info."""
    result = await db.execute(
        select(Plug)
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(Gateway.tenant_id == user.tenant_id)
    )
    plugs = list(result.scalars().all())

    response = []
    for plug in plugs:
        # Get group name if assigned
        group_name = None
        if plug.group_id:
            gn_result = await db.execute(
                select(ChargerGroup.name).where(ChargerGroup.id == plug.group_id)
            )
            row = gn_result.first()
            group_name = row[0] if row else None

        response.append({
            "id": plug.id,
            "name": plug.name,
            "gateway_id": plug.gateway_id,
            "local_ip": plug.local_ip,
            "plug_model": plug.plug_model,
            "status": plug.status.value,
            "current_power_w": plug.current_power_w,
            "group_id": plug.group_id,
            "group_name": group_name,
            "latitude": plug.latitude,
            "longitude": plug.longitude,
            "last_seen_at": plug.last_seen_at.isoformat() if plug.last_seen_at else None,
            "created_at": plug.created_at.isoformat() if plug.created_at else None,
        })

    return response


@app.post("/api/cpo/plugs")
async def cpo_create_plug(
    req: CpoPlugCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new smart plug on one of the CPO's gateways.
    Validates that the gateway belongs to the CPO's tenant.
    """
    # Verify the gateway belongs to this CPO's tenant
    gw_result = await db.execute(
        select(Gateway).where(
            and_(Gateway.id == req.gateway_id, Gateway.tenant_id == user.tenant_id)
        )
    )
    gateway = gw_result.scalar_one_or_none()
    if not gateway:
        raise HTTPException(
            status_code=404,
            detail=f"Gateway '{req.gateway_id}' not found or does not belong to your organization.",
        )

    # Validate group ownership if specified
    if req.group_id:
        group_result = await db.execute(
            select(ChargerGroup).where(
                and_(ChargerGroup.id == req.group_id, ChargerGroup.tenant_id == user.tenant_id)
            )
        )
        if not group_result.scalar_one_or_none():
            raise HTTPException(
                status_code=404,
                detail=f"Group {req.group_id} not found or does not belong to your organization.",
            )

    plug = Plug(
        gateway_id=req.gateway_id,
        name=req.name,
        local_ip=req.local_ip,
        plug_model=req.plug_model,
        group_id=req.group_id,
        latitude=req.latitude,
        longitude=req.longitude,
    )
    db.add(plug)
    await db.commit()
    await db.refresh(plug)

    logger.info(f"CPO plug registered: {plug.id} ({plug.name}) by {user.email}")

    return {
        "status": "registered",
        "plug_id": plug.id,
        "name": plug.name,
        "gateway_id": req.gateway_id,
    }


@app.put("/api/cpo/plugs/{plug_id}")
async def cpo_update_plug(
    plug_id: int,
    req: CpoPlugUpdateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Update an existing plug's details (name, group assignment, status).
    Only plugs on gateways owned by the CPO's tenant can be modified.
    """
    # Load plug and verify ownership via gateway → tenant chain
    result = await db.execute(
        select(Plug)
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(and_(Plug.id == plug_id, Gateway.tenant_id == user.tenant_id))
    )
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail="Plug not found or access denied.")

    # Apply updates
    if req.name is not None:
        plug.name = req.name
    if req.group_id is not None:
        # Validate group ownership
        if req.group_id != 0:  # 0 = remove from group
            group_result = await db.execute(
                select(ChargerGroup).where(
                    and_(ChargerGroup.id == req.group_id, ChargerGroup.tenant_id == user.tenant_id)
                )
            )
            if not group_result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="Group not found or access denied.")
            plug.group_id = req.group_id
        else:
            plug.group_id = None
    if req.status is not None:
        try:
            plug.status = PlugStatus(req.status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{req.status}'. Valid values: {[s.value for s in PlugStatus]}",
            )
    if req.latitude is not None:
        plug.latitude = req.latitude
    if req.longitude is not None:
        plug.longitude = req.longitude

    await db.commit()
    await db.refresh(plug)

    logger.info(f"CPO plug updated: {plug.id} ({plug.name}) by {user.email}")

    return {
        "status": "updated",
        "plug_id": plug.id,
        "name": plug.name,
        "plug_status": plug.status.value,
        "group_id": plug.group_id,
    }


# --- CPO Charger Group Management ---

@app.get("/api/cpo/groups")
async def cpo_list_groups(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all charger groups owned by the CPO's tenant."""
    result = await db.execute(
        select(ChargerGroup).where(ChargerGroup.tenant_id == user.tenant_id)
    )
    groups = list(result.scalars().all())

    response = []
    for group in groups:
        # Count plugs in this group
        plug_count_result = await db.execute(
            select(func.count(Plug.id)).where(Plug.group_id == group.id)
        )
        plug_count = plug_count_result.scalar() or 0

        # Count members (for private groups)
        member_count_result = await db.execute(
            select(func.count(GroupMembership.id)).where(GroupMembership.group_id == group.id)
        )
        member_count = member_count_result.scalar() or 0

        response.append({
            "id": group.id,
            "name": group.name,
            "is_public": group.is_public,
            "access_code": group.access_code if not group.is_public else None,
            "plug_count": plug_count,
            "member_count": member_count,
            "created_at": group.created_at.isoformat() if group.created_at else None,
        })

    return response


@app.post("/api/cpo/groups")
async def cpo_create_group(
    req: CpoGroupCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new charger group under the CPO's tenant.
    Private groups automatically get a generated access code.
    """
    import secrets
    import string

    access_code = None
    if not req.is_public:
        # Generate a unique 8-character alphanumeric access code
        # Using uppercase letters and digits for easy manual entry
        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            existing = await db.execute(
                select(ChargerGroup).where(ChargerGroup.access_code == code)
            )
            if not existing.scalar_one_or_none():
                access_code = code
                break

    group = ChargerGroup(
        tenant_id=user.tenant_id,
        name=req.name,
        is_public=req.is_public,
        access_code=access_code,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)

    logger.info(f"CPO group created: '{group.name}' (id={group.id}, public={group.is_public}) by {user.email}")

    return {
        "status": "created",
        "group_id": group.id,
        "name": group.name,
        "is_public": group.is_public,
        "access_code": access_code,
    }


@app.put("/api/cpo/groups/{group_id}")
async def cpo_update_group(
    group_id: int,
    req: CpoGroupUpdateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a charger group's details. Can regenerate access codes for private groups."""
    import secrets
    import string

    result = await db.execute(
        select(ChargerGroup).where(
            and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found or access denied.")

    if req.name is not None:
        group.name = req.name

    if req.is_public is not None:
        group.is_public = req.is_public
        # If switching to public, clear the access code
        if req.is_public:
            group.access_code = None
        # If switching to private, generate a new access code
        elif not group.access_code:
            while True:
                code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
                existing = await db.execute(
                    select(ChargerGroup).where(ChargerGroup.access_code == code)
                )
                if not existing.scalar_one_or_none():
                    group.access_code = code
                    break

    if req.regenerate_access_code and not group.is_public:
        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            existing = await db.execute(
                select(ChargerGroup).where(ChargerGroup.access_code == code)
            )
            if not existing.scalar_one_or_none():
                group.access_code = code
                break

    await db.commit()
    await db.refresh(group)

    logger.info(f"CPO group updated: '{group.name}' (id={group.id}) by {user.email}")

    return {
        "status": "updated",
        "group_id": group.id,
        "name": group.name,
        "is_public": group.is_public,
        "access_code": group.access_code,
    }


@app.delete("/api/cpo/groups/{group_id}")
async def cpo_delete_group(
    group_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a charger group. Plugs assigned to this group will have their
    group_id set to NULL (they become ungrouped, visible to all users).
    """
    result = await db.execute(
        select(ChargerGroup).where(
            and_(ChargerGroup.id == group_id, ChargerGroup.tenant_id == user.tenant_id)
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found or access denied.")

    group_name = group.name

    # Unlink all plugs from this group before deletion
    plugs_result = await db.execute(
        select(Plug).where(Plug.group_id == group_id)
    )
    for plug in plugs_result.scalars().all():
        plug.group_id = None

    # Delete all memberships (cascade should handle this, but be explicit)
    memberships_result = await db.execute(
        select(GroupMembership).where(GroupMembership.group_id == group_id)
    )
    for membership in memberships_result.scalars().all():
        await db.delete(membership)

    await db.delete(group)
    await db.commit()

    logger.info(f"CPO group deleted: '{group_name}' (id={group_id}) by {user.email}")

    return {"status": "deleted", "group_id": group_id, "group_name": group_name}


# --- CPO Analytics ---

@app.get("/api/cpo/analytics/overview")
async def cpo_analytics_overview(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Summary analytics for the CPO dashboard:
    - Total plugs, active sessions, gateways online/offline
    - Today's energy consumption and revenue
    - All-time totals
    """
    tenant_id = user.tenant_id

    # Total plugs count
    plug_count_result = await db.execute(
        select(func.count(Plug.id))
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(Gateway.tenant_id == tenant_id)
    )
    total_plugs = plug_count_result.scalar() or 0

    # Active sessions right now
    active_sessions_result = await db.execute(
        select(func.count(ChargingSession.id))
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.status == SessionStatus.ACTIVE,
        ))
    )
    active_sessions = active_sessions_result.scalar() or 0

    # Gateways online/offline
    gw_online_result = await db.execute(
        select(func.count(Gateway.id))
        .where(and_(Gateway.tenant_id == tenant_id, Gateway.status == GatewayStatus.ONLINE))
    )
    gateways_online = gw_online_result.scalar() or 0

    gw_total_result = await db.execute(
        select(func.count(Gateway.id)).where(Gateway.tenant_id == tenant_id)
    )
    gateways_total = gw_total_result.scalar() or 0

    # Today's stats (energy and revenue from completed sessions started today)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_stats_result = await db.execute(
        select(
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            func.count(ChargingSession.id),
        )
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.started_at >= today_start,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
    )
    today_row = today_stats_result.first()
    today_energy = float(today_row[0]) if today_row else 0.0
    today_revenue = float(today_row[1]) if today_row else 0.0
    today_sessions = int(today_row[2]) if today_row else 0

    # All-time stats
    alltime_result = await db.execute(
        select(
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            func.count(ChargingSession.id),
        )
        .where(and_(
            ChargingSession.tenant_id == tenant_id,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
    )
    alltime_row = alltime_result.first()
    alltime_energy = float(alltime_row[0]) if alltime_row else 0.0
    alltime_revenue = float(alltime_row[1]) if alltime_row else 0.0
    alltime_sessions = int(alltime_row[2]) if alltime_row else 0

    return {
        "plugs": {"total": total_plugs},
        "gateways": {"online": gateways_online, "total": gateways_total},
        "active_sessions": active_sessions,
        "today": {
            "sessions": today_sessions,
            "energy_kwh": round(today_energy, 3),
            "revenue_coins": round(today_revenue, 2),
        },
        "all_time": {
            "sessions": alltime_sessions,
            "energy_kwh": round(alltime_energy, 3),
            "revenue_coins": round(alltime_revenue, 2),
        },
    }


@app.get("/api/cpo/analytics/sessions")
async def cpo_analytics_sessions(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    plug_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    days: int = 30,
    limit: int = 50,
):
    """
    Session history across all of the CPO's plugs.
    Supports optional filters: plug_id, status, and date range (days).
    Returns sessions ordered by most recent first.
    """
    # Base query: sessions belonging to this CPO's tenant
    query = select(ChargingSession).where(
        ChargingSession.tenant_id == user.tenant_id
    )

    # Apply optional filters
    if plug_id:
        query = query.where(ChargingSession.plug_id == plug_id)
    if status_filter:
        try:
            status_enum = SessionStatus(status_filter)
            query = query.where(ChargingSession.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status filter '{status_filter}'. Valid: {[s.value for s in SessionStatus]}",
            )

    # Date range filter
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query = query.where(ChargingSession.started_at >= cutoff)

    # Order and limit
    query = query.order_by(ChargingSession.started_at.desc()).limit(limit)

    result = await db.execute(query)
    sessions = list(result.scalars().all())

    # Enrich with plug name and user email
    response = []
    for s in sessions:
        # Get plug name
        plug_result = await db.execute(select(Plug.name).where(Plug.id == s.plug_id))
        plug_row = plug_result.first()
        plug_name = plug_row[0] if plug_row else f"Plug #{s.plug_id}"

        # Get user email
        user_result = await db.execute(select(User.email).where(User.id == s.user_id))
        user_row = user_result.first()
        user_email = user_row[0] if user_row else "unknown"

        # Calculate duration
        duration_minutes = None
        if s.ended_at and s.started_at:
            duration_minutes = round((s.ended_at - s.started_at).total_seconds() / 60, 1)

        response.append({
            "id": s.id,
            "plug_id": s.plug_id,
            "plug_name": plug_name,
            "user_id": s.user_id,
            "user_email": user_email,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "duration_minutes": duration_minutes,
            "energy_kwh": round(s.energy_kwh, 3),
            "coins_spent": round(s.coins_spent, 2),
            "status": s.status.value,
        })

    return response


@app.get("/api/cpo/analytics/revenue")
async def cpo_analytics_revenue(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    """
    Daily revenue breakdown for the CPO's charting dashboard.
    Returns an array of {date, revenue_coins, session_count} for each day
    in the requested range, suitable for plotting a revenue trend line.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Query: group completed sessions by date, sum revenue
    result = await db.execute(
        select(
            cast(ChargingSession.started_at, Date).label("date"),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0).label("revenue"),
            func.count(ChargingSession.id).label("count"),
        )
        .where(and_(
            ChargingSession.tenant_id == user.tenant_id,
            ChargingSession.started_at >= cutoff,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
        .group_by(cast(ChargingSession.started_at, Date))
        .order_by(cast(ChargingSession.started_at, Date))
    )

    rows = result.all()

    return [
        {
            "date": str(row[0]),
            "revenue_coins": round(float(row[1]), 2),
            "session_count": int(row[2]),
        }
        for row in rows
    ]


@app.get("/api/cpo/analytics/energy")
async def cpo_analytics_energy(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    """
    Daily energy consumption breakdown for the CPO's charting dashboard.
    Returns an array of {date, energy_kwh, session_count} for each day
    in the requested range, suitable for plotting an energy consumption chart.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(
            cast(ChargingSession.started_at, Date).label("date"),
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0).label("energy"),
            func.count(ChargingSession.id).label("count"),
        )
        .where(and_(
            ChargingSession.tenant_id == user.tenant_id,
            ChargingSession.started_at >= cutoff,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        ))
        .group_by(cast(ChargingSession.started_at, Date))
        .order_by(cast(ChargingSession.started_at, Date))
    )

    rows = result.all()

    return [
        {
            "date": str(row[0]),
            "energy_kwh": round(float(row[1]), 3),
            "session_count": int(row[2]),
        }
        for row in rows
    ]


@app.get("/api/cpo/analytics/telemetry")
async def cpo_analytics_telemetry(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    plug_id: Optional[int] = None,
    days: int = 1,
    bucket: str = "hour",
):
    """
    Downsampled time-series telemetry for the CPO's load graphs / energy audits.

    Buckets raw `telemetry_readings` via date_trunc and returns average / peak
    power plus the cumulative energy reading per bucket. Tenant-scoped (uses the
    denormalized telemetry_readings.tenant_id); optional plug_id filter.

    Returns an array of
    {timestamp, avg_power_w, max_power_w, energy_kwh, sample_count}.
    """
    allowed_buckets = {"minute", "hour", "day"}
    if bucket not in allowed_buckets:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bucket '{bucket}'. Allowed: {sorted(allowed_buckets)}.",
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    bucket_col = func.date_trunc(bucket, TelemetryReading.recorded_at).label("bucket")

    conditions = [
        TelemetryReading.tenant_id == user.tenant_id,
        TelemetryReading.recorded_at >= cutoff,
    ]
    if plug_id is not None:
        conditions.append(TelemetryReading.plug_id == plug_id)

    result = await db.execute(
        select(
            bucket_col,
            func.avg(TelemetryReading.power_w).label("avg_power_w"),
            func.max(TelemetryReading.power_w).label("max_power_w"),
            # energy_kwh is cumulative-per-session, so max() = value at end of bucket
            func.max(TelemetryReading.energy_kwh).label("energy_kwh"),
            func.count(TelemetryReading.id).label("sample_count"),
        )
        .where(and_(*conditions))
        .group_by(bucket_col)
        .order_by(bucket_col)
    )

    rows = result.all()

    return [
        {
            "timestamp": row[0].isoformat() if row[0] else None,
            "avg_power_w": round(float(row[1]), 1),
            "max_power_w": round(float(row[2]), 1),
            "energy_kwh": round(float(row[3]), 3),
            "sample_count": int(row[4]),
        }
        for row in rows
    ]


# Wrap FastAPI app with Socket.io ASGI wrapper so they run on the same port
import socketio
from backend.services.socketio_manager import sio
app = socketio.ASGIApp(sio, other_asgi_app=app)

