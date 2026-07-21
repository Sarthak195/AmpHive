"""
Tests for SessionReaperService (backend/services/session_reaper.py).

The reaper auto-finalizes ACTIVE sessions with stale telemetry via the same
finalize path as /api/sessions/stop. DB-free: the session factory and the
injected finalize callable are mocked; what's under test is the sweep logic —
every stale id gets a finalize attempt, races (finalize -> None) aren't
counted as reaped, and one failing session doesn't abort the sweep.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.session_reaper import REAP_REASON, SessionReaperService


def _factory_yielding(db):
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _db_with_stale_ids(ids):
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(ids)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_reaps_every_stale_session_with_reason():
    db = _db_with_stale_ids([11, 12])
    finalize = AsyncMock(side_effect=[{"energy_kwh": 0.5, "coins_spent": 2.5},
                                      {"energy_kwh": 0.0, "coins_spent": 0.0}])
    svc = SessionReaperService(_factory_yielding(db), finalize)

    assert await svc.reap_once() == 2

    assert [c.args[1] for c in finalize.await_args_list] == [11, 12]
    assert all(c.kwargs == {"reason": REAP_REASON} for c in finalize.await_args_list)


@pytest.mark.asyncio
async def test_session_stopped_by_user_mid_sweep_is_not_counted():
    """finalize returns None when the row-lock re-check finds the session no
    longer ACTIVE (user stop won the race) — reaped count must exclude it."""
    db = _db_with_stale_ids([21, 22])
    finalize = AsyncMock(side_effect=[None, {"energy_kwh": 0.1, "coins_spent": 0.5}])
    svc = SessionReaperService(_factory_yielding(db), finalize)

    assert await svc.reap_once() == 1


@pytest.mark.asyncio
async def test_one_failing_session_does_not_abort_the_sweep():
    db = _db_with_stale_ids([31, 32])
    finalize = AsyncMock(side_effect=[RuntimeError("db hiccup"),
                                      {"energy_kwh": 0.2, "coins_spent": 1.0}])
    svc = SessionReaperService(_factory_yielding(db), finalize)

    assert await svc.reap_once() == 1
    assert finalize.await_count == 2


@pytest.mark.asyncio
async def test_no_stale_sessions_is_a_no_op():
    db = _db_with_stale_ids([])
    finalize = AsyncMock()
    svc = SessionReaperService(_factory_yielding(db), finalize)

    assert await svc.reap_once() == 0
    finalize.assert_not_awaited()


def test_staleness_query_uses_coalesce_over_started_at():
    """A session that never produced telemetry must be judged from started_at:
    the query must COALESCE(last_telemetry_at, started_at) and filter ACTIVE."""
    svc = SessionReaperService(MagicMock(), AsyncMock())
    sql = str(svc._stale_session_ids_query(datetime.now(timezone.utc))).lower()
    assert "coalesce" in sql
    assert "last_telemetry_at" in sql
    assert "started_at" in sql
    assert "status" in sql
