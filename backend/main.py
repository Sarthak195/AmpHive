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

import logging
import os
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from backend.database.db import async_session_factory, init_db
from backend.logging_config import configure_logging, set_correlation_id
from backend.services.mqtt_manager import MQTTManager
from backend.services.session_reaper import SessionReaperService

# [Direct Mode] Import the Tapo direct driver for ESP32-bypass plug control
from backend.services.tapo_direct import TapoDirectDriver
from backend.services.telemetry import COINS_PER_KWH
from backend.services.telemetry_persistence import TelemetryPersistenceService

# Load environment variables from .env file (for local development)
load_dotenv()

# Structured JSON logging + correlation ids (TD#28) — replaces the old
# logging.basicConfig(level=logging.INFO). See backend/logging_config.py.
configure_logging()
logger = logging.getLogger("amphive.api")

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
from backend import state  # noqa: E402
from backend.services.session_lifecycle import (  # noqa: E402
    finalize_charging_session,
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
        # Real domain (2026-07-20; .app is HSTS-preloaded so https-only).
        # duckdns origins retired 2026-07-20 after the amphive.app cutover.
        "https://amphive.app",
        "https://cpo.amphive.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Correlation ID middleware (TD#28) ---
# Every request gets a correlation id — the caller's X-Request-ID if one was
# sent, else a short generated one — bound to a contextvars.ContextVar for the
# lifetime of the request's asyncio task. Every log line emitted while
# handling the request (including a synchronous MQTT publish made directly
# from a route handler, e.g. sessions.start_charging_session -> send_plug_
# command) picks it up via logging_config.CorrelationIdFilter, and it's
# echoed back on the response so a client or operator can grep logs for one
# request end-to-end (HTTP request -> MQTT command -> session).
@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    set_correlation_id(correlation_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = correlation_id
    return response


# ===========================================================================
# Pydantic Request/Response Schemas — moved to backend/schemas.py (TD#7)
# ===========================================================================



# ===========================================================================
# Health Check
# ===========================================================================

@app.get("/api/health")
def health_check():
    """Service health check endpoint."""
    return {"status": "healthy", "service": "amphive-backend", "version": "2.0.0"}


@app.get("/api/config")
def public_config():
    """
    Public pricing/config the frontend needs to show accurate numbers instead of
    hardcoding them: the charging tariff (coins per kWh), the minimum balance to
    start a session (matches the 402 the start path enforces), and the coin↔INR
    rate (the top-up flow mints coins 1:1 with rupees).
    """
    from backend.routers.sessions import MIN_START_BALANCE_COINS
    return {
        "coins_per_kwh": COINS_PER_KWH,
        "min_start_balance_coins": MIN_START_BALANCE_COINS,
        "coin_inr_rate": 1.0,
        "currency": "INR",
        # [Google OAuth] Gates the frontend's "Continue with Google" button.
        # Mirrors GOOGLE_CLIENT_ID specifically (not the other two Google env
        # vars) — the authoritative full-config check lives in
        # routers/auth.py's google_login()/google_callback(), which 503 if
        # GOOGLE_CLIENT_SECRET or GOOGLE_OAUTH_REDIRECT_URI is missing even
        # when this flag is true.
        "google_login_enabled": bool(os.getenv("GOOGLE_CLIENT_ID")),
    }



# ===========================================================================
# Routers — the 35 API routes live in backend/routers/ (TD#7 split);
# include order mirrors the original in-file order so OpenAPI stays stable.
# ===========================================================================

from backend.routers import (  # noqa: E402
    admin,
    auth,
    cpo,
    direct,
    groups,
    notifications,
    payments,
    plugs,
    reservations,
    sessions,
)

app.include_router(auth.router)
app.include_router(groups.router)
app.include_router(plugs.router)
app.include_router(sessions.router)
app.include_router(payments.router)
app.include_router(notifications.router)
app.include_router(direct.router)
app.include_router(cpo.router)
# Appended after the original eight so their OpenAPI order stays stable
# (feat/reservations, 2026-07-12).
app.include_router(reservations.router)
# Platform-admin console surface (redesign/ui-v3, 2026-07-21) — appended
# last, same OpenAPI-order rationale as reservations.
app.include_router(admin.router)

import socketio  # noqa: E402

from backend.services.socketio_manager import sio  # noqa: E402

app = socketio.ASGIApp(sio, other_asgi_app=app)

