import asyncio
import logging
import os
from typing import Any, Dict

import socketio
from sqlalchemy import and_, select

from backend.database.db import async_session_factory
from backend.database.models import ChargingSession, SessionStatus, User
from backend.services.auth import decode_access_token, is_cpo_viewer, is_sid_revoked, token_sid
from backend.services.rate_limit import (
    client_ip_from_forwarded,
    socketio_connect_rate_limiter,
)
from backend.services.telemetry import TelemetrySnapshot, TelemetryStore

logger = logging.getLogger("amphive.socketio")

# Production CORS allowlist — the REAL front-end domains, always allowed. This
# is the single source of truth for both the Socket.io server (below) and the
# FastAPI app (main.py imports cors_allowed_origins), so the two can no longer
# drift out of sync. Note there is deliberately NO localhost here: with
# allow_credentials=True, echoing + crediting http://localhost:* on a default
# prod deploy would let a page served from localhost ride a logged-in user's
# credentials against prod. Localhost is opt-in for dev only (see below).
_PROD_ALLOWED_ORIGINS = [
    "https://amphive.app",
    "https://cpo.amphive.app",
]


def cors_allowed_origins() -> list[str]:
    """The CORS allowlist shared by the FastAPI app and the Socket.io server.

    The real domains are always present. Extra origins — typically the Vite
    (5173) / CRA (3000) localhost dev servers — are OPT-IN via the
    CORS_EXTRA_ORIGINS env var (comma-separated), which is EMPTY in production
    so a default prod deploy never trusts a localhost origin. To develop
    against a local frontend, set e.g.:

        CORS_EXTRA_ORIGINS=http://localhost:5173,http://localhost:3000

    Read at call time (not import-frozen) so a dev shell / test can set it
    without reimporting. Deduped + order-preserving so an origin already in the
    prod list can't be double-listed.
    """
    origins = list(_PROD_ALLOWED_ORIGINS)
    for origin in os.getenv("CORS_EXTRA_ORIGINS", "").split(","):
        origin = origin.strip()
        if origin and origin not in origins:
            origins.append(origin)
    return origins


# Create the Socket.io server. Same allowlist as main.py's FastAPI CORS.
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins=cors_allowed_origins())

# Active background telemetry tasks by session_id
active_streams: Dict[int, asyncio.Task] = {}

# How long a plug stays at the fast (1 s) cadence after its last live viewer
# leaves. A page reload drops the old socket before the new one subscribes;
# slowing the gateway to 10 s in that gap made a reloaded session look frozen
# for ~10 s, because the firmware sleeps out the whole slow interval before it
# honours the next SET_INTERVAL. Within the grace window the new subscriber's
# listener keeps the count above zero, so no slowdown is ever sent.
TELEMETRY_SLOWDOWN_GRACE_SEC = float(os.getenv("TELEMETRY_SLOWDOWN_GRACE_SEC", "30"))
# Strong refs so pending release tasks are not garbage-collected mid-sleep.
_release_tasks: set = set()


async def release_listener_later(plug_id: int) -> None:
    """Drop one live-view listener for `plug_id` after the grace window, and
    slow the gateway to 10 s only if nobody else is watching by then."""
    from backend.services.session_lifecycle import set_plug_telemetry_interval
    try:
        await asyncio.sleep(TELEMETRY_SLOWDOWN_GRACE_SEC)
        if telemetry_store.decrement_listeners(plug_id) == 0:
            async with async_session_factory() as db:
                await set_plug_telemetry_interval(db, plug_id, 10000)
    except Exception:
        logger.exception("Telemetry slowdown failed", extra={"plug_id": plug_id})

telemetry_store = TelemetryStore()


async def emit_plug_status(plug_id: int, status: str) -> None:
    """
    Broadcast a plug's availability change to every connected client so their
    charger lists flip OCCUPIED/AVAILABLE live, without a page refresh. Called
    from the session start/stop paths (and any other plug-status transition).
    The broadcast is global — plug availability isn't sensitive, and the frontend
    only updates plugs it is already displaying (a non-matching id is a no-op).
    """
    try:
        await sio.emit("plug_status", {"plug_id": plug_id, "status": status})
    except Exception as e:
        logger.error(f"Failed to emit plug_status for plug {plug_id}: {e}")


async def emit_plug_connectivity(plug_id: int, gateway_online: bool) -> None:
    """
    Broadcast a plug's gateway-connectivity change to every connected client the
    instant its gateway goes offline/online, so charger lists can flag a plug as
    unreachable (and clear it on reconnect) without waiting on the telemetry
    timeout. Global broadcast like emit_plug_status — connectivity isn't
    sensitive, and clients only update plugs they already display.
    """
    try:
        await sio.emit("plug_connectivity", {"plug_id": plug_id, "gateway_online": gateway_online})
    except Exception as e:
        logger.error(f"Failed to emit plug_connectivity for plug {plug_id}: {e}")


async def emit_notification(user_id: int, notification: Dict[str, Any]) -> None:
    """
    Deliver a driver notification to that user's connected clients only
    (their per-user room, joined on connect). Unlike plug_status /
    gateway_alarm this is NOT a broadcast — a notification carries
    wallet/session details that belong to one user.

    Note: we do not re-authorize each socket in the room per push. The room
    is keyed by user id, not by token, so a revoked-token socket and a valid
    fresh-token socket for the same user can coexist here, and there is no
    single tv or sid to gate the whole emit on. Revocation is instead enforced
    when a stale socket next interacts (see _socket_user_still_valid, which
    boots it from all rooms). The residual is a fully idle revoked socket
    (stale token_version OR a sid revoked by per-device logout) that keeps
    receiving these pushes until it disconnects or re-interacts: bounded, and
    the logging-out tab closes its own socket anyway.
    """
    try:
        await sio.emit("notification", notification, room=f"user_{user_id}")
    except Exception as e:
        logger.error(f"Failed to emit notification for user {user_id}: {e}")


async def emit_gateway_alarm(event: Dict[str, Any]) -> None:
    """
    Broadcast a gateway alarm/event (safety cutoff, unauthorized-on, OTA notice)
    to every connected client. The CPO portal renders it in an alert feed; a
    driver whose active plug matches reacts (e.g. an unauthorized-on warning).
    Broadcast is global for simplicity — the payload carries no wallet/PII, only
    operational fault metadata, and clients filter to what they display.
    """
    try:
        await sio.emit("gateway_alarm", event)
    except Exception as e:
        logger.error(f"Failed to emit gateway_alarm: {e}")


@sio.event
async def connect(sid, environ, auth=None):
    """
    Handle connection. Authenticate the JWT token.

    The token must arrive in the Socket.io auth payload ({ "token": "..." }),
    which travels in the CONNECT packet body — never in the URL. The old
    `?token=` query-string fallback (an SSE-era leftover no client used) was
    removed 2026-07-09: query strings land in proxy/access logs, so accepting
    a full JWT there turns every log line into a bearer credential.
    """
    # Per-IP handshake cap FIRST — before the JWT decode and the users SELECT
    # below, so a connect flood can't spend DB work per attempt. `environ` is
    # the WSGI-style dict python-socketio hands the connect handler; header
    # names are uppercased with dashes turned into underscores there.
    env = environ or {}
    peer = None
    client = env.get("asgi.scope", {}).get("client")
    if isinstance(client, (tuple, list)) and client:
        peer = client[0]
    ip = client_ip_from_forwarded(
        env.get("HTTP_X_FORWARDED_FOR", ""),
        peer or env.get("REMOTE_ADDR"),
    )
    if socketio_connect_rate_limiter.check(ip) is not None:
        logger.warning("Socket connection rejected: rate limited (sid: %s)", sid)
        return False

    token = None
    if auth and isinstance(auth, dict):
        token = auth.get("token")

    if not token:
        logger.warning(f"Socket connection rejected: No token provided (sid: {sid})")
        return False  # Refuses connection

    try:
        payload = decode_access_token(token)
        if not payload or not payload.get("sub"):
            logger.warning(f"Socket connection rejected: Invalid token (sid: {sid})")
            return False

        user_id = int(payload.get("sub"))

        # Reject tokens issued before the user's current token epoch (log out
        # everywhere / password change / admin revoke bumps token_version) and
        # tokens whose sign-in session was revoked by per-device logout,
        # mirroring the HTTP auth check in
        # backend.services.auth.get_current_user. NB: python-socketio's `sid`
        # here is the SOCKET id; the token's session id is `auth_sid`.
        auth_sid = token_sid(payload)
        revoked = False
        async with async_session_factory() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user is not None and payload.get("tv", 0) == user.token_version:
                revoked = await is_sid_revoked(db, auth_sid)

        if user is None or payload.get("tv", 0) != user.token_version or revoked:
            logger.warning(f"Socket connection rejected: Revoked or unknown user (sid: {sid})")
            return False

        # Save user info in connection session. Persist the token's `tv`
        # (token_version) claim alongside the id so authenticated event
        # handlers can re-validate the account's *current* state on later
        # events without re-decoding the JWT — connect() is the only place the
        # raw token is available. See _socket_user_still_valid.
        # The token's sign-in session id rides along under `auth_sid` (only
        # when the token has one) so the same re-validation also catches a
        # per-device logout.
        socket_session = {"user_id": user_id, "tv": payload.get("tv", 0)}
        # [F10] A read-only cpo_viewer may connect (it only receives pushes)
        # but may not emit anything that acts: see subscribe_session.
        if is_cpo_viewer(user):
            socket_session["read_only"] = True
        if auth_sid is not None:
            socket_session["auth_sid"] = auth_sid
        await sio.save_session(sid, socket_session)
        # Per-user room: lets the backend target one user's live clients
        # (driver notifications) without tracking sids ourselves.
        await sio.enter_room(sid, f"user_{user_id}")
        logger.info(f"Socket connected: user {user_id} (sid: {sid})")
        return True
    except Exception as e:
        logger.error(f"Socket connection authentication error: {e}")
        return False

@sio.event
async def disconnect(sid):
    logger.info(f"Socket disconnected (sid: {sid})")


async def _socket_user_still_valid(sid) -> bool:
    """
    Re-authorize an already-connected socket against the account's CURRENT
    state. connect() authenticates only once, at CONNECT time, then the
    connection lives indefinitely. Without this, a socket that was valid at
    connect keeps acting for the user (subscribe_session / telemetry) and keeps
    receiving that user's private pushes to room user_{id} even after the
    account was logged-out-everywhere / password-reset / demoted / disabled
    (all of which bump users.token_version; disable also sets is_disabled) or
    after that device's sign-in session was revoked by per-device logout. The
    HTTP layer re-checks all three on every request (backend.services.auth
    .get_current_user); this brings the socket layer to parity.

    Returns True only if the saved session names a user who still exists, is
    not disabled, whose token_version still matches the `tv` captured at
    connect, and whose sign-in session (`auth_sid`, when the token had one)
    is not in revoked_sessions. Callers MUST force-disconnect (await
    sio.disconnect(sid)) on False: leaving all rooms is what stops future
    server-pushed notifications to a revoked socket.

    Residual: a fully idle revoked socket that never emits another event keeps
    receiving pushes until it disconnects or next interacts (at which point
    this guard boots it). Revocation is enforced at the socket's next touch,
    not instantaneously mid-idle.
    """
    session = await sio.get_session(sid)
    user_id = session.get("user_id")
    if not user_id:
        return False
    saved_tv = session.get("tv", 0)
    saved_auth_sid = session.get("auth_sid")

    # Cheap single-row reload, matching connect()'s query style, plus one PK
    # probe of revoked_sessions when the token carried a sid.
    revoked = False
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is not None and not user.is_disabled and user.token_version == saved_tv:
            revoked = await is_sid_revoked(db, saved_auth_sid)

    if user is None or user.is_disabled or user.token_version != saved_tv or revoked:
        logger.warning(
            f"Socket re-auth failed: revoked, disabled, or unknown user (sid: {sid})"
        )
        return False
    return True


@sio.event
async def subscribe_session(sid, data):
    """
    Subscribe a client to a charging session's real-time telemetry.
    Expected data: { "session_id": 123 }
    """
    # Re-authorize against current account state (connect() only checked once).
    # A revoked/disabled account gets no further action and is booted from all
    # rooms — which also cuts off its private notification pushes.
    if not await _socket_user_still_valid(sid):
        await sio.disconnect(sid)
        return

    if not isinstance(data, dict) or "session_id" not in data:
        await sio.emit("subscription_error", {"detail": "Invalid parameters"}, to=sid)
        return

    try:
        session_id = int(data["session_id"])
    except (ValueError, TypeError):
        await sio.emit("subscription_error", {"detail": "Invalid session_id"}, to=sid)
        return

    # Retrieve user_id from session
    socket_session = await sio.get_session(sid)
    user_id = socket_session.get("user_id")
    if not user_id:
        await sio.emit("subscription_error", {"detail": "Unauthenticated"}, to=sid)
        return
    # [F10] Subscribing starts a telemetry stream that raises the plug's
    # report rate over MQTT (a device side effect): never for a read-only
    # viewer, whatever session id it names.
    if socket_session.get("read_only"):
        await sio.emit("subscription_error", {"detail": "read_only_demo"}, to=sid)
        return

    # Verify session and ownership
    async with async_session_factory() as db:
        result = await db.execute(
            select(ChargingSession).where(
                and_(
                    ChargingSession.id == session_id,
                    ChargingSession.user_id == user_id,
                )
            )
        )
        charging_session = result.scalar_one_or_none()
        if not charging_session:
            await sio.emit("subscription_error", {"detail": "Session not found or unauthorized"}, to=sid)
            return

        plug_id = charging_session.plug_id

        # [C15] A finished session gets one terminal frame built from its OWN
        # row and no stream: the store is keyed by plug, so streaming it would
        # show whoever charges on that plug now (and pin it to the 1 s rate).
        if charging_session.status != SessionStatus.ACTIVE:
            done = TelemetrySnapshot(
                plug_id=plug_id,
                energy_kwh=charging_session.energy_kwh or 0.0,
                cost_coins=float(charging_session.coins_spent or 0),
                status="completed",
            )
            await sio.emit("telemetry", done.to_dict(), to=sid)
            return

    # Enter room for this session
    room_name = f"session_{session_id}"
    await sio.enter_room(sid, room_name)
    logger.info(f"Client {sid} (user {user_id}) joined room {room_name}")

    await sio.emit("subscription_success", {"session_id": session_id}, to=sid)

    # Start telemetry stream task if not already running for this session
    if session_id not in active_streams or active_streams[session_id].done():
        task = asyncio.create_task(stream_telemetry_task(session_id, plug_id))
        active_streams[session_id] = task

@sio.event
async def unsubscribe_session(sid, data):
    """
    Explicitly unsubscribe from session telemetry.
    Expected data: { "session_id": 123 }
    """
    # Re-authorize against current account state (see subscribe_session).
    if not await _socket_user_still_valid(sid):
        await sio.disconnect(sid)
        return

    if not isinstance(data, dict) or "session_id" not in data:
        return
    try:
        session_id = int(data["session_id"])
    except (ValueError, TypeError):
        return

    room_name = f"session_{session_id}"
    await sio.leave_room(sid, room_name)
    logger.info(f"Client {sid} left room {room_name}")

async def stream_telemetry_task(session_id: int, plug_id: int):
    """
    Background task to stream telemetry for a session.
    It runs as long as the room has participants and the session is not completed.
    """
    # Late import so unit tests can stub the module with a DB-free fake
    from backend.services.session_lifecycle import set_plug_telemetry_interval
    room_name = f"session_{session_id}"
    logger.info(f"Starting telemetry stream task for session {session_id} (plug {plug_id})")

    listeners_incremented = False

    try:
        # Increment active listeners in telemetry store
        async with async_session_factory() as db:
            telemetry_store.increment_listeners(plug_id)
            listeners_incremented = True
            await set_plug_telemetry_interval(db, plug_id, 1000)

        async for snapshot in telemetry_store.stream(plug_id):
            # Check if there are participants left in the room via the room
            # manager's registry (a plain dict — safe when the namespace or
            # room no longer exists). NOTE: there is no awaitable
            # sio.get_participants(room=...) API — the previous code called
            # one, raised TypeError on every iteration, and killed each
            # stream before the first emit.
            has_listeners = bool(sio.manager.rooms.get("/", {}).get(room_name))

            if not has_listeners:
                logger.info(f"No participants left in room {room_name}. Terminating telemetry stream task.")
                break

            # Emit telemetry to the room
            await sio.emit("telemetry", snapshot, room=room_name)

            if snapshot.get("status") == "completed":
                logger.info(f"Session {session_id} completed. Terminating telemetry stream task.")
                break
    except Exception as e:
        logger.error(f"Error in stream_telemetry_task for session {session_id}: {e}", exc_info=True)
    finally:
        # Free the slot first so a re-subscribe starts a fresh stream task.
        active_streams.pop(session_id, None)
        if listeners_incremented:
            release = asyncio.create_task(release_listener_later(plug_id))
            _release_tasks.add(release)
            release.add_done_callback(_release_tasks.discard)

        logger.info(f"Telemetry stream task for session {session_id} ended.")
