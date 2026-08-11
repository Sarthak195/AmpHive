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
from fastapi.responses import JSONResponse

from backend.database.db import async_session_factory, init_db
from backend.logging_config import configure_logging, set_correlation_id
from backend.services.mqtt_manager import MQTTManager
from backend.services.rate_limit import api_rate_limit_middleware
from backend.services.session_reaper import SessionReaperService
from backend.services.socketio_manager import cors_allowed_origins
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

# --- Shared runtime state + session-lifecycle helpers (TD#7 split) ---
# Mutable runtime handles live in backend/state.py (set below in lifespan);
# the session helpers moved verbatim to services/session_lifecycle.py.
from backend import state  # noqa: E402
from backend.services.session_lifecycle import (  # noqa: E402
    finalize_charging_session,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage MQTT connection lifecycle with the FastAPI application."""
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

# --- Blanket per-IP API rate limit (SECURITY.md §8.6) ---
# Registered BEFORE CORSMiddleware on purpose: the middleware added last is
# the outermost, so CORS ends up wrapping this limiter — preflight OPTIONS
# are answered by CORS without spending budget, and a 429 still gets CORS
# headers stamped on the way out (a cross-origin page can read the error).
app.middleware("http")(api_rate_limit_middleware)

# --- Request body-size cap (DoS hardening) ---
# nginx's compiled-in ~1 MB default is the only bound today, and it's
# undeclared (see frontend/nginx.conf's explicit client_max_body_size for the
# defense-in-depth companion to this) — a request that reaches uvicorn
# directly (e.g. the VM-local :8000 debug port, bypassing nginx entirely) had
# ZERO app-layer limit. This is a pure JSON API, so a cheap Content-Length
# check is sufficient; there is no need to stream-count the body. A request
# with no Content-Length (chunked transfer) is let through unchecked — nginx
# still caps it in every deployed environment.
#
# Registered here (AFTER api_rate_limit_middleware, BEFORE CORSMiddleware)
# so it runs early relative to the rest of the stack — an oversized request
# is rejected before it can spend any rate-limit budget — while still being
# wrapped BY CORSMiddleware (registered next), same rationale as the limiter
# above: a 413 still gets CORS headers stamped on the way out. Reads
# MAX_REQUEST_BODY_BYTES as a module global (not a closed-over parameter),
# mirroring api_rate_limiter's "looked up per-call" pattern in
# services/rate_limit.py, so tests can monkeypatch it directly.
MAX_REQUEST_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", 1024 * 1024))

# [Firmware upload] The ONE endpoint that legitimately carries a multi-MB
# body: POST /api/admin/firmware-releases/upload takes a raw signed OTA image
# (~1 MB today — 2.5.0-direct is 1,048,564 bytes, 12 bytes under the global
# 1 MiB cap — and the OTA app partition allows up to 1.9 MB). That path gets
# its own ceiling, matching routers/admin.py's _FIRMWARE_MAX_BYTES endpoint
# guard (which re-checks the actual body) and the per-location
# client_max_body_size carve-out in frontend/nginx.conf.
FIRMWARE_UPLOAD_MAX_BYTES = int(os.getenv("FIRMWARE_UPLOAD_MAX_BYTES", 4 * 1024 * 1024))
_FIRMWARE_UPLOAD_PATH = "/api/admin/firmware-releases/upload"


@app.middleware("http")
async def max_body_size_middleware(request: Request, call_next):
    """Reject a request whose declared Content-Length exceeds the cap with a
    413 and a JSON {"detail": ...} body, before it reaches routing/handlers."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None:
            cap = (
                FIRMWARE_UPLOAD_MAX_BYTES
                if request.url.path == _FIRMWARE_UPLOAD_PATH
                else MAX_REQUEST_BODY_BYTES
            )
            if declared_length > cap:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"Request body too large ({declared_length} bytes; "
                            f"max {cap} bytes)."
                        )
                    },
                )
    return await call_next(request)


# --- CORS Middleware ---
# Allow the frontend (running on a different port/domain) to make API requests.
# The allowlist is the shared source of truth in services/socketio_manager.py:
# the real .app domains always, plus any CORS_EXTRA_ORIGINS (empty in prod;
# set to the localhost dev servers for local development). Localhost is NOT
# trusted by default — with allow_credentials=True that would let a localhost
# page ride a user's credentials against prod. Real domains use HSTS-preloaded
# .app (https-only); duckdns origins retired 2026-07-20 after the cutover.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
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
    firmware_images,
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
# Public OTA image host (GET /api/firmware/images/{filename}) — unauthenticated
# by design (the device verifies the image's ECDSA signature, not the
# transport); kept next to the other public/plug routers.
app.include_router(firmware_images.router)
app.include_router(sessions.router)
app.include_router(payments.router)
app.include_router(notifications.router)
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

