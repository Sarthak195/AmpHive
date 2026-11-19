"""
[Plug hours] DB-gated tests (CI's postgres service, TEST_DATABASE_URL; skipped
locally): the JSONB columns round-trip, the reservation route refuses a
booking outside the hours under the plug lock, and the walk-up start route
refuses a closed plug and persists the max_session_minutes clamp. Reuses the
seed rig and the fresh-schema fixture from test_reservations.py.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.services.pricing import _zone
from backend.tests.test_reservations import (  # noqa: F401 (factory is a fixture)
    TEST_DATABASE_URL,
    _book,
    _seed_gateway,
    _seed_plug,
    _seed_tenant,
    _seed_user,
    _start_session,
    factory,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)


def _closed_today_hours():
    """Open only three weekdays from today in the tenant's default zone."""
    wd = datetime.now(_zone("Asia/Kolkata")).weekday()
    return [{"days": 1 << ((wd + 3) % 7), "start": 0, "end": 1440}]


async def _set_plug(factory, plug_id, **fields):  # noqa: F811
    from backend.database.models import Plug

    async with factory() as db:
        plug = (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one()
        for key, value in fields.items():
            setattr(plug, key, value)
        await db.commit()


async def _seed(factory):  # noqa: F811
    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id)
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, tenant_id=tenant_id)
    return plug_id, uid


@pytest.mark.asyncio
async def test_booking_outside_hours_409_inside_ok(factory, monkeypatch):  # noqa: F811
    plug_id, uid = await _seed(factory)
    await _set_plug(factory, plug_id, opening_hours=_closed_today_hours())
    start = datetime.now(timezone.utc) + timedelta(minutes=30)

    with pytest.raises(HTTPException) as exc:
        await _book(factory, monkeypatch, plug_id=plug_id, user_id=uid,
                    start_at=start, end_at=start + timedelta(hours=1))
    assert exc.value.status_code == 409
    assert "opening hours" in exc.value.detail

    await _set_plug(factory, plug_id, opening_hours=[{"days": 127, "start": 0, "end": 1440}])
    resp = await _book(factory, monkeypatch, plug_id=plug_id, user_id=uid,
                       start_at=start, end_at=start + timedelta(hours=1))
    assert resp.status == "booked"


@pytest.mark.asyncio
async def test_closed_start_refused_and_max_minutes_clamp_persisted(factory, monkeypatch):  # noqa: F811
    from backend.database.models import ChargingSession, Plug, PlugStatus

    plug_id, uid = await _seed(factory)
    await _set_plug(factory, plug_id, opening_hours=_closed_today_hours(),
                    amenities=["wifi"], access_note="Gate on the left")

    with pytest.raises(HTTPException) as exc:
        await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=uid)
    assert exc.value.status_code == 409
    assert "closed" in exc.value.detail
    async with factory() as db:
        plug = (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one()
        assert plug.status == PlugStatus.AVAILABLE
        assert plug.amenities == ["wifi"]                      # JSONB round-trip
        assert (await db.execute(
            select(ChargingSession).where(ChargingSession.plug_id == plug_id)
        )).first() is None

    await _set_plug(factory, plug_id, opening_hours=None, max_session_minutes=20)
    result = await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=uid)
    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == result["session_id"])
        )).scalar_one()
    assert session.max_duration_seconds == 1200
