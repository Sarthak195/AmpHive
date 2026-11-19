"""
[Durable notice state] Wave-4 lane DUR (migration 0051): notice dedupe that
survives a backend restart.

- F1 "looks fully charged" and C7 nearly_done / power_dropped are claimed once
  per session on the charging_sessions row (MQTTTelemetryMixin.
  _claim_session_notice), only when a detector fires.
- F3 offline / back-online state is on the gateways row (operator_alerts.py;
  its DB-free tests live in test_operator_alerts.py).

Two tiers, like test_payouts.py: DB-free tests (fake session, always run) for
the telemetry wiring, including "no write when nothing transitions"; DB-gated
tests (TEST_DATABASE_URL, CI's postgres service, skipped locally) for the
claim UPDATE, the gateway row lock and the startup scan.
"""
import asyncio
import itertools
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from backend.services import operator_alerts as oa
from backend.tests.test_charge_full import (
    PLUG,
    SID,
    T0,
    _manager,
    _persist_frame,
    _real_handler_manager,
    _session_row,
)


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    import backend.services.mqtt_manager as mm
    import backend.services.session_reaper as reaper

    monkeypatch.setattr(mm, "CHARGE_FULL_ACTIVE_W", 200.0)
    monkeypatch.setattr(mm, "CHARGE_FULL_IDLE_W", 30.0)
    monkeypatch.setattr(mm, "CHARGE_FULL_GRACE_MIN", 5.0)
    monkeypatch.setattr(mm, "CHARGE_FULL_MINUTES", 10.0)
    monkeypatch.setattr(reaper, "STALE_TIMEOUT_SEC", 300.0)
    yield
    mm.MQTTManager._instance = None


def _types(notify):
    return [c.args[1] for c in notify.await_args_list]


# ===========================================================================
# Tier 1: DB-free telemetry wiring
# ===========================================================================

@pytest.mark.asyncio
async def test_no_notice_write_when_nothing_transitions():
    """Steady charging frames: no detector fires, so no claim UPDATE runs and
    the frame transaction never touches the *_notified_at columns."""
    mgr = _manager()
    mgr._claim_session_notice = AsyncMock(return_value=True)
    row = _session_row(auto_stop=True)
    row.max_kwh = 50.0                          # 5 kWh delivered = 10%
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for m in range(10, 40):
            await _persist_frame(mgr, row, m, 1500.0)
    mgr._claim_session_notice.assert_not_awaited()
    notify.assert_not_awaited()
    assert row.full_notified_at is None
    assert row.nearly_done_notified_at is None
    assert row.power_dropped_notified_at is None


@pytest.mark.asyncio
async def test_each_notice_is_claimed_once_on_its_transition():
    mgr = _manager()
    mgr._claim_session_notice = AsyncMock(return_value=True)
    row = _session_row(auto_stop=False)
    row.max_kwh = 5.5                           # 5.0 kWh = 91%
    with patch("backend.services.notifications.notify", AsyncMock()):
        for m in range(10, 20):
            await _persist_frame(mgr, row, m, 600.0)    # nearly + dropped
        for m in range(20, 40):
            await _persist_frame(mgr, row, m, 0.0)      # then looks full
    claimed = [c.args for c in mgr._claim_session_notice.await_args_list]
    assert claimed == [
        (SID, "nearly_done_notified_at"),
        (SID, "power_dropped_notified_at"),
        (SID, "full_notified_at"),
    ]


@pytest.mark.asyncio
async def test_restart_after_the_full_notice_neither_repeats_it_nor_skips_the_stop():
    """The row already records the "looks full" notice (sent before a
    restart): the fresh detector fires again, the opted-in stop still runs
    through finalize, no second notice and no claim write."""
    from backend.services.mqtt.charge_full import CHARGE_COMPLETE_REASON

    mgr = _real_handler_manager()
    mgr._claim_session_notice = AsyncMock(return_value=True)
    row = _session_row(auto_stop=True)
    row.full_notified_at = T0 + timedelta(minutes=20)
    finalize = AsyncMock(return_value={"energy_kwh": 5.0, "coins_spent": 25.0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize), \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for m in range(30, 45):
            await _persist_frame(mgr, row, m, 0.0)
    finalize.assert_awaited_once()
    assert finalize.await_args.kwargs["reason"] == CHARGE_COMPLETE_REASON
    assert "charge_full" not in _types(notify)
    mgr._claim_session_notice.assert_not_awaited()
    mgr.telemetry_store.mark_charge_full.assert_called_once_with(PLUG)


@pytest.mark.asyncio
async def test_restart_after_c7_notices_sends_none_again():
    mgr = _manager()
    mgr._claim_session_notice = AsyncMock(return_value=True)
    row = _session_row(auto_stop=False)
    row.max_kwh = 5.5
    row.nearly_done_notified_at = T0
    row.power_dropped_notified_at = T0
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for m in range(10, 20):
            await _persist_frame(mgr, row, m, 600.0)
    notify.assert_not_awaited()
    mgr._claim_session_notice.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_c7_notice_after_a_restart_once_the_row_says_full():
    """[C7 + F1] has_fired is False in a restarted process; the persisted
    full_notified_at keeps "almost done" / "slowing down" quiet."""
    mgr = _manager()
    mgr._claim_session_notice = AsyncMock(return_value=True)
    row = _session_row(auto_stop=False)
    row.max_kwh = 5.5
    row.full_notified_at = T0
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for m in range(10, 20):
            await _persist_frame(mgr, row, m, 600.0)
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_lost_claim_sends_nothing():
    """The UPDATE matched no row (already claimed, or the session ended):
    no notice."""
    mgr = _real_handler_manager()
    mgr._claim_session_notice = AsyncMock(return_value=False)
    row = _session_row(auto_stop=False)
    row.max_kwh = 5.5
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for m in range(10, 40):
            await _persist_frame(mgr, row, m, 600.0 if m < 20 else 0.0)
    notify.assert_not_awaited()
    assert mgr._claim_session_notice.await_count == 3


@pytest.mark.asyncio
async def test_claim_db_error_still_sends_the_notice():
    """A DB hiccup on the claim must not lose the notice (pre-0051
    behaviour: at most one repeat)."""
    mgr = _manager()

    def broken():
        raise RuntimeError("db down")

    mgr.db_session_factory = broken
    assert await mgr._claim_session_notice(SID, "full_notified_at") is True


# ===========================================================================
# Tier 2: DB-gated (real Postgres)
# ===========================================================================

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status",
]

_seed_counter = itertools.count(1)


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test (test_payouts.py's fixture)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(factory, gateway_status, session_status=None):
    """Tenant + CPO + admin + gateway (+ plug and a session when asked).
    Returns (gateway_id, session_id or None, cpo_id, admin_id)."""
    from backend.database.models import (
        ChargingSession,
        Gateway,
        Plug,
        PlugStatus,
        Tenant,
        User,
        UserRole,
    )

    n = next(_seed_counter)
    async with factory() as db:
        tenant = Tenant(name=f"T{n}")
        db.add(tenant)
        await db.flush()
        cpo = User(tenant_id=tenant.id, email=f"cpo-{n}@example.com", hashed_password="x",
                   full_name="CPO", role=UserRole.CPO)
        admin = User(email=f"admin-{n}@example.com", hashed_password="x",
                     full_name="Admin", role=UserRole.ADMIN)
        gw = Gateway(id=f"gw-{n}", tenant_id=tenant.id, name="GW",
                     vpn_ip=f"10.9.{n // 250}.{n % 250 + 1}", status=gateway_status)
        db.add_all([cpo, admin, gw])
        session_id = None
        if session_status is not None:
            plug = Plug(gateway_id=gw.id, name="Plug", local_ip="10.0.1.1",
                        status=PlugStatus.OCCUPIED)
            row = ChargingSession(
                tenant_id=tenant.id, user=cpo, plug=plug,
                started_at=datetime.now(timezone.utc), energy_kwh=1.0,
                coins_spent=Decimal("0"), status=session_status,
            )
            db.add_all([plug, row])
            await db.flush()
            session_id = row.id
        await db.commit()
        return gw.id, session_id, cpo.id, admin.id


async def _gateway(factory, gateway_id):
    from backend.database.models import Gateway

    async with factory() as db:
        return await db.get(Gateway, gateway_id)


@requires_db
@pytest.mark.asyncio
async def test_concurrent_claims_have_exactly_one_winner(factory):
    from backend.database.models import ChargingSession, SessionStatus
    from backend.services.mqtt_manager import MQTTManager

    _gid, sid, _cpo, _admin = await _seed(factory, _status("ONLINE"), SessionStatus.ACTIVE)
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=factory)
    results = await asyncio.gather(*[
        mgr._claim_session_notice(sid, "full_notified_at") for _ in range(5)
    ])
    assert sorted(results) == [False] * 4 + [True]
    # A restarted process claiming again: still no.
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=factory)
    assert await mgr._claim_session_notice(sid, "full_notified_at") is False
    # The other notices are independent.
    assert await mgr._claim_session_notice(sid, "nearly_done_notified_at") is True
    async with factory() as db:
        row = await db.get(ChargingSession, sid)
    assert row.full_notified_at is not None and row.power_dropped_notified_at is None
    MQTTManager._instance = None


@requires_db
@pytest.mark.asyncio
async def test_no_claim_for_a_session_that_is_over(factory):
    from backend.database.models import SessionStatus
    from backend.services.mqtt_manager import MQTTManager

    _gid, sid, _cpo, _admin = await _seed(factory, _status("ONLINE"), SessionStatus.COMPLETED)
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=factory)
    assert await mgr._claim_session_notice(sid, "power_dropped_notified_at") is False
    MQTTManager._instance = None


def _status(name):
    from backend.database.models import GatewayStatus

    return GatewayStatus[name]


@requires_db
@pytest.mark.asyncio
async def test_offline_alert_once_across_concurrency_and_restart(factory, monkeypatch):
    from backend.database.models import GatewayStatus

    notify = AsyncMock()
    monkeypatch.setattr(oa, "notify", notify)
    gid, _sid, cpo, admin = await _seed(factory, GatewayStatus.OFFLINE)

    await asyncio.gather(*[oa.alert_gateway_offline(factory, gid) for _ in range(3)])
    assert sorted(c.args[0] for c in notify.await_args_list) == sorted([cpo, admin])
    gw = await _gateway(factory, gid)
    assert gw.offline_notified_at is not None
    assert gw.last_operator_alert_at == gw.offline_notified_at

    oa._reset_state()                                   # backend restart
    await oa.alert_gateway_offline(factory, gid)
    assert notify.await_count == 2                      # not repeated

    async with factory() as db:
        (await db.get(type(gw), gid)).status = GatewayStatus.ONLINE
        await db.commit()
    oa._reset_state()                                   # and again
    await oa.alert_gateway_online(factory, gid)
    await oa.alert_gateway_online(factory, gid)         # retained replay
    online = [c for c in notify.await_args_list if c.args[1] == "gateway_online"]
    assert sorted(c.args[0] for c in online) == sorted([cpo, admin])
    assert (await _gateway(factory, gid)).offline_notified_at is None


@requires_db
@pytest.mark.asyncio
async def test_no_offline_alert_when_online_is_stored_first(factory, monkeypatch):
    from backend.database.models import GatewayStatus

    notify = AsyncMock()
    monkeypatch.setattr(oa, "notify", notify)
    gid, _sid, _cpo, _admin = await _seed(factory, GatewayStatus.ONLINE)
    await oa.alert_gateway_offline(factory, gid)
    await oa.alert_gateway_online(factory, gid)
    notify.assert_not_awaited()
    gw = await _gateway(factory, gid)
    assert gw.offline_notified_at is None and gw.last_operator_alert_at is None


@requires_db
@pytest.mark.asyncio
async def test_offline_alert_leaves_out_an_operator_who_is_charging(factory, monkeypatch):
    from backend.database.models import GatewayStatus, SessionStatus

    notify = AsyncMock()
    monkeypatch.setattr(oa, "notify", notify)
    gid, _sid, cpo, admin = await _seed(factory, GatewayStatus.OFFLINE, SessionStatus.ACTIVE)
    await oa.alert_gateway_offline(factory, gid)
    assert [c.args[0] for c in notify.await_args_list] == [admin]


@requires_db
@pytest.mark.asyncio
async def test_startup_scan_rearms_only_a_pending_recheck(factory, monkeypatch):
    from backend.database.models import Gateway, GatewayStatus

    now = datetime.now(timezone.utc)
    pending, _s, _c, _a = await _seed(factory, GatewayStatus.OFFLINE)
    outstanding, _s, _c, _a = await _seed(factory, GatewayStatus.OFFLINE)
    never, _s, _c, _a = await _seed(factory, GatewayStatus.OFFLINE)
    back, _s, _c, _a = await _seed(factory, GatewayStatus.ONLINE)
    async with factory() as db:
        for gid in (pending, outstanding, back):
            (await db.get(Gateway, gid)).last_operator_alert_at = now - timedelta(hours=1)
        (await db.get(Gateway, outstanding)).offline_notified_at = now - timedelta(hours=1)
        await db.commit()

    armed = []
    monkeypatch.setattr(oa, "_arm_recheck", lambda _f, gid, wait: armed.append((gid, wait)))
    assert await oa.rearm_offline_rechecks(factory) == 1
    assert [gid for gid, _w in armed] == [pending]
    assert 7000 < armed[0][1] <= 7200
    assert never not in [gid for gid, _w in armed]
