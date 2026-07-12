import asyncio
import sys
import types

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.services.socketio_manager import connect, disconnect, subscribe_session, unsubscribe_session

@pytest.mark.asyncio
async def test_connect_valid_token():
    with patch("backend.services.socketio_manager.decode_access_token") as mock_decode, \
         patch("backend.services.socketio_manager.sio") as mock_sio:
        
        mock_decode.return_value = {"sub": "42"}
        mock_sio.save_session = AsyncMock()
        mock_sio.enter_room = AsyncMock()

        # Token in the auth payload (CONNECT packet body) — the only accepted path
        res = await connect("sid-123", {}, {"token": "valid-jwt"})
        assert res is True
        mock_sio.save_session.assert_awaited_once_with("sid-123", {"user_id": 42})
        # Joined the per-user room (driver notification delivery target)
        mock_sio.enter_room.assert_awaited_once_with("sid-123", "user_42")

        # Token in the query string is NOT accepted (removed 2026-07-09: query
        # strings land in proxy/access logs, turning them into bearer creds)
        mock_sio.save_session.reset_mock()
        res = await connect("sid-123", {"QUERY_STRING": "token=query-jwt"}, None)
        assert res is False
        mock_sio.save_session.assert_not_awaited()

@pytest.mark.asyncio
async def test_connect_invalid_or_missing_token():
    with patch("backend.services.socketio_manager.decode_access_token") as mock_decode, \
         patch("backend.services.socketio_manager.sio") as mock_sio:
        
        mock_decode.return_value = None
        
        # No token at all
        res = await connect("sid-123", {}, None)
        assert res is False
        
        # Invalid token
        res = await connect("sid-123", {}, {"token": "invalid-jwt"})
        assert res is False

@pytest.mark.asyncio
async def test_subscribe_session_unauthorized():
    with patch("backend.services.socketio_manager.sio") as mock_sio, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_db_factory:
        
        mock_sio.get_session = AsyncMock(return_value={"user_id": 42})
        mock_sio.emit = AsyncMock()
        
        # Setup mock db session returning None (no session found or unauthorized)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db_factory.return_value.__aenter__.return_value = mock_db
        
        await subscribe_session("sid-123", {"session_id": 100})
        
        mock_sio.emit.assert_awaited_once_with(
            "subscription_error", {"detail": "Session not found or unauthorized"}, to="sid-123"
        )

@pytest.mark.asyncio
async def test_subscribe_session_success():
    with patch("backend.services.socketio_manager.sio") as mock_sio, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_db_factory, \
         patch("backend.services.socketio_manager.stream_telemetry_task") as mock_task, \
         patch("backend.services.socketio_manager.active_streams", {}) as mock_active_streams:
        
        mock_sio.get_session = AsyncMock(return_value={"user_id": 42})
        mock_sio.emit = AsyncMock()
        mock_sio.enter_room = AsyncMock()
        
        # Setup mock db session returning a valid session
        mock_session = MagicMock()
        mock_session.plug_id = 9
        
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_session
        mock_db.execute.return_value = mock_result
        mock_db_factory.return_value.__aenter__.return_value = mock_db
        
        await subscribe_session("sid-123", {"session_id": 100})
        
        mock_sio.enter_room.assert_awaited_once_with("sid-123", "session_100")
        mock_sio.emit.assert_awaited_once_with("subscription_success", {"session_id": 100}, to="sid-123")
        assert 100 in mock_active_streams


@pytest.mark.asyncio
async def test_stream_task_emits_telemetry_and_stops_when_room_empties(monkeypatch):
    """
    Regression test for the get_participants bug: the task previously called
    a non-existent `await sio.get_participants(room=...)` API, hit TypeError
    on every loop iteration, and terminated before emitting a single
    telemetry event. This runs the REAL task against the real sio instance
    (only emit + the backend.main late-import are stubbed) and asserts that
    telemetry is actually emitted while the room has a participant, and that
    the task exits once the room empties.
    """
    from backend.services import socketio_manager as sm

    # Stub the session-lifecycle module so the task's late import gets a
    # DB-free set_plug_telemetry_interval.
    fake_lifecycle = types.ModuleType("backend.services.session_lifecycle")
    fake_lifecycle.set_plug_telemetry_interval = AsyncMock()
    monkeypatch.setitem(sys.modules, "backend.services.session_lifecycle", fake_lifecycle)

    session_id, plug_id = 4242, 7777
    room_name = f"session_{session_id}"

    # Seed live telemetry for the plug.
    sm.telemetry_store.start_session(plug_id)
    sm.telemetry_store.update(plug_id=plug_id, power_w=42.0, current_a=0.2, energy_kwh=0.01)

    # One participant in the room, registered in the real manager structure.
    monkeypatch.setitem(sm.sio.manager.rooms, "/", {room_name: {"sid-1": "eio-1"}})

    emitted = []

    async def fake_emit(event, data=None, room=None, to=None, **kwargs):
        emitted.append((event, data, room))

    monkeypatch.setattr(sm.sio, "emit", fake_emit)

    task = asyncio.create_task(sm.stream_telemetry_task(session_id, plug_id))
    try:
        # Wait for the first telemetry emit (the old bug produced none, ever).
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.05)
        assert emitted, "stream task emitted no telemetry while the room had a participant"
        event, data, room = emitted[0]
        assert event == "telemetry"
        assert room == room_name
        assert data["power_w"] == 42.0

        # Empty the room — the task must notice and terminate on its own.
        sm.sio.manager.rooms["/"].pop(room_name)
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
        sm.telemetry_store.end_session(plug_id)

    assert session_id not in sm.active_streams
