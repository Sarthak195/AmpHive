"""
Tests for the CPO OTA trigger endpoint (POST /api/cpo/gateways/{id}/ota).

The route validates tenant ownership + gateway liveness, finds a plug to
route the command through (the firmware only subscribes to per-plug command
topics), and publishes the OTA command. Failures map to specific statuses:
404 (not yours / missing), 409 (offline, or no plugs), 502 (publish failed).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.database.models import GatewayStatus
from backend.routers.cpo import cpo_gateway_ota
from backend.schemas import CpoGatewayOtaRequest

URL = "https://storage.googleapis.com/amphive-fw/amphive-gateway-1.4.0.bin"


def _online_gateway(last_seen_at=None):
    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE
    # Default deliberately stale: OTA gates on the ONLINE flag, not telemetry
    # freshness (see the endpoint), so this must still be accepted.
    gw.last_seen_at = last_seen_at or (datetime.now(timezone.utc) - timedelta(hours=1))
    return gw


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
    return db


@pytest.mark.asyncio
async def test_ota_published_for_online_gateway():
    db = _db(_result(_online_gateway()), _result(1))  # gateway, then plug_id=1
    with patch("backend.routers.cpo._gateways.state") as state:
        state.mqtt_manager.send_gateway_ota.return_value = True
        res = await cpo_gateway_ota("gw-1", CpoGatewayOtaRequest(firmware_url=URL), _user(), db)

    assert res["status"] == "ota_triggered"
    state.mqtt_manager.send_gateway_ota.assert_called_once_with("gw-1", 1, URL)


@pytest.mark.asyncio
async def test_ota_404_when_not_owned():
    db = _db(_result(None))  # gateway lookup finds nothing
    with pytest.raises(HTTPException) as exc:
        await cpo_gateway_ota("gw-x", CpoGatewayOtaRequest(firmware_url=URL), _user(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_ota_409_when_gateway_offline():
    gw = _online_gateway()
    gw.status = GatewayStatus.OFFLINE
    db = _db(_result(gw))
    with pytest.raises(HTTPException) as exc:
        await cpo_gateway_ota("gw-1", CpoGatewayOtaRequest(firmware_url=URL), _user(), db)
    assert exc.value.status_code == 409
    assert "offline" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_ota_409_when_gateway_has_no_plugs():
    db = _db(_result(_online_gateway()), _result(None))  # live, but no plug rows
    with pytest.raises(HTTPException) as exc:
        await cpo_gateway_ota("gw-1", CpoGatewayOtaRequest(firmware_url=URL), _user(), db)
    assert exc.value.status_code == 409
    assert "no registered plugs" in exc.value.detail


@pytest.mark.asyncio
async def test_ota_502_when_publish_fails():
    db = _db(_result(_online_gateway()), _result(1))
    with patch("backend.routers.cpo._gateways.state") as state:
        state.mqtt_manager.send_gateway_ota.return_value = False
        with pytest.raises(HTTPException) as exc:
            await cpo_gateway_ota("gw-1", CpoGatewayOtaRequest(firmware_url=URL), _user(), db)
    assert exc.value.status_code == 502


def test_ota_request_rejects_non_http_url():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        CpoGatewayOtaRequest(firmware_url="ftp://evil/x.bin")


def test_ota_request_rejects_plain_http_url():
    """Images cross the public internet; fw >= 1.4.0 refuses http anyway."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        CpoGatewayOtaRequest(firmware_url="http://100.87.241.70:8070/fw.bin")
