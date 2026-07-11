"""
Tests for the OFF republish on gateway reconnect (MQTTManager).

OFF commands aren't retained, so a gateway that was dead when its session got
finalized (session reaper, or a stop while offline) never received one — its
NVS crash recovery then resumes the session on reboot with the relay ON and
nobody billing (observed on-device 2026-07-07). When a gateway reports
"online", the backend must re-send OFF to each of its plugs that has no
ACTIVE session, and leave plugs with a live session alone.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.services.mqtt_manager import MQTTManager


def _result_scalar_one(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _result_scalars(values):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(values)
    return r


def _manager_with_db(execute_results):
    MQTTManager._instance = None
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_results)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    mgr = MQTTManager(db_session_factory=factory)
    mgr.send_plug_command = MagicMock(return_value=True)
    return mgr, db


@pytest.mark.asyncio
async def test_online_republishes_off_only_to_plugs_without_active_session():
    mgr, db = _manager_with_db([
        _result_scalar_one(MagicMock()),   # gateway row lookup
        _result_scalars([1, 2]),           # the gateway's plugs
        _result_scalars([2]),              # plug 2 has an ACTIVE session
    ])

    await mgr._persist_gateway_status("gw-1", "online")

    mgr.send_plug_command.assert_called_once_with("gw-1", 1, "OFF", wait=False)
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_offline_status_does_not_republish():
    mgr, db = _manager_with_db([
        _result_scalar_one(MagicMock()),   # gateway row lookup only
    ])

    await mgr._persist_gateway_status("gw-1", "offline")

    mgr.send_plug_command.assert_not_called()
    assert db.execute.await_count == 1  # no plug/session queries
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_gateway_with_no_plugs_is_a_no_op():
    mgr, db = _manager_with_db([
        _result_scalar_one(MagicMock()),
        _result_scalars([]),               # no plugs registered
    ])

    await mgr._persist_gateway_status("gw-1", "online")

    mgr.send_plug_command.assert_not_called()
    MQTTManager._instance = None
