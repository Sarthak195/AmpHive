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
from backend.services.session_reaper import SessionReaperService
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

# --- [Direct Mode] Tapo P110 Configuration ---
# When DIRECT_MODE=true, the backend can control the plug directly via the
# `tapo` Python library, bypassing the ESP32 gateway and MQTT broker.
# This is used for development/testing before the ESP32 board is available.
DIRECT_MODE = os.getenv("DIRECT_MODE", "false").lower() == "true"
TAPO_USERNAME = os.getenv("TAPO_USERNAME", "")
TAPO_PASSWORD = os.getenv("TAPO_PASSWORD", "")
TAPO_PLUG_IP = os.getenv("TAPO_PLUG_IP", "")

# --- Shared runtime state + session-lifecycle helpers (TD#7 split) ---
# Mutable runtime handles live in backend/state.py (set below in lifespan);
# the session helpers moved verbatim to services/session_lifecycle.py.
from backend import state
from backend.services.session_lifecycle import (  # noqa: E402
    check_and_speed_up_active_session,
    finalize_charging_session,
    gateway_is_live,
    set_plug_telemetry_interval,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage MQTT and Tapo Direct connection lifecycles with the FastAPI application."""
    # Initialize the database tables
    await init_db()

    import asyncio
    loop = asyncio.get_running_loop()

    # Start the telemetry persistence flush loop before MQTT so the buffer is
    # ready to receive enqueued readings as soon as messages arrive.
    state.telemetry_persistence = TelemetryPersistenceService(
        db_session_factory=async_session_factory,
    )
    state.telemetry_persistence.start(loop)

    # Initialize and start the MQTT connection.
    # Pass telemetry_store so inbound MQTT data feeds the live stream,
    # db_session_factory so session totals are persisted, and
    # telemetry_persistence so raw samples are buffered into telemetry_readings.
    state.mqtt_manager = MQTTManager(
        broker_host=MQTT_BROKER_HOST,
        broker_port=MQTT_BROKER_PORT,
        username=MQTT_USERNAME,
        password=MQTT_PASSWORD,
        telemetry_store=state.telemetry_store,
        db_session_factory=async_session_factory,
        event_loop=loop,
        telemetry_persistence=state.telemetry_persistence,
    )
    state.mqtt_manager.start()

    # [Direct Mode] Initialize the Tapo direct driver if enabled.
    # This allows controlling the plug without an ESP32 gateway, useful for
    # development/testing. The plug is reached via a WireGuard tunnel to
    # the developer's home network.
    if DIRECT_MODE and TAPO_USERNAME and TAPO_PASSWORD:
        state.tapo_driver = TapoDirectDriver(
            tapo_email=TAPO_USERNAME,
            tapo_password=TAPO_PASSWORD,
        )
        logger.info(f"🔌 Direct Mode ENABLED — Tapo plug target: {TAPO_PLUG_IP}")
    else:
        logger.info("Direct Mode DISABLED — using standard ESP32/MQTT path")

    # Auto-finalize ACTIVE sessions whose telemetry has gone silent (dead
    # gateway mid-session). Shares finalize_charging_session with the stop
    # route, so reaping bills/frees exactly like a user-initiated stop.
    state.session_reaper = SessionReaperService(
        db_session_factory=async_session_factory,
        finalize=finalize_charging_session,
    )
    state.session_reaper.start(loop)

    yield
    # Stop MQTT first (no new enqueues), then drain + stop the flush loop so the
    # buffered tail is persisted.
    if state.session_reaper:
        await state.session_reaper.stop()
    if state.mqtt_manager:
        state.mqtt_manager.stop()
    if state.telemetry_persistence:
        await state.telemetry_persistence.stop()


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
# Pydantic Request/Response Schemas — moved to backend/schemas.py (TD#7)
# ===========================================================================

from backend.schemas import (  # noqa: E402
    AuthResponse, CpoGatewayCreateRequest, CpoGroupCreateRequest,
    CpoGroupUpdateRequest, CpoPlugCreateRequest, CpoPlugUpdateRequest,
    CpoSetupRequest, CreateOrderRequest, CreateOrderResponse,
    DirectPlugRequest, GatewayRegisterRequest, GroupResponse,
    JoinGroupRequest, LoginRequest, PlugRegisterRequest, PlugResponse,
    RegisterRequest, SessionStartRequest, SessionStopRequest, UserResponse,
    VerifyPaymentRequest,
)


# ===========================================================================
# Health Check
# ===========================================================================

@app.get("/api/health")
def health_check():
    """Service health check endpoint."""
    return {"status": "healthy", "service": "amphive-backend", "version": "2.0.0"}



# ===========================================================================
# Routers — the 35 API routes live in backend/routers/ (TD#7 split);
# include order mirrors the original in-file order so OpenAPI stays stable.
# ===========================================================================

from backend.routers import auth, cpo, direct, groups, payments, plugs, sessions  # noqa: E402

app.include_router(auth.router)
app.include_router(groups.router)
app.include_router(plugs.router)
app.include_router(sessions.router)
app.include_router(payments.router)
app.include_router(direct.router)
app.include_router(cpo.router)

import socketio
from backend.services.socketio_manager import sio
app = socketio.ASGIApp(sio, other_asgi_app=app)

