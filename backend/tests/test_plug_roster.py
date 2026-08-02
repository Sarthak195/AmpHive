"""
Tests for the CPO plug-CRUD → retained-roster republish wiring
(routers/cpo.py). A plug create or a local_ip/name/max_current_a update must
republish amphive/gateways/{gw}/config so the gateway reconciles its slot table.

DB-free: the router coroutines are called directly with a mocked AsyncSession
(mirrors test_gateway_create.py), and state.mqtt_manager is monkeypatched to a
MagicMock so publish_plug_roster calls are observable without a live broker.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import state
from backend.routers import cpo
from backend.schemas import CpoPlugCreateRequest, CpoPlugUpdateRequest


def _user():
    u = MagicMock()
    u.tenant_id = 1
    u.email = "cpo@amphive.test"
    return u


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _rows_result(rows):
    r = MagicMock()
    r.all.return_value = rows
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()  # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_publish_gateway_roster_helper(monkeypatch):
    """The helper loads the gateway's plugs and hands them to publish_plug_roster."""
    fake_mgr = MagicMock()
    monkeypatch.setattr(state, "mqtt_manager", fake_mgr)

    db = _db(_rows_result([(7, "10.0.0.7", 16.0), (8, "10.0.0.8", None)]))
    await cpo._publish_gateway_roster(db, "gw-1")

    fake_mgr.publish_plug_roster.assert_called_once()
    gw, roster = fake_mgr.publish_plug_roster.call_args[0]
    assert gw == "gw-1"
    assert roster == [
        {"plug_id": 7, "local_ip": "10.0.0.7", "max_current_a": 16.0},
        {"plug_id": 8, "local_ip": "10.0.0.8", "max_current_a": None},
    ]


@pytest.mark.asyncio
async def test_publish_gateway_roster_noop_without_manager(monkeypatch):
    """No MQTT manager (tests / pre-startup) → the helper is inert, no DB query."""
    monkeypatch.setattr(state, "mqtt_manager", None)
    db = _db()
    await cpo._publish_gateway_roster(db, "gw-1")
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_create_plug_publishes_roster(monkeypatch):
    """Creating a plug republishes the gateway's roster with the new plug."""
    fake_mgr = MagicMock()
    monkeypatch.setattr(state, "mqtt_manager", fake_mgr)

    gateway = MagicMock()  # gateway ownership lookup returns a row
    db = _db(
        _scalar_result(gateway),
        _rows_result([(7, "10.0.0.7", None)]),  # roster query after commit
    )
    res = await cpo.cpo_create_plug(
        CpoPlugCreateRequest(gateway_id="gw-1", name="Bay 1", local_ip="10.0.0.7"),
        _user(), db,
    )

    assert res["status"] == "registered"
    fake_mgr.publish_plug_roster.assert_called_once()
    gw, roster = fake_mgr.publish_plug_roster.call_args[0]
    assert gw == "gw-1"
    assert roster == [{"plug_id": 7, "local_ip": "10.0.0.7", "max_current_a": None}]


@pytest.mark.asyncio
async def test_update_plug_local_ip_applies_and_republishes(monkeypatch):
    """A local_ip update writes the new IP and republishes the roster (so the
    gateway re-IPs the plug's slot after a DHCP change)."""
    fake_mgr = MagicMock()
    monkeypatch.setattr(state, "mqtt_manager", fake_mgr)

    plug = MagicMock()
    plug.gateway_id = "gw-1"
    db = _db(
        _scalar_result(plug),                     # plug load (join)
        _rows_result([(7, "10.0.0.9", None)]),    # roster query after commit
    )
    res = await cpo.cpo_update_plug(
        7, CpoPlugUpdateRequest(local_ip="10.0.0.9"), _user(), db,
    )

    assert res["status"] == "updated"
    assert plug.local_ip == "10.0.0.9"  # the new IP was applied to the row
    fake_mgr.publish_plug_roster.assert_called_once()
    gw, roster = fake_mgr.publish_plug_roster.call_args[0]
    assert gw == "gw-1"
    assert roster[0]["local_ip"] == "10.0.0.9"


@pytest.mark.asyncio
async def test_create_plug_persists_discovery_specs(monkeypatch):
    """[Discovery] rated_power_w / connector_type land on the new Plug row —
    the create request's fields, not defaults."""
    monkeypatch.setattr(state, "mqtt_manager", None)  # no-op the roster push

    gateway = MagicMock()
    db = _db(_scalar_result(gateway))
    await cpo.cpo_create_plug(
        CpoPlugCreateRequest(
            gateway_id="gw-1", name="Bay 1", local_ip="10.0.0.7",
            rated_power_w=3300, connector_type="Type 2",
        ),
        _user(), db,
    )

    # First add() call is the Plug row; a second (AuditLog) may follow.
    added = db.add.call_args_list[0][0][0]
    assert added.rated_power_w == 3300
    assert added.connector_type == "Type 2"


@pytest.mark.asyncio
async def test_update_plug_discovery_specs_applied_and_connector_clearable(monkeypatch):
    """rated_power_w/connector_type update the row; an empty-string
    connector_type clears it back to NULL (same "0 clears" spirit as
    max_current_a)."""
    monkeypatch.setattr(state, "mqtt_manager", None)  # no-op the roster push

    plug = MagicMock()
    plug.gateway_id = "gw-1"
    plug.connector_type = "3-pin 16A"
    db = _db(_scalar_result(plug))
    await cpo.cpo_update_plug(
        7, CpoPlugUpdateRequest(rated_power_w=7400, connector_type=""), _user(), db,
    )

    assert plug.rated_power_w == 7400
    assert plug.connector_type is None


@pytest.mark.asyncio
async def test_update_plug_rated_power_zero_clears_to_null(monkeypatch):
    """rated_power_w=0 clears the advertised spec back to NULL — the same
    sentinel-clear convention as max_current_a."""
    monkeypatch.setattr(state, "mqtt_manager", None)  # no-op the roster push

    plug = MagicMock()
    plug.gateway_id = "gw-1"
    plug.rated_power_w = 3300
    db = _db(_scalar_result(plug))
    await cpo.cpo_update_plug(
        7, CpoPlugUpdateRequest(rated_power_w=0), _user(), db,
    )

    assert plug.rated_power_w is None
