"""
Regression tests for check_and_speed_up_active_session (backend/main.py).

The helper runs on every /api/auth/login and /api/auth/me. It used to fetch
the user's ACTIVE session with `scalar_one_or_none()`, which raises
MultipleResultsFound when the user holds more than one ACTIVE session
(possible — nothing limits a user to a single active session, e.g. a stale
session on an offline gateway alongside a live one). That crashed login and
session restore for the affected user (seen in production 2026-07-06 with
three concurrent ACTIVE sessions).

The mock result mirrors SQLAlchemy semantics: `scalars().all()` returns every
row, while `scalar_one_or_none()` raises on more than one — so the old code
path fails these tests and the fixed one passes.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import MultipleResultsFound

from backend.services.session_lifecycle import check_and_speed_up_active_session


def _session(plug_id):
    s = MagicMock()
    s.plug_id = plug_id
    return s


def _db_returning_sessions(sessions):
    result = MagicMock()
    result.scalars.return_value.all.return_value = sessions
    if len(sessions) > 1:
        result.scalar_one_or_none.side_effect = MultipleResultsFound(
            "Multiple rows were found when one or none was required"
        )
    else:
        result.scalar_one_or_none.return_value = sessions[0] if sessions else None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_multiple_active_sessions_do_not_crash_and_each_plug_speeds_up():
    db = _db_returning_sessions([_session(1), _session(3), _session(4)])
    with patch("backend.services.session_lifecycle.set_plug_telemetry_interval", new=AsyncMock()) as speed_up:
        await check_and_speed_up_active_session(db, user_id=4)

    assert sorted(call.args[1] for call in speed_up.await_args_list) == [1, 3, 4]
    assert all(call.args[2] == 1000 for call in speed_up.await_args_list)


@pytest.mark.asyncio
async def test_single_active_session_speeds_up_its_plug():
    db = _db_returning_sessions([_session(7)])
    with patch("backend.services.session_lifecycle.set_plug_telemetry_interval", new=AsyncMock()) as speed_up:
        await check_and_speed_up_active_session(db, user_id=8)

    speed_up.assert_awaited_once()
    assert speed_up.await_args.args[1] == 7
    assert speed_up.await_args.args[2] == 1000


@pytest.mark.asyncio
async def test_no_active_sessions_is_a_no_op():
    db = _db_returning_sessions([])
    with patch("backend.services.session_lifecycle.set_plug_telemetry_interval", new=AsyncMock()) as speed_up:
        await check_and_speed_up_active_session(db, user_id=9)

    speed_up.assert_not_awaited()
