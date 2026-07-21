"""
Tests for the CPO admin audit trail (TD#26): services/audit.py plus its
wiring into routers/cpo.py (gateway/plug/group create-delete, plug status
change, access-code regen) and the GET /api/cpo/audit read path.

DB-free: uses the mocked-AsyncSession pattern from test_gateway_ota.py /
test_session_start_plug_status.py.
"""
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.database.models import PlugStatus
from backend.routers.cpo import (
    cpo_create_gateway,
    cpo_create_group,
    cpo_create_plug,
    cpo_delete_group,
    cpo_list_audit_log,
    cpo_update_group,
    cpo_update_plug,
)
from backend.schemas import (
    CpoGatewayCreateRequest,
    CpoGroupCreateRequest,
    CpoGroupUpdateRequest,
    CpoPlugCreateRequest,
    CpoPlugUpdateRequest,
)
from backend.services.audit import record_audit, try_record_audit


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
    p.group_id = None
    return p


def _group(group_id=42, name="Sunrise Lot", is_public=False, access_code="OLDCODE1"):
    g = MagicMock()
    g.id = group_id
    g.name = name
    g.is_public = is_public
    g.access_code = access_code
    return g


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _scalars_all(items):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(items)
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()          # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    return db


# --- services/audit.py -------------------------------------------------


@pytest.mark.asyncio
async def test_record_audit_adds_row_without_committing():
    """record_audit only stages the row (db.add) — it must not commit; the
    caller decides when/whether the write is persisted."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    entry = await record_audit(
        db,
        tenant_id=1,
        actor_user_id=42,
        action="plug.status_change",
        target_type="plug",
        target_id=7,  # int on purpose: record_audit must stringify it
        detail="available -> offline",
    )

    db.add.assert_called_once_with(entry)
    assert entry.tenant_id == 1
    assert entry.actor_user_id == 42
    assert entry.action == "plug.status_change"
    assert entry.target_type == "plug"
    assert entry.target_id == "7"
    assert entry.detail == "available -> offline"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_record_audit_commits_on_success():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    await try_record_audit(
        db, tenant_id=1, actor_user_id=42,
        action="gateway.create", target_type="gateway", target_id="gw-1",
    )

    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_record_audit_is_non_fatal_and_logs_on_failure(caplog):
    """An audit-write failure must be caught (never raised) and logged, not
    swallowed silently — the whole point of TD#26 is an accountability trail,
    so a broken one needs to be visible in the server logs."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock(side_effect=RuntimeError("db exploded"))
    db.rollback = AsyncMock()

    with caplog.at_level(logging.ERROR, logger="amphive.api"):
        await try_record_audit(  # must not raise
            db, tenant_id=1, actor_user_id=42,
            action="gateway.create", target_type="gateway", target_id="gw-1",
        )

    db.rollback.assert_awaited_once()
    assert "Audit log write failed" in caplog.text


@pytest.mark.asyncio
async def test_try_record_audit_swallows_rollback_failure_too(caplog):
    """Regression (PR #27): even the failure-path rollback is best-effort —
    if the commit fails AND the rollback fails (e.g. the connection died),
    neither exception may propagate into the already-committed admin action.
    Both failures must still be visible at ERROR."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock(side_effect=RuntimeError("db exploded"))
    db.rollback = AsyncMock(side_effect=RuntimeError("rollback exploded"))

    with caplog.at_level(logging.ERROR, logger="amphive.api"):
        await try_record_audit(  # must not raise
            db, tenant_id=1, actor_user_id=42,
            action="payout.mark_paid", target_type="payout", target_id="1",
        )

    assert "Audit rollback failed" in caplog.text
    assert "Audit log write failed" in caplog.text


# --- routers/cpo.py wiring ----------------------------------------------


@pytest.mark.asyncio
async def test_gateway_create_writes_audit_row():
    user = _user(tenant_id=1, user_id=42)
    db = _db(_scalar_one_or_none(None))  # no existing gateway with this id

    await cpo_create_gateway(
        CpoGatewayCreateRequest(gateway_id="gw-99", name="Site Z"), user, db,
    )

    # db.add: first call is the Gateway, second is the audit log row.
    added = db.add.call_args_list[1][0][0]
    assert added.tenant_id == 1
    assert added.actor_user_id == 42
    assert added.action == "gateway.create"
    assert added.target_type == "gateway"
    assert added.target_id == "gw-99"
    assert db.commit.await_count == 2  # gateway create + audit write


@pytest.mark.asyncio
async def test_plug_create_writes_audit_row():
    user = _user(tenant_id=1, user_id=42)
    gateway = MagicMock()
    gateway.id = "gw-1"
    gateway.tenant_id = 1
    db = _db(_scalar_one_or_none(gateway))  # gateway ownership lookup

    await cpo_create_plug(
        CpoPlugCreateRequest(gateway_id="gw-1", name="Bay 1", local_ip="10.20.0.5"),
        user, db,
    )

    added = db.add.call_args_list[1][0][0]
    assert added.tenant_id == 1
    assert added.actor_user_id == 42
    assert added.action == "plug.create"
    assert added.target_type == "plug"
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_plug_status_change_writes_audit_row():
    """The representative case (TD#26 acceptance): an audit row with the
    correct tenant_id/actor/action/target for a plug status change."""
    user = _user(tenant_id=1, user_id=42)
    plug = _plug(plug_id=7, status=PlugStatus.AVAILABLE)
    db = _db(_scalar_one_or_none(plug))  # plug ownership lookup

    await cpo_update_plug(7, CpoPlugUpdateRequest(status="offline"), user, db)

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.tenant_id == 1
    assert added.actor_user_id == 42
    assert added.action == "plug.status_change"
    assert added.target_type == "plug"
    assert added.target_id == "7"
    assert added.detail == "available -> offline"
    assert db.commit.await_count == 2  # plug update + audit write


@pytest.mark.asyncio
async def test_plug_rename_without_status_change_does_not_audit():
    """A plug update that never touches `status` isn't in the audited
    taxonomy (plug.status_change specifically) — no row should be written."""
    user = _user()
    plug = _plug(plug_id=7, status=PlugStatus.AVAILABLE)
    db = _db(_scalar_one_or_none(plug))

    await cpo_update_plug(7, CpoPlugUpdateRequest(name="New Name"), user, db)

    db.add.assert_not_called()
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_group_create_writes_audit_row():
    user = _user(tenant_id=1, user_id=42)
    # is_public=True skips generate_unique_access_code (no extra db.execute)
    db = _db()

    await cpo_create_group(
        CpoGroupCreateRequest(name="Sunrise Lot", is_public=True), user, db,
    )

    added = db.add.call_args_list[1][0][0]
    assert added.tenant_id == 1
    assert added.actor_user_id == 42
    assert added.action == "group.create"
    assert added.target_type == "group"
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_group_delete_writes_audit_row():
    user = _user(tenant_id=1, user_id=42)
    group = _group(group_id=42, name="Sunrise Lot")
    db = _db(
        _scalar_one_or_none(group),  # group lookup
        _scalars_all([]),            # plugs assigned to this group
        _scalars_all([]),            # memberships of this group
    )

    await cpo_delete_group(42, user, db)

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.tenant_id == 1
    assert added.actor_user_id == 42
    assert added.action == "group.delete"
    assert added.target_type == "group"
    assert added.target_id == "42"
    assert added.detail == "name=Sunrise Lot"


@pytest.mark.asyncio
async def test_access_code_regenerate_writes_audit_row():
    user = _user(tenant_id=1, user_id=42)
    group = _group(group_id=42, name="Sunrise Lot", is_public=False, access_code="OLDCODE1")
    db = _db(
        _scalar_one_or_none(group),  # group lookup
        _scalar_one_or_none(None),   # generate_unique_access_code's uniqueness check
    )

    await cpo_update_group(42, CpoGroupUpdateRequest(regenerate_access_code=True), user, db)

    added = db.add.call_args[0][0]
    assert added.tenant_id == 1
    assert added.actor_user_id == 42
    assert added.action == "access_code.regen"
    assert added.target_type == "group"
    assert added.target_id == "42"


# --- GET /api/cpo/audit --------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_log_returns_tenant_scoped_entries():
    user = _user(tenant_id=1, user_id=42)
    entry = MagicMock()
    entry.id = 1
    entry.actor_user_id = 42
    entry.action = "plug.status_change"
    entry.target_type = "plug"
    entry.target_id = "7"
    entry.detail = "available -> offline"
    entry.created_at = datetime(2026, 7, 12, tzinfo=timezone.utc)

    result = MagicMock()
    result.all.return_value = [(entry, "cpo@amphive.test")]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    res = await cpo_list_audit_log(user, db, limit=50, offset=0)

    assert res == [{
        "id": 1,
        "actor_user_id": 42,
        "actor_email": "cpo@amphive.test",
        "action": "plug.status_change",
        "target_type": "plug",
        "target_id": "7",
        "detail": "available -> offline",
        "created_at": "2026-07-12T00:00:00+00:00",
    }]
