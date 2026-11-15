"""
CPO alert-feed window vs. the fw 2.8.0 forensic events — needs a real
PostgreSQL (CI's postgres:15 service, exported as TEST_DATABASE_URL). Skipped
locally: this repo's dev boxes run no database by policy (see test_wallet.py,
the same pattern).

What's proven here: the console's alert strip (frontend CpoAlerts.jsx) asks
GET /api/cpo/events?unacknowledged_only=true&limit=50 — the newest 50
unacknowledged events, newest first — and drops info rows only in the
browser. A GATEWAY_BOOT on every chip reset (fw >= 2.8.0, ~1.5 a day at the
2026-10-20 forensic's reset rate, far more in a brownout boot loop) would
fill that window and push an older unacknowledged critical out of the strip.
Through the real persist path (MQTTManager._persist_gateway_event) and the
real endpoint (routers/cpo/_events.py cpo_list_events): 60 boot reports and a
PLUG_RECOVERED after an UNAUTHORIZED_ON leave the critical as the only open
alert, and the forensic rows are still in the full feed (stored acknowledged,
not dropped).
"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status", "dispute_status", "plug_report_status", "reservation_status",
    "queued_charge_status",
]


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)

    # Mirror the app factory's expire_on_commit=False (backend/database/db.py).
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed_world(factory) -> dict:
    """One tenant with a gateway and one plug on it."""
    from backend.database.models import Gateway, Plug, Tenant

    tag = uuid.uuid4().hex[:10]

    async with factory() as db:
        tenant = Tenant(name=f"tenant-{tag}")
        db.add(tenant)
        await db.flush()

        u = uuid.uuid4().int
        gateway = Gateway(
            id=f"gw-{tag}", tenant_id=tenant.id, name="Test GW",
            vpn_ip=f"10.{u % 250 + 1}.{(u >> 8) % 250 + 1}.{(u >> 16) % 250 + 1}",
        )
        db.add(gateway)
        await db.flush()

        plug = Plug(gateway_id=gateway.id, name="Test Plug", local_ip="10.0.1.1")
        db.add(plug)
        await db.commit()

        return {"tenant_id": tenant.id, "gateway_id": gateway.id, "plug_id": plug.id}


@pytest.mark.asyncio
async def test_boot_reports_do_not_push_an_older_critical_out_of_the_alert_window(factory):
    from backend.routers.cpo import cpo_list_events
    from backend.services.mqtt_manager import MQTTManager

    world = await _seed_world(factory)
    gw, plug_id = world["gateway_id"], world["plug_id"]
    cpo = SimpleNamespace(id=1, email="cpo@amphive.test", tenant_id=world["tenant_id"])

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=factory)
    try:
        with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
            await mgr._persist_gateway_event(
                gw, plug_id, "UNAUTHORIZED_ON", "critical", "Plug switched ON with no session."
            )
            for n in range(60):
                await mgr._persist_gateway_event(
                    gw, None, "GATEWAY_BOOT", "info", f"Gateway booted: boot #{n}."
                )
            await mgr._persist_gateway_event(
                gw, plug_id, "PLUG_RECOVERED", "info", "Plug reachable again after ~95 s."
            )
    finally:
        MQTTManager._instance = None

    # Exactly the alert strip's query: the critical is still in its window.
    async with factory() as db:
        strip = await cpo_list_events(cpo, db, limit=50, offset=0,
                                      unacknowledged_only=True, severity=None)
    assert strip["total"] == 1
    assert [ev.event_type for ev in strip["items"]] == ["UNAUTHORIZED_ON"]

    # The forensic rows are recorded, just not as open alerts (the Health
    # page shows them with "Unacknowledged only" unticked).
    async with factory() as db:
        feed = await cpo_list_events(cpo, db, limit=100, offset=0,
                                     unacknowledged_only=False, severity=None)
    assert feed["total"] == 62
    forensic = [ev for ev in feed["items"] if ev.event_type in ("GATEWAY_BOOT", "PLUG_RECOVERED")]
    assert len(forensic) == 61
    assert all(ev.acknowledged for ev in forensic)
