"""
Tests for the operator maintenance workflow (fault console):
POST /api/cpo/plugs/{id}/maintenance.

Distinct from the general PUT /api/cpo/plugs/{id} status setter (covered by
test_audit_log.py's plug.status_change case) — this is the dedicated
enter/clear action the fault console drives: `clear` is refused (409) while
the plug has an ACTIVE session, and both actions audit under their own
action names (plug.maintenance_enter / plug.maintenance_clear).

DB-free: uses the mocked-AsyncSession pattern from test_gateway_ota.py /
test_audit_log.py.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.database.models import PlugStatus
from backend.routers.cpo import cpo_plug_maintenance
from backend.schemas import CpoPlugMaintenanceRequest


def _user(tenant_id=1, user_id=42, email="cpo@amphive.test"):
    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.email = email
    return u


def _plug(plug_id=7, status=PlugStatus.AVAILABLE, name="Bay 1"):
    p = MagicMock()
    p.id = plug_id
    p.name = name
    p.status = status
    return p


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _scalar_one(value):
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()          # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_maintenance_enter_sets_status_and_audits():
    user = _user(tenant_id=1, user_id=42)
    plug = _plug(plug_id=7, status=PlugStatus.AVAILABLE)
    db = _db(_scalar_one_or_none(plug))  # plug ownership lookup only — enter has no session guard

    emit_mock = AsyncMock()
    with patch("backend.services.socketio_manager.emit_plug_status", emit_mock):
        res = await cpo_plug_maintenance(
            7, CpoPlugMaintenanceRequest(action="enter"), user, db,
        )

    assert plug.status == PlugStatus.MAINTENANCE
    assert res == {
        "status": "updated", "plug_id": 7, "action": "enter",
        "plug_status": "maintenance",
    }
    assert db.commit.await_count == 2  # plug update + audit write
    added = db.add.call_args[0][0]
    assert added.tenant_id == 1
    assert added.actor_user_id == 42
    assert added.action == "plug.maintenance_enter"
    assert added.target_type == "plug"
    assert added.target_id == "7"
    assert added.detail == "available -> maintenance"
    emit_mock.assert_awaited_once_with(7, "maintenance")


@pytest.mark.asyncio
async def test_maintenance_enter_note_included_in_audit_detail():
    user = _user()
    plug = _plug(plug_id=7, status=PlugStatus.AVAILABLE)
    db = _db(_scalar_one_or_none(plug))

    with patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()):
        await cpo_plug_maintenance(
            7, CpoPlugMaintenanceRequest(action="enter", note="breaker tripped"), user, db,
        )

    added = db.add.call_args[0][0]
    assert "note=breaker tripped" in added.detail


@pytest.mark.asyncio
async def test_maintenance_clear_sets_available_when_no_active_session():
    user = _user(tenant_id=1, user_id=42)
    plug = _plug(plug_id=7, status=PlugStatus.MAINTENANCE)
    db = _db(
        _scalar_one_or_none(plug),  # plug ownership lookup
        _scalar_one(0),             # no ACTIVE sessions on this plug
    )

    emit_mock = AsyncMock()
    with patch("backend.services.socketio_manager.emit_plug_status", emit_mock):
        res = await cpo_plug_maintenance(
            7, CpoPlugMaintenanceRequest(action="clear"), user, db,
        )

    assert plug.status == PlugStatus.AVAILABLE
    assert res["plug_status"] == "available"
    added = db.add.call_args[0][0]
    assert added.action == "plug.maintenance_clear"
    assert added.detail == "maintenance -> available"
    emit_mock.assert_awaited_once_with(7, "available")


@pytest.mark.asyncio
async def test_maintenance_clear_blocked_while_session_active():
    """The representative case (fault-console acceptance criteria): clear is
    refused 409 while the plug has an ACTIVE session — the plug is left
    untouched and nothing is committed or audited."""
    user = _user()
    plug = _plug(plug_id=7, status=PlugStatus.MAINTENANCE)
    db = _db(
        _scalar_one_or_none(plug),  # plug ownership lookup
        _scalar_one(1),             # 1 ACTIVE session on this plug
    )

    with pytest.raises(HTTPException) as exc:
        await cpo_plug_maintenance(7, CpoPlugMaintenanceRequest(action="clear"), user, db)

    assert exc.value.status_code == 409
    assert "active charging session" in exc.value.detail.lower()
    assert plug.status == PlugStatus.MAINTENANCE  # unchanged
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_maintenance_404_when_plug_not_owned():
    user = _user()
    db = _db(_scalar_one_or_none(None))  # plug lookup finds nothing (wrong tenant / missing)

    with pytest.raises(HTTPException) as exc:
        await cpo_plug_maintenance(999, CpoPlugMaintenanceRequest(action="enter"), user, db)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_maintenance_400_on_invalid_action():
    """Validated before any DB access — an invalid action never touches the
    plug lookup."""
    user = _user()
    db = _db()  # no queued results: execute must not be called

    with pytest.raises(HTTPException) as exc:
        await cpo_plug_maintenance(7, CpoPlugMaintenanceRequest(action="explode"), user, db)

    assert exc.value.status_code == 400
    db.execute.assert_not_called()
