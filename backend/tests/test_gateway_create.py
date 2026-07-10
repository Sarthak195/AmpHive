"""
Tests for the CPO gateway-registration endpoint (POST /api/cpo/gateways).

Focus: vpn_ip is a legacy overlay field (NOT NULL + UNIQUE) that direct-MQTT
gateways don't set, so the endpoint falls back to the gateway_id — otherwise
two direct gateways would collide on the unique empty string. gateway_id is the
device MAC, which the firmware derives and shows in the setup portal.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.routers.cpo import cpo_create_gateway
from backend.schemas import CpoGatewayCreateRequest


def _user():
    u = MagicMock()
    u.tenant_id = 1
    u.email = "cpo@amphive.test"
    return u


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()          # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_create_gateway_defaults_vpn_ip_to_gateway_id():
    """No vpn_ip supplied → the persisted Gateway uses the gateway_id, so the
    UNIQUE/NOT NULL column is satisfied without an invented overlay IP."""
    db = _db(_result(None))  # no existing gateway with this id
    res = await cpo_create_gateway(
        CpoGatewayCreateRequest(gateway_id="aabbccddeeff", name="Site A - Bay 1"),
        _user(), db,
    )
    assert res["status"] == "registered"
    assert res["gateway_id"] == "aabbccddeeff"

    added = db.add.call_args[0][0]
    assert added.id == "aabbccddeeff"
    assert added.tenant_id == 1
    assert added.vpn_ip == "aabbccddeeff"  # fell back to the (unique) gateway_id
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_gateway_preserves_explicit_vpn_ip():
    """A caller that still provides vpn_ip (legacy overlay gateway) keeps it."""
    db = _db(_result(None))
    await cpo_create_gateway(
        CpoGatewayCreateRequest(gateway_id="gw-legacy", name="Old", vpn_ip="100.87.0.5"),
        _user(), db,
    )
    assert db.add.call_args[0][0].vpn_ip == "100.87.0.5"


@pytest.mark.asyncio
async def test_create_gateway_rejects_duplicate():
    """An existing gateway_id → 400, nothing added."""
    db = _db(_result(MagicMock()))  # lookup finds an existing row
    with pytest.raises(HTTPException) as exc:
        await cpo_create_gateway(
            CpoGatewayCreateRequest(gateway_id="dup", name="Dup"),
            _user(), db,
        )
    assert exc.value.status_code == 400
    db.add.assert_not_called()
