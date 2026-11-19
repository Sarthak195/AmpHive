"""
[Idle fee C1] Opt-in idle fee after the car looks fully charged
(services/idle_fee.py, migration 0052_idle_fee).

Tier 1 (DB-free, runs everywhere): terms resolution, the grace boundary, the
cap, the idle clock's transitions (stale telemetry pauses, renewed draw
voids the fee for the session, relay OFF pauses), the auto-stop opt-out, the grace-start notice and
its push policy, the persist-path wiring, and the start hold including the
cap.

Tier 2 (DB-gated, needs TEST_DATABASE_URL, runs in CI): finalize settles the
fee exactly once as its own IDLE_FEE ledger line, even if finalize runs
twice or a line already exists; the fee never exceeds what the hold has left
or what the wallet holds; and a dispute can refund it.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from backend.services import idle_fee
from backend.services.idle_fee import (
    IDLE_GRACE_FLOOR_MIN,
    IDLE_VOID,
    idle_end,
    idle_fee_due,
    idle_total_seconds,
    session_terms,
    tariff_terms,
    track_idle,
)

T0 = datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc)
TERMS = dict(idle_grace_min=15, idle_fee_per_min=Decimal("2.00"), idle_fee_cap=Decimal("30.00"))


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    import backend.services.mqtt_manager as mm

    monkeypatch.setattr(mm, "CHARGE_FULL_IDLE_W", 30.0)
    monkeypatch.setattr(mm, "PLUG_POWER_STALE_SEC", 90)
    yield
    mm.MQTTManager._instance = None


def _row(**kw):
    base = dict(
        auto_stop_when_full=None, full_notified_at=T0, idle_started_at=None,
        idle_seconds=0, idle_grace_min=None, idle_fee_per_min=None, idle_fee_cap=None,
    )
    base.update(TERMS)
    base.update(kw)
    return SimpleNamespace(**base)


def _at(sec):
    return T0 + timedelta(seconds=sec)


def _feed(row, secs, watts=0.0, relay_on=True, prev=None):
    """Feed live frames at the given second offsets; prev chains like
    plugs.last_telemetry_at does."""
    for s in secs:
        track_idle(row, _at(s), watts, relay_on, prev)
        prev = _at(s)
    return prev


# ===========================================================================
# Tier 1: DB-free
# ===========================================================================

def test_no_fee_when_not_configured():
    """Default tariff (all 0) or no tariff: no terms, nothing tracked or due."""
    assert tariff_terms(None) is None
    assert tariff_terms(SimpleNamespace(
        idle_grace_min=0, idle_fee_per_min=Decimal("0.00"), idle_fee_cap=Decimal("0.00"),
    )) is None
    assert tariff_terms(SimpleNamespace(
        idle_grace_min=20, idle_fee_per_min=Decimal("2.00"), idle_fee_cap=Decimal("0.00"),
    )) is None
    row = _row(idle_grace_min=None, idle_fee_per_min=None, idle_fee_cap=None)
    _feed(row, range(0, 3600, 10))
    assert row.idle_started_at is None and row.idle_seconds == 0
    assert idle_fee_due(row, _at(3600)) == Decimal("0.00")


def test_tariff_terms_and_grace_floor():
    t = SimpleNamespace(idle_grace_min=20, idle_fee_per_min=Decimal("2.00"),
                        idle_fee_cap=Decimal("30.00"))
    assert tariff_terms(t) == (20, Decimal("2.00"), Decimal("30.00"))
    t.idle_grace_min = 5
    assert tariff_terms(t)[0] == IDLE_GRACE_FLOOR_MIN == 15


def test_grace_boundary_bills_whole_minutes_after_the_grace():
    row = _row(idle_started_at=T0)
    assert idle_fee_due(row, _at(15 * 60)) == Decimal("0.00")
    assert idle_fee_due(row, _at(15 * 60 + 59)) == Decimal("0.00")
    assert idle_fee_due(row, _at(16 * 60)) == Decimal("2.00")
    assert idle_fee_due(row, _at(20 * 60 + 30)) == Decimal("10.00")


def test_fee_is_capped():
    row = _row(idle_started_at=T0)
    assert idle_fee_due(row, _at(10 * 3600)) == Decimal("30.00")


def test_never_judged_full_never_accrues():
    row = _row(full_notified_at=None)
    _feed(row, range(0, 3600, 10))
    assert row.idle_started_at is None and row.idle_seconds == 0
    assert idle_fee_due(_row(full_notified_at=None, idle_seconds=7200), _at(0)) == Decimal("0.00")


def test_auto_stop_opt_in_never_accrues():
    row = _row(auto_stop_when_full=True)
    assert session_terms(row) is None
    _feed(row, range(0, 3600, 10))
    assert row.idle_started_at is None and row.idle_seconds == 0
    # Even a row that somehow carries idle time owes nothing.
    assert idle_fee_due(_row(auto_stop_when_full=True, idle_seconds=7200), _at(0)) == Decimal("0.00")


def test_idle_stretch_opens_once_and_frames_inside_it_write_nothing():
    row = _row()
    _feed(row, [0])
    assert row.idle_started_at == T0
    _feed(row, range(1, 600), prev=T0)
    assert row.idle_started_at == T0 and row.idle_seconds == 0


def test_stale_telemetry_pauses_accrual():
    """A 30-minute silence (gateway offline) is never billed: the stretch is
    banked up to the last live frame and reopens on the next one."""
    row = _row()
    prev = _feed(row, range(0, 601, 10))           # 10 min idle
    _feed(row, [600 + 1800], prev=prev)            # next frame 30 min later
    assert row.idle_seconds == 600
    assert row.idle_started_at == _at(2400)
    # 5 more minutes of fresh idle: 15 min counted in total, still in grace.
    assert idle_total_seconds(row, _at(2700)) == 900
    assert idle_fee_due(row, _at(2700)) == Decimal("0.00")


def test_finalize_counts_only_up_to_the_last_live_frame():
    """A stop after lost telemetry: the open stretch ends at the plug's last
    live frame, not at the stop."""
    row = _row(idle_started_at=T0)
    assert idle_total_seconds(row, _at(20 * 60)) == 1200
    assert idle_fee_due(row, _at(20 * 60)) == Decimal("10.00")
    assert idle_total_seconds(row, None) == 0


def test_idle_end_is_the_earlier_of_the_plug_and_session_clocks():
    """The plug clock also moves on relay-OFF frames that are not this
    session's; the session clock also moves on offline-resync replays. The
    stretch closes at whichever is earlier."""
    row = _row(idle_started_at=T0, last_telemetry_at=_at(600))
    plug = SimpleNamespace(last_telemetry_at=_at(3000))
    assert idle_end(row, plug) == _at(600)
    row.last_telemetry_at = _at(4000)
    assert idle_end(row, plug) == _at(3000)
    row.last_telemetry_at = None
    assert idle_end(row, SimpleNamespace(last_telemetry_at=None)) is None


def test_relay_off_pauses_and_the_next_idle_frame_resumes():
    row = _row()
    prev = _feed(row, range(0, 311, 10))                # idle from 0
    prev = _feed(row, [320], relay_on=False, prev=prev)
    assert row.idle_started_at is None and row.idle_seconds == 320
    _feed(row, [330], prev=prev)
    assert row.idle_started_at == _at(330) and row.idle_seconds == 320


def test_renewed_draw_voids_the_fee_for_the_session():
    """The review reproduction: idle 0..300 s, the car draws 1500 W at 310 s
    (it was not done), then 40 min of 0 W. Idle was not continuous since the
    one "looks full" notice, so nothing is billed, ever, and the later low
    frames write nothing."""
    row = _row()
    prev = _feed(row, range(0, 301, 10))
    prev = _feed(row, [310], watts=1500.0, prev=prev)
    assert row.idle_started_at is None and row.idle_seconds == IDLE_VOID
    prev = _feed(row, range(320, 320 + 40 * 60, 10), prev=prev)
    _feed(row, [320 + 40 * 60], relay_on=False, prev=prev)
    assert row.idle_started_at is None and row.idle_seconds == IDLE_VOID
    assert idle_total_seconds(row, _at(3000)) == 0
    assert idle_fee_due(row, _at(3000)) == Decimal("0.00")
    # Even a row that somehow kept an open stretch owes nothing once voided.
    assert idle_fee_due(_row(idle_started_at=T0, idle_seconds=IDLE_VOID),
                        _at(3600)) == Decimal("0.00")


def test_renewed_draw_before_any_idle_frame_also_voids():
    """The car draws again right after the notice, before a stretch opened:
    still voided, so a later idle stretch is not billed without a notice."""
    row = _row()
    prev = _feed(row, [0], watts=1500.0)
    assert row.idle_seconds == IDLE_VOID
    _feed(row, range(10, 3600, 10), prev=prev)
    assert row.idle_started_at is None
    assert idle_fee_due(row, _at(3600)) == Decimal("0.00")


@pytest.mark.asyncio
async def test_grace_start_sends_one_idle_notice_that_always_pushes():
    from backend.services.mqtt_manager import MQTTManager
    from backend.services.notification_prefs import always_pushed

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr.telemetry_store = None
    try:
        with patch("backend.services.notifications.notify", AsyncMock()) as notify:
            await mgr._on_charge_full(42, 3, 5, False, send_notice=True,
                                      idle_terms=(15, Decimal("2.00"), Decimal("30.00")))
        assert notify.await_count == 1
        args = notify.await_args.args
        assert args[:3] == (3, "idle_fee_notice", "Charging done, please stop and unplug")
        assert "2.00 coins per minute" in args[3] and "15 minutes" in args[3]
        assert "30.00 coins" in args[3]
        assert always_pushed("idle_fee_notice", "info")
    finally:
        MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_path_opens_the_idle_stretch_on_a_live_idle_frame():
    """Wiring: the locked telemetry block feeds track_idle on live frames."""
    from backend.tests.test_charge_full import _manager, _persist_frame, _session_row

    mgr = _manager()
    row = _session_row(auto_stop=None)
    row.full_notified_at = T0
    row.idle_started_at = None
    row.idle_seconds = 0
    row.idle_grace_min, row.idle_fee_per_min, row.idle_fee_cap = (
        15, Decimal("2.00"), Decimal("30.00"),
    )
    with patch("backend.services.notifications.notify", AsyncMock()):
        await _persist_frame(mgr, row, 40, 0.0)
        assert row.idle_started_at is not None
        await _persist_frame(mgr, row, 41, 1500.0)
    assert row.idle_started_at is None and row.idle_seconds == IDLE_VOID


async def _begin(terms, *, max_kwh, auto_stop=False):
    from backend.services import session_start

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    user = MagicMock(id=3, coin_balance=Decimal("500"))
    plug = MagicMock(id=5, gateway_id="gw-1", local_ip="10.0.0.2")
    mqtt = MagicMock()
    mqtt.send_plug_command.return_value = True
    resolver = AsyncMock(return_value=terms)
    with patch.object(session_start, "check_circuit_admission", AsyncMock()), \
         patch.object(session_start, "resolve_rate_window",
                      AsyncMock(return_value=(Decimal("5.00"), None))), \
         patch.object(session_start, "available_balance", AsyncMock(return_value=Decimal("500"))), \
         patch.object(session_start, "max_rate_over_window",
                      AsyncMock(return_value=Decimal("5.00"))), \
         patch.object(session_start, "resolve_idle_terms", resolver), \
         patch.object(session_start, "set_plug_telemetry_interval", AsyncMock()), \
         patch.object(session_start.state, "mqtt_manager", mqtt), \
         patch.object(session_start.state, "telemetry_store", MagicMock()), \
         patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()), \
         patch("backend.services.caps.effective_plug_cap", return_value=16.0), \
         patch("backend.services.notifications.notify", AsyncMock()):
        session = await session_start.begin_active_session(
            db, user, plug, MagicMock(tenant_id=1), max_kwh=max_kwh,
            max_duration_seconds=None, auto_stop_when_full=auto_stop,
        )
    return session, resolver


@pytest.mark.asyncio
async def test_hold_includes_the_idle_fee_cap_and_terms_are_snapshotted():
    terms = (15, Decimal("2.00"), Decimal("30.00"))
    session, _ = await _begin(terms, max_kwh=10.0)
    assert session.hold_coins == Decimal("80.00")       # 10 kWh * 5 + 30 cap
    assert (session.idle_grace_min, session.idle_fee_per_min, session.idle_fee_cap) == terms

    session, _ = await _begin(None, max_kwh=10.0)
    assert session.hold_coins == Decimal("50.00")
    assert session.idle_fee_per_min is None

    session, _ = await _begin(terms, max_kwh=None)
    assert session.hold_coins == Decimal("500.00")      # unlimited: the available balance


@pytest.mark.asyncio
async def test_auto_stop_session_reserves_and_snapshots_no_idle_fee():
    session, resolver = await _begin((15, Decimal("2.00"), Decimal("30.00")),
                                     max_kwh=10.0, auto_stop=True)
    resolver.assert_not_awaited()
    assert session.hold_coins == Decimal("50.00")
    assert session.idle_fee_per_min is None


async def _patch_max_kwh(monkeypatch, *, auto_stop=None, available="500.00"):
    """The real update_session_limits on a mocked db: a session at 5.00/kWh
    with idle terms of 15 min, 2.00/min and a 30.00 cap, started with
    max_kwh 4 and hold 50, PATCHed to max_kwh 6."""
    import backend.routers.sessions as sessions_mod
    from backend.database.models import SessionStatus
    from backend.schemas import SessionLimitsUpdateRequest

    session = SimpleNamespace(
        id=11, user_id=3, plug_id=5, tenant_id=1, status=SessionStatus.ACTIVE,
        energy_kwh=1.0, max_kwh=4.0, max_duration_seconds=None,
        hold_coins=Decimal("50.00"), auto_stop_when_full=auto_stop, **TERMS,
    )
    plug = SimpleNamespace(id=5, gateway_id="gw-1", local_ip="10.0.0.2")

    def _res(**kw):
        r = MagicMock()
        for k, v in kw.items():
            getattr(r, k).return_value = v
        return r

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        _res(scalar_one_or_none=SimpleNamespace(id=3)),
        _res(scalar_one_or_none=session),
        _res(scalar_one=plug),
    ])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    monkeypatch.setattr(sessions_mod, "max_rate_over_window",
                        AsyncMock(return_value=Decimal("5.00")))
    monkeypatch.setattr(sessions_mod, "available_balance",
                        AsyncMock(return_value=Decimal(available)))
    monkeypatch.setattr(sessions_mod, "session_cost", lambda _s, _kwh: Decimal("5.00"))
    monkeypatch.setattr(sessions_mod.state, "mqtt_manager", None)
    resp = await sessions_mod.update_session_limits(
        11, SessionLimitsUpdateRequest(max_kwh=6.0), SimpleNamespace(id=3), db,
    )
    return session, resp


@pytest.mark.asyncio
async def test_limits_edit_keeps_the_idle_cap_in_the_resized_hold(monkeypatch):
    """hold = min(headroom, energy_cost(max_kwh, max_rate) + cap), as at start:
    6 kWh x 5.00 + 30 = 60. Without the cap term it would be 30 and finalize
    could collect no idle fee."""
    session, resp = await _patch_max_kwh(monkeypatch)
    assert session.hold_coins == Decimal("60.00")
    assert resp["hold_coins"] == 60.0


@pytest.mark.asyncio
async def test_limits_edit_hold_with_the_cap_is_still_bounded_by_headroom(monkeypatch):
    session, _ = await _patch_max_kwh(monkeypatch, available="5.00")   # headroom 55
    assert session.hold_coins == Decimal("55.00")


@pytest.mark.asyncio
async def test_limits_edit_reserves_no_cap_for_an_auto_stop_session(monkeypatch):
    session, _ = await _patch_max_kwh(monkeypatch, auto_stop=True)
    assert session.hold_coins == Decimal("30.00")


@pytest.mark.asyncio
async def test_finalize_settles_once_capped_by_the_hold_left_db_free(monkeypatch):
    """The real finalize with mocked rows: the energy debit, then the idle fee
    clamped to what the hold has left (14 - 10 = 4 of a 30 due), as its own
    IDLE_FEE line, folded into coins_spent; a second finalize settles nothing."""
    from datetime import datetime as dt

    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module
    from backend.database.models import LedgerTransaction, SessionStatus, TransactionType
    from backend.tests.test_plug_watch import _db, _plug, _scalar_one, _scalar_one_or_none, _user

    now = dt.now(timezone.utc)
    session = MagicMock()
    session.id, session.status, session.user_id, session.plug_id = 99, SessionStatus.ACTIVE, 3, 7
    session.hold_coins = Decimal("14.00")
    session.energy_kwh = 2.0
    session.rate_coins_per_kwh = Decimal("5.00")
    session.settled_cost_coins = None
    session.rate_segment_start_kwh = None
    session.peak_power_w = 0.0
    session.started_at = now - timedelta(hours=2)
    session.auto_stop_when_full = None
    session.full_notified_at = now - timedelta(minutes=50)
    session.idle_started_at = now - timedelta(minutes=40)
    session.idle_seconds = 0
    session.last_telemetry_at = now
    session.idle_grace_min, session.idle_fee_per_min, session.idle_fee_cap = (
        15, Decimal("2.00"), Decimal("30.00"),
    )
    plug = _plug(plug_id=7, name="Bay 1")
    plug.last_telemetry_at = now

    no_line = MagicMock()
    no_line.first.return_value = None
    debit = AsyncMock(side_effect=[(Decimal("10.00"), Decimal("190.00")),
                                   (Decimal("4.00"), Decimal("186.00"))])
    monkeypatch.setattr(state_module, "mqtt_manager",
                        MagicMock(send_plug_command=MagicMock(return_value=True)))
    monkeypatch.setattr(state_module, "telemetry_store",
                        MagicMock(get_latest=MagicMock(return_value=None), end_session=MagicMock()))
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    monkeypatch.setattr(sl_mod, "debit_wallet_clamped", debit)
    monkeypatch.setattr("backend.services.billing_emails.schedule", MagicMock())
    db = _db(_scalar_one_or_none(_user(3)), _scalar_one_or_none(session), _scalar_one(plug), no_line)
    with patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()),          patch("backend.services.notifications.notify", AsyncMock()) as notify,          patch("backend.services.plug_watch.notify_watchers_plug_available", AsyncMock()),          patch("backend.services.capacity.notify_capacity_available", AsyncMock()):
        receipt = await sl_mod.finalize_charging_session(db, 99)

        assert debit.await_args_list[1].args[2] == Decimal("4.00")
        lines = [c.args[0] for c in db.add.call_args_list
                 if isinstance(c.args[0], LedgerTransaction)]
        assert [(ln.transaction_type, ln.amount) for ln in lines] == [
            (TransactionType.SESSION_DEBIT, Decimal("-10.00")),
            (TransactionType.IDLE_FEE, Decimal("-4.00")),
        ]
        assert lines[1].balance_after == Decimal("186.00")
        assert session.coins_spent == Decimal("14.00")
        assert session.idle_fee_coins == Decimal("4.00")
        assert receipt["coins_spent"] == 14.0 and receipt["idle_fee_coins"] == 4.0
        assert receipt["balance_before"] == 200.0 and receipt["balance_remaining"] == 186.0
        assert "Includes an idle fee of 4.00 coins." in notify.await_args.args[3]

        # Finalize again (double stop, reaper race): the row is no longer ACTIVE.
        db2 = _db(_scalar_one_or_none(_user(3)), _scalar_one_or_none(session))
        assert await sl_mod.finalize_charging_session(db2, 99) is None
    assert debit.await_count == 2
    db2.add.assert_not_called()


@pytest.mark.asyncio
async def test_terms_lookup_failure_means_no_fee_not_a_failed_start():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    plug = SimpleNamespace(tariff_id=1, group_id=None, gateway_id="gw-1")
    assert await idle_fee.resolve_idle_terms(db, plug) is None


def test_tariff_route_validation():
    from fastapi import HTTPException

    from backend.routers.cpo._tariffs import _apply_idle_terms

    t = SimpleNamespace(idle_grace_min=0, idle_fee_per_min=Decimal("0.00"),
                        idle_fee_cap=Decimal("0.00"))
    _apply_idle_terms(t, None, 2.0, 30.0)                # grace defaults to the floor
    assert t.idle_grace_min == 15 and t.idle_fee_per_min == Decimal("2.00")
    with pytest.raises(HTTPException):
        _apply_idle_terms(t, 10, None, None)             # below the floor: refused
    t2 = SimpleNamespace(idle_grace_min=0, idle_fee_per_min=Decimal("0.00"),
                         idle_fee_cap=Decimal("0.00"))
    with pytest.raises(HTTPException):
        _apply_idle_terms(t2, 20, 2.0, None)             # enabled with no cap: refused
    t3 = SimpleNamespace(idle_grace_min=0, idle_fee_per_min=Decimal("0.00"),
                         idle_fee_cap=Decimal("0.00"))
    _apply_idle_terms(t3, 5, 0.0, None)                  # fee off: any grace is fine
    assert t3.idle_grace_min == 5


# ===========================================================================
# Tier 2: DB-gated (CI's Postgres)
# ===========================================================================

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)
ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "dispute_status",
]


@pytest_asyncio.fixture()
async def factory():
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


async def _seed(factory, *, balance="200.00", hold="100.00", idle_minutes=40,
                auto_stop=None) -> dict:
    """A tenant, CPO, driver, plug and an ACTIVE session that used 2 kWh at
    5.00 (10 coins) and has been idle `idle_minutes` since it looked full,
    with the plug's last live frame at the end of that stretch."""
    from backend.database.models import (
        ChargingSession,
        Gateway,
        Plug,
        PlugStatus,
        SessionStatus,
        Tenant,
        User,
        UserRole,
    )

    tag = uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    async with factory() as db:
        tenant = Tenant(name=f"t-{tag}")
        db.add(tenant)
        await db.flush()
        cpo = User(email=f"cpo-{tag}@amphive.test", hashed_password="x", full_name="CPO",
                   role=UserRole.CPO, tenant_id=tenant.id)
        driver = User(email=f"drv-{tag}@amphive.test", hashed_password="x", full_name="Driver",
                      role=UserRole.DRIVER, coin_balance=Decimal(balance))
        db.add_all([cpo, driver])
        await db.flush()
        gw = Gateway(id=f"gw-{tag}", tenant_id=tenant.id, name="GW", vpn_ip=f"vpn-{tag}")
        db.add(gw)
        await db.flush()
        plug = Plug(gateway_id=gw.id, name="Bay 1", local_ip="10.0.0.5",
                    status=PlugStatus.OCCUPIED, last_telemetry_at=now)
        db.add(plug)
        await db.flush()
        session = ChargingSession(
            tenant_id=tenant.id, user_id=driver.id, plug_id=plug.id,
            status=SessionStatus.ACTIVE, hold_coins=Decimal(hold), energy_kwh=2.0,
            rate_coins_per_kwh=Decimal("5.00"), auto_stop_when_full=auto_stop,
            full_notified_at=now - timedelta(minutes=idle_minutes + 10),
            idle_started_at=now - timedelta(minutes=idle_minutes), idle_seconds=0,
            **TERMS,
        )
        db.add(session)
        await db.commit()
        return {"tenant_id": tenant.id, "cpo_id": cpo.id, "driver_id": driver.id,
                "plug_id": plug.id, "session_id": session.id}


@pytest.fixture()
def _no_hardware(monkeypatch):
    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module

    monkeypatch.setattr(state_module, "mqtt_manager",
                        MagicMock(send_plug_command=MagicMock(return_value=True)))
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    monkeypatch.setattr("backend.services.notifications.notify", AsyncMock())
    monkeypatch.setattr("backend.services.billing_emails.schedule", MagicMock())
    return sl_mod


async def _idle_lines(factory, session_id):
    from sqlalchemy import select

    from backend.database.models import LedgerTransaction, TransactionType

    async with factory() as db:
        return (await db.execute(
            select(LedgerTransaction).where(
                LedgerTransaction.session_id == session_id,
                LedgerTransaction.transaction_type == TransactionType.IDLE_FEE,
            )
        )).scalars().all()


async def _balance(factory, user_id):
    from sqlalchemy import select

    from backend.database.models import User

    async with factory() as db:
        return (await db.execute(select(User.coin_balance).where(User.id == user_id))).scalar_one()


@requires_db
@pytest.mark.asyncio
async def test_finalize_settles_the_capped_fee_exactly_once(factory, _no_hardware):
    w = await _seed(factory, idle_minutes=40)   # 25 min past grace = 50, capped at 30
    async with factory() as db:
        out = await _no_hardware.finalize_charging_session(db, w["session_id"])
    assert out["coins_spent"] == 40.0 and out["idle_fee_coins"] == 30.0
    assert out["balance_remaining"] == 160.0 and out["balance_before"] == 200.0
    async with factory() as db:
        assert await _no_hardware.finalize_charging_session(db, w["session_id"]) is None
    lines = await _idle_lines(factory, w["session_id"])
    assert len(lines) == 1
    assert lines[0].amount == Decimal("-30.00") and lines[0].balance_after == Decimal("160.00")
    assert await _balance(factory, w["driver_id"]) == Decimal("160.00")


@requires_db
@pytest.mark.asyncio
async def test_existing_idle_line_is_never_written_twice(factory, _no_hardware):
    from backend.database.models import LedgerTransaction, TransactionType

    w = await _seed(factory, idle_minutes=40)
    async with factory() as db:
        db.add(LedgerTransaction(user_id=w["driver_id"], session_id=w["session_id"],
                                 amount=Decimal("-30.00"), transaction_type=TransactionType.IDLE_FEE,
                                 description="earlier settle", balance_after=Decimal("170.00")))
        await db.commit()
    async with factory() as db:
        out = await _no_hardware.finalize_charging_session(db, w["session_id"])
    assert out["idle_fee_coins"] == 0.0
    assert len(await _idle_lines(factory, w["session_id"])) == 1


@requires_db
@pytest.mark.asyncio
async def test_fee_never_exceeds_what_the_hold_has_left(factory, _no_hardware):
    w = await _seed(factory, hold="14.00", idle_minutes=40)   # 10 energy, 4 left
    async with factory() as db:
        out = await _no_hardware.finalize_charging_session(db, w["session_id"])
    assert out["idle_fee_coins"] == 4.0 and out["coins_spent"] == 14.0


@requires_db
@pytest.mark.asyncio
async def test_zero_balance_driver_is_charged_nothing(factory, _no_hardware):
    w = await _seed(factory, balance="0.00", idle_minutes=40)
    async with factory() as db:
        out = await _no_hardware.finalize_charging_session(db, w["session_id"])
    assert out["coins_spent"] == 0.0 and out["idle_fee_coins"] == 0.0
    assert await _idle_lines(factory, w["session_id"]) == []
    assert await _balance(factory, w["driver_id"]) == Decimal("0.00")


@requires_db
@pytest.mark.asyncio
async def test_inside_grace_or_auto_stop_writes_no_idle_line(factory, _no_hardware):
    w = await _seed(factory, idle_minutes=14)
    async with factory() as db:
        out = await _no_hardware.finalize_charging_session(db, w["session_id"])
    assert out["coins_spent"] == 10.0 and out["idle_fee_coins"] == 0.0
    assert await _idle_lines(factory, w["session_id"]) == []

    w = await _seed(factory, idle_minutes=40, auto_stop=True)
    async with factory() as db:
        out = await _no_hardware.finalize_charging_session(db, w["session_id"])
    assert out["coins_spent"] == 10.0 and out["idle_fee_coins"] == 0.0
    assert await _idle_lines(factory, w["session_id"]) == []


@requires_db
@pytest.mark.asyncio
async def test_dispute_can_refund_the_idle_fee(factory, _no_hardware):
    from backend.database.models import DisputeStatus, SessionDispute
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    w = await _seed(factory, idle_minutes=40)
    async with factory() as db:
        await _no_hardware.finalize_charging_session(db, w["session_id"])
    async with factory() as db:
        dispute = SessionDispute(session_id=w["session_id"], tenant_id=w["tenant_id"],
                                 driver_user_id=w["driver_id"],
                                 reason="I unplugged on time, the idle fee is wrong.",
                                 status=DisputeStatus.OPEN)
        db.add(dispute)
        await db.commit()
        dispute_id = dispute.id
    cpo = SimpleNamespace(id=w["cpo_id"], email="cpo@amphive.test", tenant_id=w["tenant_id"])
    async with factory() as db:
        await cpo_resolve_dispute(dispute_id, CpoDisputeResolveRequest(action="approve"), cpo, db)
    # The default refund is the whole coins_spent, idle fee included.
    assert await _balance(factory, w["driver_id"]) == Decimal("200.00")
