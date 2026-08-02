import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.socketio_manager import connect, subscribe_session


def _configure_user_db(mock_db_factory, user):
    """Wire a patched async_session_factory so `async with` yields a session
    whose User query returns `user` (pass None to simulate a deleted account)."""
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result
    mock_db_factory.return_value.__aenter__.return_value = mock_db


@pytest.mark.asyncio
async def test_connect_valid_token():
    with patch("backend.services.socketio_manager.decode_access_token") as mock_decode, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_db_factory, \
         patch("backend.services.socketio_manager.sio") as mock_sio:

        # tv matches the user's current token_version (0), so the token is live.
        mock_decode.return_value = {"sub": "42", "tv": 0}
        _configure_user_db(mock_db_factory, MagicMock(token_version=0))
        mock_sio.save_session = AsyncMock()
        mock_sio.enter_room = AsyncMock()

        # Token in the auth payload (CONNECT packet body) — the only accepted path
        res = await connect("sid-123", {}, {"token": "valid-jwt"})
        assert res is True
        # The saved session now also carries the token's tv claim, so
        # authenticated event handlers can re-validate the account later
        # without re-decoding the JWT.
        mock_sio.save_session.assert_awaited_once_with("sid-123", {"user_id": 42, "tv": 0})
        # Joined the per-user room (driver notification delivery target)
        mock_sio.enter_room.assert_awaited_once_with("sid-123", "user_42")

        # Token in the query string is NOT accepted (removed 2026-07-09: query
        # strings land in proxy/access logs, turning them into bearer creds)
        mock_sio.save_session.reset_mock()
        res = await connect("sid-123", {"QUERY_STRING": "token=query-jwt"}, None)
        assert res is False
        mock_sio.save_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_rejects_revoked_or_unknown_user():
    """A token whose tv predates the user's token_version (logout / password
    reset / admin revoke), or that names a deleted user, must not connect."""
    with patch("backend.services.socketio_manager.decode_access_token") as mock_decode, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_db_factory, \
         patch("backend.services.socketio_manager.sio") as mock_sio:

        mock_sio.save_session = AsyncMock()

        # Revoked: token minted at tv=1, user's token_version has since bumped to 2.
        mock_decode.return_value = {"sub": "42", "tv": 1}
        _configure_user_db(mock_db_factory, MagicMock(token_version=2))
        assert await connect("sid-r", {}, {"token": "revoked-jwt"}) is False

        # Unknown/deleted user: the User row is gone.
        mock_decode.return_value = {"sub": "99", "tv": 0}
        _configure_user_db(mock_db_factory, None)
        assert await connect("sid-u", {}, {"token": "orphan-jwt"}) is False

        mock_sio.save_session.assert_not_awaited()

@pytest.mark.asyncio
async def test_connect_invalid_or_missing_token():
    with patch("backend.services.socketio_manager.decode_access_token") as mock_decode, \
         patch("backend.services.socketio_manager.sio"):

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

        mock_sio.get_session = AsyncMock(return_value={"user_id": 42, "tv": 0})
        mock_sio.emit = AsyncMock()
        mock_sio.disconnect = AsyncMock()

        # Two DB reads happen in order: the re-auth helper's User query (a live,
        # matching, non-disabled user), then the handler's ownership query
        # (None → session not found or unauthorized).
        live_user = MagicMock(token_version=0, is_disabled=False)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.side_effect = [live_user, None]
        mock_db.execute.return_value = mock_result
        mock_db_factory.return_value.__aenter__.return_value = mock_db

        await subscribe_session("sid-123", {"session_id": 100})

        mock_sio.emit.assert_awaited_once_with(
            "subscription_error", {"detail": "Session not found or unauthorized"}, to="sid-123"
        )
        # Re-auth passed, so the socket is not force-disconnected.
        mock_sio.disconnect.assert_not_awaited()

@pytest.mark.asyncio
async def test_subscribe_session_success():
    with patch("backend.services.socketio_manager.sio") as mock_sio, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_db_factory, \
         patch("backend.services.socketio_manager.stream_telemetry_task"), \
         patch("backend.services.socketio_manager.active_streams", {}) as mock_active_streams:

        mock_sio.get_session = AsyncMock(return_value={"user_id": 42, "tv": 0})
        mock_sio.emit = AsyncMock()
        mock_sio.enter_room = AsyncMock()
        mock_sio.disconnect = AsyncMock()

        # Two DB reads happen in order: the re-auth helper's User query (a live,
        # matching, non-disabled user), then the handler's ownership query
        # (a valid ChargingSession).
        live_user = MagicMock(token_version=0, is_disabled=False)
        mock_session = MagicMock()
        mock_session.plug_id = 9

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.side_effect = [live_user, mock_session]
        mock_db.execute.return_value = mock_result
        mock_db_factory.return_value.__aenter__.return_value = mock_db

        await subscribe_session("sid-123", {"session_id": 100})

        mock_sio.enter_room.assert_awaited_once_with("sid-123", "session_100")
        mock_sio.emit.assert_awaited_once_with("subscription_success", {"session_id": 100}, to="sid-123")
        assert 100 in mock_active_streams
        # Valid, unchanged user → not force-disconnected (regression guard).
        mock_sio.disconnect.assert_not_awaited()


def _configure_reauth_db(mock_db_factory, user):
    """Wire the mocked async_session_factory so the re-auth helper's User query
    (the first DB read in an authenticated handler) yields `user`."""
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result
    mock_db_factory.return_value.__aenter__.return_value = mock_db


@pytest.mark.asyncio
async def test_subscribe_session_reauth_rejects_revoked_tv():
    """A still-open socket whose account has since bumped token_version (logout
    everywhere / password reset / demote) must be refused AND force-disconnected
    on its next event — parity with the HTTP token_version re-check."""
    with patch("backend.services.socketio_manager.sio") as mock_sio, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_db_factory:

        # Socket saved tv=0 at connect; the DB user has since moved to tv=1.
        mock_sio.get_session = AsyncMock(return_value={"user_id": 42, "tv": 0})
        mock_sio.emit = AsyncMock()
        mock_sio.enter_room = AsyncMock()
        mock_sio.disconnect = AsyncMock()
        _configure_reauth_db(mock_db_factory, MagicMock(token_version=1, is_disabled=False))

        await subscribe_session("sid-123", {"session_id": 100})

        # Action refused: no room join, no success emit.
        mock_sio.enter_room.assert_not_awaited()
        mock_sio.emit.assert_not_awaited()
        # Socket booted from all rooms (also cuts off future private pushes).
        mock_sio.disconnect.assert_awaited_once_with("sid-123")


@pytest.mark.asyncio
async def test_subscribe_session_reauth_rejects_disabled_user():
    """A still-open socket whose account has since been disabled (is_disabled)
    must be refused AND force-disconnected, even if token_version still matches."""
    with patch("backend.services.socketio_manager.sio") as mock_sio, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_db_factory:

        mock_sio.get_session = AsyncMock(return_value={"user_id": 42, "tv": 0})
        mock_sio.emit = AsyncMock()
        mock_sio.enter_room = AsyncMock()
        mock_sio.disconnect = AsyncMock()
        _configure_reauth_db(mock_db_factory, MagicMock(token_version=0, is_disabled=True))

        await subscribe_session("sid-123", {"session_id": 100})

        mock_sio.enter_room.assert_not_awaited()
        mock_sio.emit.assert_not_awaited()
        mock_sio.disconnect.assert_awaited_once_with("sid-123")


@pytest.mark.asyncio
async def test_subscribe_session_reauth_allows_unchanged_user():
    """A valid, unchanged account (tv matches, not disabled) passes re-auth and
    is never force-disconnected."""
    with patch("backend.services.socketio_manager.sio") as mock_sio, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_db_factory, \
         patch("backend.services.socketio_manager.stream_telemetry_task"), \
         patch("backend.services.socketio_manager.active_streams", {}):

        mock_sio.get_session = AsyncMock(return_value={"user_id": 42, "tv": 0})
        mock_sio.emit = AsyncMock()
        mock_sio.enter_room = AsyncMock()
        mock_sio.disconnect = AsyncMock()

        live_user = MagicMock(token_version=0, is_disabled=False)
        mock_session = MagicMock()
        mock_session.plug_id = 9
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.side_effect = [live_user, mock_session]
        mock_db.execute.return_value = mock_result
        mock_db_factory.return_value.__aenter__.return_value = mock_db

        await subscribe_session("sid-123", {"session_id": 100})

        mock_sio.disconnect.assert_not_awaited()
        mock_sio.emit.assert_awaited_once_with("subscription_success", {"session_id": 100}, to="sid-123")


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
