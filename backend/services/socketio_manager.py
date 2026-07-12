import logging
import asyncio
from typing import Dict, Any, Optional
import socketio

from backend.services.auth import decode_access_token
from backend.services.telemetry import TelemetryStore
from backend.database.db import async_session_factory
from backend.database.models import ChargingSession
from sqlalchemy import select, and_

logger = logging.getLogger("amphive.socketio")

# Create the Socket.io server.
# cors_allowed_origins mirrors the FastAPI CORS allowlist in main.py (no
# wildcard). Keep the two lists in sync when the frontend origin changes.
_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://amphive.duckdns.org",
    "https://amphive.duckdns.org",
    "http://8.231.81.12",
    "https://8.231.81.12",
]
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins=_ALLOWED_ORIGINS)

# Active background telemetry tasks by session_id
active_streams: Dict[int, asyncio.Task] = {}

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


async def emit_notification(user_id: int, notification: Dict[str, Any]) -> None:
    """
    Deliver a driver notification to that user's connected clients only
    (their per-user room, joined on connect). Unlike plug_status /
    gateway_alarm this is NOT a broadcast — a notification carries
    wallet/session details that belong to one user.
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
        # Save user info in connection session
        await sio.save_session(sid, {"user_id": user_id})
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

@sio.event
async def subscribe_session(sid, data):
    """
    Subscribe a client to a charging session's real-time telemetry.
    Expected data: { "session_id": 123 }
    """
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
        if listeners_incremented:
            async with async_session_factory() as db:
                listeners = telemetry_store.decrement_listeners(plug_id)
                if listeners == 0:
                    await set_plug_telemetry_interval(db, plug_id, 10000)
        
        active_streams.pop(session_id, None)
        logger.info(f"Telemetry stream task for session {session_id} ended.")
