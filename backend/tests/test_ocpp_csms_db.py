"""
[C2] OCPP 1.6J central system, DB-gated tests (CI's postgres service,
TEST_DATABASE_URL; skipped locally). Reuses the fresh-schema factory and seed
rig from test_reservations.py.

1. A Starlette TestClient simulator charge point runs a whole session over
   the WebSocket against the real database: real bcrypt Basic auth (a wrong
   password gets 401), BootNotification, Authorize accepted / blocked (balance
   below the start floor) / invalid (unknown tag), StartTransaction blocked
   for the poor driver and accepted for the funded one (a retried start gets
   the same transactionId), MeterValues, StopTransaction. The session bills
   through the ledger exactly like a walk-up session, and no MQTT command
   (ON, OFF or SET_INTERVAL) is ever published.
2. Admission and scoping, through the handlers directly: another driver's
   reservation blocks a start while the holder's own start goes through; a
   private group blocks a non-member; a closed plug (opening hours) blocks;
   a second charge point (another tenant) can neither meter nor stop the
   first one's transaction; and the app's own
   walk-up start refuses an OCPP connector (its backing gateway is never
   live), so a charge point's plugs only start through StartTransaction.
"""
import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from starlette.testclient import TestClient, WebSocketDenialResponse

from backend.tests.test_reservations import (  # noqa: F401 (factory is a fixture)
    TEST_DATABASE_URL,
    _seed_private_group,
    _seed_reservation,
    _seed_tenant,
    _seed_user,
    _start_session,
    factory,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

PASSWORD = "sim-password-123456"


async def _seed_cp(factory, tenant_id: int, cp_id: str):  # noqa: F811
    from backend.database.models import (
        Gateway,
        GatewayStatus,
        OcppChargePoint,
        Plug,
        PlugStatus,
    )
    from backend.services.auth import hash_password

    gateway_id = f"ocpp-{cp_id}"
    async with factory() as db:
        db.add(Gateway(
            id=gateway_id, tenant_id=tenant_id, name=cp_id, vpn_ip=gateway_id,
            status=GatewayStatus.OFFLINE,
        ))
        await db.flush()
        plug = Plug(
            gateway_id=gateway_id, name=f"{cp_id} #1", local_ip="ocpp",
            plug_model="ocpp1.6j", unique_id=f"ocpp:{cp_id}:1",
            status=PlugStatus.AVAILABLE,
        )
        cp = OcppChargePoint(
            charge_point_id=cp_id, tenant_id=tenant_id, gateway_id=gateway_id,
            password_hash=hash_password(PASSWORD),
        )
        db.add_all([plug, cp])
        await db.commit()
        return cp.id, plug.id


async def _seed_tag(factory, user_id: int, tag: str) -> None:  # noqa: F811
    from backend.database.models import OcppIdTag
    from backend.services.ocpp_adapter import hash_id_tag

    async with factory() as db:
        db.add(OcppIdTag(user_id=user_id, tag_hash=hash_id_tag(tag)))
        await db.commit()


@pytest_asyncio.fixture()
async def rig(factory, monkeypatch):  # noqa: F811
    """Point the adapter and the telemetry persist path at the test DB, with
    every MQTT publish recorded (none may happen) and notices/emails stubbed."""
    import backend.services.billing_emails as billing_emails
    import backend.services.notifications as notifications
    from backend import state
    from backend.database import db as db_module
    from backend.routers import ocpp as ocpp_router
    from backend.services.mqtt_manager import MQTTManager
    from backend.services.rate_limit import SlidingWindowRateLimiter

    monkeypatch.setattr(db_module, "async_session_factory", factory)
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=factory, telemetry_store=state.telemetry_store)
    mgr.send_plug_command = MagicMock(return_value=True)
    mgr.send_plug_interval = MagicMock(return_value=True)
    mgr.send_plug_limits = MagicMock(return_value=True)
    monkeypatch.setattr(state, "mqtt_manager", mgr)
    monkeypatch.setattr(notifications, "notify", AsyncMock())
    monkeypatch.setattr(billing_emails, "schedule", MagicMock())
    monkeypatch.setattr(billing_emails, "send_session_bill", MagicMock())
    monkeypatch.setattr(ocpp_router, "handshake_limiter", SlidingWindowRateLimiter(100, 60))
    monkeypatch.setattr(ocpp_router, "connections", {})
    yield mgr
    MQTTManager._instance = None


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _simulate(flow, password: str = PASSWORD):
    """Run `flow(call)` as charge point CP1 over a real WebSocket. Runs in a
    worker thread: the TestClient drives the app on its own event loop."""
    from backend.routers import ocpp as ocpp_router

    app = FastAPI()
    app.include_router(ocpp_router.router)
    client = TestClient(app)
    with client.websocket_connect(
        "/ocpp/CP1", subprotocols=["ocpp1.6"], headers=_basic("CP1", password),
    ) as ws:
        counter = iter(range(1, 1000))

        def call(action, payload):
            ws.send_text(json.dumps([2, f"m{next(counter)}", action, payload]))
            return json.loads(ws.receive_text())

        return flow(call)


def _sv(wh: float, watts: float) -> list:
    return [{"timestamp": "2026-11-19T10:00:00Z", "sampledValue": [
        {"value": str(wh), "unit": "Wh"},
        {"value": str(watts), "measurand": "Power.Active.Import", "unit": "W"},
    ]}]


@pytest.mark.asyncio
async def test_simulator_session_bills_through_the_ledger(factory, rig):  # noqa: F811
    from backend.database.models import (
        ChargingSession,
        LedgerTransaction,
        OcppChargePoint,
        OcppTransaction,
        Plug,
        PlugStatus,
        SessionStatus,
        TransactionType,
        User,
    )
    from backend.services.money import energy_cost

    tenant_id = await _seed_tenant(factory)
    _cp_pk, plug_id = await _seed_cp(factory, tenant_id, "CP1")
    rich = await _seed_user(factory, balance="500.00")
    poor = await _seed_user(factory, balance="10.00")
    await _seed_tag(factory, rich, "TAG-RICH")
    await _seed_tag(factory, poor, "TAG-POOR")

    with pytest.raises(WebSocketDenialResponse) as denied:
        await asyncio.to_thread(_simulate, lambda call: None, "wrong-password")
    assert denied.value.status_code == 401

    def flow(call):
        r = {
            "boot": call("BootNotification", {
                "chargePointVendor": "Sim", "chargePointModel": "S1", "firmwareVersion": "1.0",
            }),
            "auth_ok": call("Authorize", {"idTag": "tag-rich"}),  # tags are case-insensitive
            "auth_poor": call("Authorize", {"idTag": "TAG-POOR"}),
            "auth_unknown": call("Authorize", {"idTag": "TAG-NOBODY"}),
            "start_poor": call("StartTransaction", {
                "connectorId": 1, "idTag": "TAG-POOR", "meterStart": 1000, "timestamp": "t",
            }),
            "start": call("StartTransaction", {
                "connectorId": 1, "idTag": "TAG-RICH", "meterStart": 1000, "timestamp": "t",
            }),
        }
        tx = r["start"][2]["transactionId"]
        r["start_retry"] = call("StartTransaction", {
            "connectorId": 1, "idTag": "TAG-RICH", "meterStart": 1000, "timestamp": "t",
        })
        r["meter"] = call("MeterValues", {
            "connectorId": 1, "transactionId": tx, "meterValue": _sv(2500, 7000),
        })
        r["stop"] = call("StopTransaction", {
            "transactionId": tx, "meterStop": 3000, "timestamp": "t",
            "idTag": "TAG-RICH", "reason": "EVDisconnected",
        })
        return r

    r = await asyncio.to_thread(_simulate, flow)

    assert r["boot"][2]["status"] == "Accepted"
    assert r["auth_ok"][2] == {"idTagInfo": {"status": "Accepted"}}
    assert r["auth_poor"][2] == {"idTagInfo": {"status": "Blocked"}}
    assert r["auth_unknown"][2] == {"idTagInfo": {"status": "Invalid"}}
    assert r["start_poor"][2] == {"transactionId": 0, "idTagInfo": {"status": "Blocked"}}
    assert r["start"][2]["idTagInfo"]["status"] == "Accepted"
    tx = r["start"][2]["transactionId"]
    assert tx > 0
    assert r["start_retry"][2] == {"transactionId": tx, "idTagInfo": {"status": "Accepted"}}
    assert r["meter"][2] == {}
    assert r["stop"][2] == {"idTagInfo": {"status": "Accepted"}}

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == tx)
        )).scalar_one()
        assert session.user_id == rich
        assert session.plug_id == plug_id
        assert session.status == SessionStatus.COMPLETED
        assert session.energy_kwh == pytest.approx(2.0)
        expected = energy_cost(2.0, session.rate_coins_per_kwh)
        ledger = (await db.execute(
            select(LedgerTransaction).where(LedgerTransaction.session_id == tx)
        )).scalar_one()
        assert ledger.transaction_type == TransactionType.SESSION_DEBIT
        assert ledger.amount == -expected
        balance = (await db.execute(select(User.coin_balance).where(User.id == rich))).scalar_one()
        assert balance == ledger.balance_after
        assert (await db.execute(
            select(Plug.status).where(Plug.id == plug_id)
        )).scalar_one() == PlugStatus.AVAILABLE
        ocpp_tx = (await db.execute(
            select(OcppTransaction).where(OcppTransaction.session_id == tx)
        )).scalar_one()
        assert ocpp_tx.meter_start_wh == 1000
        assert (await db.execute(select(OcppChargePoint.vendor))).scalar_one() == "Sim"
        # Only the funded driver's session exists: the blocked start wrote nothing.
        assert len((await db.execute(select(ChargingSession.id))).all()) == 1

    rig.send_plug_command.assert_not_called()
    rig.send_plug_interval.assert_not_called()


def _closed_today_hours():
    from backend.services.pricing import _zone

    wd = datetime.now(_zone("Asia/Kolkata")).weekday()
    return [{"days": 1 << ((wd + 3) % 7), "start": 0, "end": 1440}]


@pytest.mark.asyncio
async def test_admission_gates_and_charge_point_scoping(factory, rig, monkeypatch):  # noqa: F811
    from backend.database.models import ChargingSession, Plug, SessionStatus
    from backend.services import ocpp_adapter as adapter

    t1 = await _seed_tenant(factory)
    t2 = await _seed_tenant(factory)
    cp1_pk, plug1 = await _seed_cp(factory, t1, "CP1")
    cp2_pk, plug2 = await _seed_cp(factory, t2, "CP2")
    driver = await _seed_user(factory, balance="500.00")
    holder = await _seed_user(factory, balance="500.00")
    await _seed_tag(factory, driver, "TAG-A")
    await _seed_tag(factory, holder, "TAG-B")
    cp1 = adapter.ChargePointCtx(cp1_pk, "CP1", t1, "ocpp-CP1")
    cp2 = adapter.ChargePointCtx(cp2_pk, "CP2", t2, "ocpp-CP2")

    def start(tag):
        return adapter.StartTransactionReq(connectorId=1, idTag=tag, meterStart=0, timestamp="t")

    # Another driver's booking covers now: blocked. The holder starts fine.
    now = datetime.now(timezone.utc)
    await _seed_reservation(
        factory, plug_id=plug1, user_id=holder, tenant_id=t1,
        start_at=now - timedelta(minutes=1), end_at=now + timedelta(minutes=59),
    )
    assert await adapter.handle_start(cp1, start("TAG-A")) == {
        "transactionId": 0, "idTagInfo": {"status": "Blocked"},
    }
    started = await adapter.handle_start(cp1, start("TAG-B"))
    assert started["idTagInfo"]["status"] == "Accepted"
    tx = started["transactionId"]

    # CP2 (another tenant) can neither meter nor stop CP1's transaction.
    await adapter.handle_meter_values(cp2, adapter.MeterValuesReq(
        connectorId=1, transactionId=tx, meterValue=_sv(99000, 7000),
    ))
    assert await adapter.handle_stop(cp2, adapter.StopTransactionReq(
        transactionId=tx, meterStop=99000, timestamp="t", idTag="TAG-B",
    )) == {"idTagInfo": {"status": "Invalid"}}
    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == tx)
        )).scalar_one()
    assert session.status == SessionStatus.ACTIVE
    assert session.energy_kwh == 0.0

    # Private group: a non-member is blocked.
    group_id = await _seed_private_group(factory, t2)
    async with factory() as db:
        plug = (await db.execute(select(Plug).where(Plug.id == plug2))).scalar_one()
        plug.group_id = group_id
        await db.commit()
    assert await adapter.handle_start(cp2, start("TAG-A")) == {
        "transactionId": 0, "idTagInfo": {"status": "Blocked"},
    }

    # Opening hours: CP2's connector is closed today, so the start is blocked.
    async with factory() as db:
        plug = (await db.execute(select(Plug).where(Plug.id == plug2))).scalar_one()
        plug.group_id = None
        plug.opening_hours = _closed_today_hours()
        await db.commit()
    assert await adapter.handle_start(cp2, start("TAG-A")) == {
        "transactionId": 0, "idTagInfo": {"status": "Blocked"},
    }
    rig.send_plug_command.assert_not_called()

    # The app's own walk-up start refuses an OCPP connector: its backing
    # gateway is never live (last, since this helper swaps the MQTT stub).
    with pytest.raises(HTTPException) as exc:
        await _start_session(factory, monkeypatch, plug_id=plug2, user_id=driver)
    assert exc.value.status_code == 409
    assert "offline" in exc.value.detail
