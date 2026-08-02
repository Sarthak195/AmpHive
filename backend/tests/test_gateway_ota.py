"""
Tests for the CPO OTA trigger endpoint (POST /api/cpo/gateways/{id}/ota) and
its firmware-release version picker (feat/ota-version-picker).

The route validates tenant ownership + gateway liveness, resolves the image
URL from either a registered release (`release_id`, the version-picker
flow) or an admin-only custom `firmware_url`, finds a plug to route the
command through (the firmware only subscribes to per-plug command topics),
and publishes the OTA command. Failures map to specific statuses: 404 (not
yours / missing gateway or release), 409 (offline, or no plugs), 403
(non-admin custom URL), 502 (publish failed).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pydantic
import pytest
from fastapi import HTTPException

from backend.database.models import GatewayStatus, UserRole
from backend.routers.cpo import cpo_gateway_ota, cpo_list_firmware_releases
from backend.schemas import CpoGatewayOtaRequest

URL = "https://storage.googleapis.com/amphive-fw/amphive-gateway-1.4.0.bin"


def _online_gateway(last_seen_at=None):
    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE
    # Default deliberately stale: OTA gates on the ONLINE flag, not telemetry
    # freshness (see the endpoint), so this must still be accepted.
    gw.last_seen_at = last_seen_at or (datetime.now(timezone.utc) - timedelta(hours=1))
    return gw


def _user(role=UserRole.ADMIN):
    u = MagicMock()
    u.tenant_id = 1
    u.email = "cpo@amphive.test"
    u.role = role
    return u


def _release(release_id=1, version="1.5.0-direct", url=URL, is_active=True):
    r = MagicMock()
    r.id = release_id
    r.version = version
    r.url = url
    r.notes = None
    r.is_active = is_active
    r.created_at = datetime.now(timezone.utc)
    return r


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


# --- Admin custom-URL escape hatch (firmware_url) ---------------------------

@pytest.mark.asyncio
async def test_ota_published_for_online_gateway_admin_custom_url():
    db = _db(_result(_online_gateway()), _result(1))  # gateway, then plug_id=1
    with patch("backend.routers.cpo._gateways.state") as state:
        state.mqtt_manager.send_gateway_ota.return_value = True
        res = await cpo_gateway_ota(
            "gw-1", CpoGatewayOtaRequest(firmware_url=URL), _user(role=UserRole.ADMIN), db
        )

    assert res["status"] == "ota_triggered"
    assert res["firmware_url"] == URL
    assert res["release_version"] is None
    state.mqtt_manager.send_gateway_ota.assert_called_once_with("gw-1", 1, URL)


@pytest.mark.asyncio
async def test_ota_custom_url_rejected_for_non_admin():
    """The escape hatch is admin-only; a cpo gets 403 before any plug lookup."""
    db = _db(_result(_online_gateway()))
    with pytest.raises(HTTPException) as exc:
        await cpo_gateway_ota(
            "gw-1", CpoGatewayOtaRequest(firmware_url=URL), _user(role=UserRole.CPO), db
        )
    assert exc.value.status_code == 403
    assert "admin" in exc.value.detail.lower()


# --- Version-picker flow (release_id) ---------------------------------------

@pytest.mark.asyncio
async def test_ota_published_for_release_id():
    release = _release(release_id=7, version="2.3.0-direct", url=URL)
    db = _db(_result(_online_gateway()), _result(release), _result(1))  # gateway, release, plug
    with patch("backend.routers.cpo._gateways.state") as state:
        state.mqtt_manager.send_gateway_ota.return_value = True
        res = await cpo_gateway_ota(
            "gw-1", CpoGatewayOtaRequest(release_id=7), _user(role=UserRole.CPO), db
        )

    assert res["status"] == "ota_triggered"
    assert res["firmware_url"] == URL
    assert res["release_version"] == "2.3.0-direct"
    state.mqtt_manager.send_gateway_ota.assert_called_once_with("gw-1", 1, URL)


@pytest.mark.asyncio
async def test_ota_404_when_release_not_found_or_inactive():
    db = _db(_result(_online_gateway()), _result(None))  # gateway, release lookup finds nothing
    with pytest.raises(HTTPException) as exc:
        await cpo_gateway_ota(
            "gw-1", CpoGatewayOtaRequest(release_id=999), _user(role=UserRole.CPO), db
        )
    assert exc.value.status_code == 404
    assert "release" in exc.value.detail.lower()


# --- Shared guards (gateway ownership / liveness / plugs / publish) --------

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


# --- Request schema validation ----------------------------------------------

def test_ota_request_rejects_non_http_url():
    with pytest.raises(pydantic.ValidationError):
        CpoGatewayOtaRequest(firmware_url="ftp://evil/x.bin")


def test_ota_request_rejects_plain_http_url():
    """Images cross the public internet; fw >= 1.4.0 refuses http anyway."""
    with pytest.raises(pydantic.ValidationError):
        CpoGatewayOtaRequest(firmware_url="http://100.87.241.70:8070/fw.bin")


def test_ota_request_rejects_neither_release_id_nor_url():
    with pytest.raises(pydantic.ValidationError):
        CpoGatewayOtaRequest()


def test_ota_request_rejects_both_release_id_and_url():
    with pytest.raises(pydantic.ValidationError):
        CpoGatewayOtaRequest(release_id=1, firmware_url=URL)


def test_ota_request_accepts_release_id_alone():
    req = CpoGatewayOtaRequest(release_id=3)
    assert req.release_id == 3
    assert req.firmware_url is None


# --- CPO firmware-release list (version picker source) ---------------------

@pytest.mark.asyncio
async def test_list_firmware_releases_only_active_and_semver_descending():
    """2.10.0 must sort above 2.9.0 (not a string sort), and an inactive
    release never reaches the CPO picker — the query itself filters
    is_active, so this also exercises that the endpoint only requests
    active rows."""
    releases = [
        _release(1, "2.9.0"),
        _release(2, "2.10.0"),
        _release(3, "1.9.0-direct"),
        _release(4, "2.10.0-direct"),
    ]
    scalars_result = MagicMock()
    scalars_result.scalars.return_value.all.return_value = releases
    db = AsyncMock()
    db.execute = AsyncMock(return_value=scalars_result)

    items = await cpo_list_firmware_releases(_user(), db)

    versions = [item["version"] for item in items]
    assert versions == ["2.10.0-direct", "2.10.0", "2.9.0", "1.9.0-direct"]
    assert all("id" in item and "url" in item for item in items)
