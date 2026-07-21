"""
Tests for the per-user concurrent-session cap and the multi-session
/api/sessions/active response.

A user may run up to MAX_ACTIVE_SESSIONS_PER_USER (default 2) sessions at
once. /api/sessions/start enforces the cap under a user-row lock (two
simultaneous starts by the same user serialize, so the loser counts the
winner's committed session), and /api/sessions/active returns every ACTIVE
session — previously only the newest was surfaced, which left any older
active session unreachable/un-stoppable from the UI.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.routers import sessions as sessions_module
from backend.routers.sessions import get_active_session, start_charging_session
from backend.schemas import SessionStartRequest


def _user(user_id=1):
    u = MagicMock()
    u.id = user_id
    u.email = "driver@example.com"
    u.coin_balance = 100
    return u


def _scalar_one(value):
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()
    return db


# --- /api/sessions/start cap -----------------------------------------------

@pytest.mark.asyncio
async def test_start_rejected_at_cap():
    user = _user()
    db = _db(
        _scalar_one(user),  # user row lock
        _scalar_one(2),     # ACTIVE-session count == cap
    )

    with pytest.raises(HTTPException) as exc_info:
        await start_charging_session(SessionStartRequest(plug_id=1), user, db)

    assert exc_info.value.status_code == 409
    assert "2 active charging sessions" in exc_info.value.detail
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_start_allowed_below_cap():
    """One active session must not block a second start: the request gets
    past the cap check (and then 404s on the mocked missing plug)."""
    user = _user()
    db = _db(
        _scalar_one(user),          # user row lock
        _scalar_one(1),             # one ACTIVE session — below the cap of 2
        _scalar_one_or_none(None),  # plug lookup finds nothing
    )

    with pytest.raises(HTTPException) as exc_info:
        await start_charging_session(SessionStartRequest(plug_id=999), user, db)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_cap_is_configurable(monkeypatch):
    monkeypatch.setattr(sessions_module, "MAX_ACTIVE_SESSIONS_PER_USER", 1)
    user = _user()
    db = _db(
        _scalar_one(user),
        _scalar_one(1),
    )

    with pytest.raises(HTTPException) as exc_info:
        await start_charging_session(SessionStartRequest(plug_id=1), user, db)

    assert exc_info.value.status_code == 409
    assert "(limit 1)" in exc_info.value.detail


# --- /api/sessions/active multi-session shape -------------------------------

def _session_row(session_id, plug_id, started_at):
    s = MagicMock()
    s.id = session_id
    s.plug_id = plug_id
    s.started_at = started_at
    return s


@pytest.mark.asyncio
async def test_active_returns_all_sessions_newest_first():
    newest = _session_row(2, 20, datetime(2026, 7, 7, 12, 30, tzinfo=timezone.utc))
    older = _session_row(1, 10, datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc))
    result = MagicMock()
    result.all.return_value = [(newest, "Plug B"), (older, "Plug A")]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    res = await get_active_session(_user(), db)

    assert res["active"] is True
    assert [s["session_id"] for s in res["sessions"]] == [2, 1]
    assert res["sessions"][1]["plug_name"] == "Plug A"
    # Top-level fields keep the legacy single-session shape (newest session).
    assert res["session_id"] == 2
    assert res["plug_id"] == 20
    assert res["plug_name"] == "Plug B"


@pytest.mark.asyncio
async def test_active_with_no_sessions():
    result = MagicMock()
    result.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    res = await get_active_session(_user(), db)

    assert res == {"active": False, "sessions": []}
