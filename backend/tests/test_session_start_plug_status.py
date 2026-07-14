"""
Tests for the plug-status gate on /api/sessions/start (TD#22).

A session may only start on an AVAILABLE plug. Previously only OCCUPIED was
rejected, so a plug the CPO deliberately set OFFLINE/MAINTENANCE (or a freshly
registered plug, which defaults to OFFLINE) was still startable — it pinned
OCCUPIED and billed nothing.

DB-free: uses the mocked-AsyncSession pattern from test_max_active_sessions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from backend.database.models import GatewayStatus, PlugStatus
from backend.routers.sessions import start_charging_session
from backend.schemas import SessionStartRequest


def _user(user_id=1):
    u = MagicMock()
    u.id = user_id
    u.email = "driver@example.com"
    u.coin_balance = 100
    return u


def _plug(status, plug_id=1):
    p = MagicMock()
    p.id = plug_id
    p.status = status
    p.group_id = None  # public / ungrouped: skips the membership check
    return p


def _scalar_one(value):
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [PlugStatus.OFFLINE, PlugStatus.MAINTENANCE])
async def test_start_rejected_on_out_of_service_plug(status):
    user = _user()
    db = _db(
        _scalar_one(user),                    # user row lock
        _scalar_one(0),                       # no ACTIVE sessions
        _scalar_one_or_none(_plug(status)),   # plug row lock
    )

    with pytest.raises(HTTPException) as exc_info:
        await start_charging_session(SessionStartRequest(plug_id=1), user, db)

    assert exc_info.value.status_code == 409
    assert "out of service" in exc_info.value.detail
    assert status.value in exc_info.value.detail
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_start_rejected_on_occupied_plug_keeps_in_use_message():
    """OCCUPIED keeps its own message (it's a different user story than a
    plug an operator disabled)."""
    user = _user()
    db = _db(
        _scalar_one(user),
        _scalar_one(0),
        _scalar_one_or_none(_plug(PlugStatus.OCCUPIED)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await start_charging_session(SessionStartRequest(plug_id=1), user, db)

    assert exc_info.value.status_code == 409
    assert "in use" in exc_info.value.detail


@pytest.mark.asyncio
async def test_start_rejected_on_unpowered_plug():
    """[Plug power] A plug on a LIVE gateway that reported before but has since
    lost power (last_telemetry_at STALE, past PLUG_POWER_STALE_SEC) is refused
    with its own 409, distinct from the gateway-offline message. Starting there
    pins OCCUPIED and bills nothing. (A never-reported/NULL plug is intentionally
    allowed through — absence of a heartbeat isn't proof of no power.)"""
    user = _user()
    gateway = MagicMock()
    gateway.status = GatewayStatus.ONLINE
    gateway.last_seen_at = datetime.now(timezone.utc)  # gateway IS live
    plug = _plug(PlugStatus.AVAILABLE)
    # Reported an hour ago, then went silent -> positive evidence of power loss.
    plug.last_telemetry_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
    db = _db(
        _scalar_one(user),
        _scalar_one(0),
        _scalar_one_or_none(plug),
        MagicMock(),                          # [Reservations] lazy-expiry UPDATE
        _scalar_one_or_none(None),            # [Reservations] no covering booking
        _scalar_one(gateway),                 # gateway lookup (live)
    )

    with pytest.raises(HTTPException) as exc_info:
        await start_charging_session(SessionStartRequest(plug_id=1), user, db)

    assert exc_info.value.status_code == 409
    assert "no power" in exc_info.value.detail
    assert "gateway is offline" not in exc_info.value.detail


@pytest.mark.asyncio
async def test_available_plug_passes_the_status_gate():
    """An AVAILABLE plug must get PAST the status check: the request proceeds
    to the gateway-liveness gate, whose distinct 409 message proves it."""
    user = _user()
    gateway = MagicMock()
    gateway.status = GatewayStatus.OFFLINE  # not live -> distinguishable 409
    db = _db(
        _scalar_one(user),
        _scalar_one(0),
        _scalar_one_or_none(_plug(PlugStatus.AVAILABLE)),
        MagicMock(),                          # [Reservations] lazy-expiry UPDATE
        _scalar_one_or_none(None),            # [Reservations] no covering booking
        _scalar_one(gateway),                # gateway lookup
    )

    with pytest.raises(HTTPException) as exc_info:
        await start_charging_session(SessionStartRequest(plug_id=1), user, db)

    assert exc_info.value.status_code == 409
    assert "gateway is offline" in exc_info.value.detail
    assert "out of service" not in exc_info.value.detail
