"""
Tests for SessionReaperService (backend/services/session_reaper.py).

The reaper auto-finalizes ACTIVE sessions with stale telemetry via the same
finalize path as /api/sessions/stop. DB-free: the session factory and the
injected finalize callable are mocked; what's under test is the sweep logic —
every stale id gets a finalize attempt, races (finalize -> None) aren't
counted as reaped, and one failing session doesn't abort the sweep.

Also covers the gateway-silence connectivity sweep (faster-gateway-offline-
detection, lever 1 backstop) and the gateway-logs retention prune (TD#28).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.database.models import GatewayStatus
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


# --------------------------------------------------------------------------
# Gateway silence sweep (faster-gateway-offline-detection, lever 1 backstop)
# --------------------------------------------------------------------------

def _db_seq(results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


def _online_gateway(gateway_id="gw-1", stale=True):
    gw = MagicMock()
    gw.id = gateway_id
    gw.status = GatewayStatus.ONLINE
    gw.last_seen_at = (
        datetime.now(timezone.utc) - timedelta(seconds=999)
        if stale
        else datetime.now(timezone.utc)
    )
    return gw


@pytest.mark.asyncio
async def test_gateway_silence_sweep_flags_once_not_twice_then_recovers():
    """A gateway the DB still flags ONLINE but that has gone non-live is
    flagged with one plug_connectivity(False) push per plug, exactly once —
    a second stale tick (still non-live, already flagged) makes no further
    plug-id query and emits nothing. Once it's live again, one
    plug_connectivity(True) push per plug fires and the dedup set clears."""
    gw = _online_gateway(stale=True)

    gateways_result = MagicMock()
    gateways_result.scalars.return_value.all.return_value = [gw]
    plugs_result = MagicMock()
    plugs_result.scalars.return_value.all.return_value = [101, 102]

    svc = SessionReaperService(
        _factory_yielding(_db_seq([gateways_result, plugs_result])), AsyncMock()
    )

    with patch("backend.services.socketio_manager.emit_plug_connectivity", AsyncMock()) as emit_mock:
        pushed = await svc.reap_gateway_silence_once()

        assert pushed == 1
        assert [c.args for c in emit_mock.await_args_list] == [(101, False), (102, False)]
        assert "gw-1" in svc._silence_pushed

        # Second tick: still stale, already flagged -> gateways query only,
        # no plug-id query, no re-emit.
        emit_mock.reset_mock()
        svc.db_session_factory = _factory_yielding(_db_seq([gateways_result]))
        pushed_again = await svc.reap_gateway_silence_once()

        assert pushed_again == 0
        emit_mock.assert_not_awaited()
        assert "gw-1" in svc._silence_pushed

        # Recovery: live again -> True per plug, dedup set cleared.
        gw.last_seen_at = datetime.now(timezone.utc)
        svc.db_session_factory = _factory_yielding(_db_seq([gateways_result, plugs_result]))
        pushed_recovery = await svc.reap_gateway_silence_once()

        assert pushed_recovery == 1
        assert [c.args for c in emit_mock.await_args_list] == [(101, True), (102, True)]
        assert "gw-1" not in svc._silence_pushed


@pytest.mark.asyncio
async def test_gateway_silence_sweep_live_gateway_is_a_no_op():
    """A gateway that is ONLINE and live (fresh last_seen_at) and was never
    flagged makes no plug-id query and emits nothing."""
    gw = _online_gateway(stale=False)
    gateways_result = MagicMock()
    gateways_result.scalars.return_value.all.return_value = [gw]

    svc = SessionReaperService(_factory_yielding(_db_seq([gateways_result])), AsyncMock())

    with patch("backend.services.socketio_manager.emit_plug_connectivity", AsyncMock()) as emit_mock:
        pushed = await svc.reap_gateway_silence_once()

    assert pushed == 0
    emit_mock.assert_not_awaited()
    assert svc._silence_pushed == set()


# --------------------------------------------------------------------------
# Gateway-logs retention prune (TD#28)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reap_gateway_logs_once_deletes_and_commits():
    result = MagicMock()
    result.rowcount = 7
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    svc = SessionReaperService(_factory_yielding(db), AsyncMock())

    deleted = await svc.reap_gateway_logs_once()

    assert deleted == 7
    db.commit.assert_awaited_once()
    sql = str(db.execute.await_args.args[0]).lower()
    assert "gateway_logs" in sql
    assert "created_at" in sql


@pytest.mark.asyncio
async def test_reap_gateway_logs_once_swallows_db_errors():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    svc = SessionReaperService(_factory_yielding(db), AsyncMock())

    assert await svc.reap_gateway_logs_once() == 0
