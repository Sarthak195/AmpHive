"""
Operator alerts (services/operator_alerts.py): gateway offline while idle and
safety faults reach the tenant's CPOs (+ admins for offline) via notify(),
with cooldowns, a kill switch and an optional email. DB-free: the gateway row
lock, the recipient lookups, notify() and send_email() are faked; no mail is
sent. The offline state lives on a fake gateway row that survives
oa._reset_state(), which is how a backend restart is simulated. The real SQL
(row lock, claim races, the startup scan) is DB-gated in
test_durable_notice_state.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.database.models import GatewayStatus
from backend.services import operator_alerts as oa
from backend.services.mqtt_manager import MQTTManager

T0 = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    oa._reset_state()
    for name in ("OPERATOR_ALERTS", "OPERATOR_ALERT_EMAIL",
                 "GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC", "OPERATOR_ALERT_COOLDOWN_SEC"):
        monkeypatch.delenv(name, raising=False)
    yield
    oa._reset_state()


class _GwDB:
    """Fake session factory over durable gateway rows (they outlive
    oa._reset_state, like the real columns outlive a restart)."""

    def __init__(self):
        self.rows = {}
        self.commits = 0

    def gw(self, gateway_id, status=GatewayStatus.OFFLINE):
        row = self.rows.get(gateway_id)
        if row is None:
            row = self.rows[gateway_id] = SimpleNamespace(
                status=status, tenant_id=1, offline_notified_at=None, last_operator_alert_at=None,
            )
        row.status = status
        return row

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.commits += 1


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(oa, "_now", lambda: now[0])
    return now


@pytest.fixture
def db(monkeypatch):
    fake = _GwDB()

    async def locked(_session, gateway_id):
        return fake.rows.get(gateway_id)

    monkeypatch.setattr(oa, "_locked_gateway", locked)
    return fake


@pytest.fixture
def mocks(monkeypatch, db):
    notify_mock = AsyncMock()
    email_mock = MagicMock()
    recip = AsyncMock(return_value=[7, 8])
    monkeypatch.setattr(oa, "notify", notify_mock)
    monkeypatch.setattr(oa, "send_email", email_mock)
    monkeypatch.setattr(oa, "_recipients", recip)
    monkeypatch.setattr(oa, "_recipient_ids", AsyncMock(return_value=[7, 8]))
    return notify_mock, email_mock, recip


def _titles(notify_mock):
    return [c.args[2] for c in notify_mock.await_args_list]


async def _drain():
    if oa._pending:
        await asyncio.gather(*list(oa._pending))


@pytest.mark.asyncio
async def test_offline_alert_notifies_each_recipient_with_cooldown(mocks, db, clock):
    notify_mock, email_mock, _ = mocks
    db.gw("gw-1")
    db.gw("gw-2")

    await oa.alert_gateway_offline(db, "gw-1")
    assert [c.args[0] for c in notify_mock.await_args_list] == [7, 8]
    assert notify_mock.await_args_list[0].args[1] == "gateway_offline"
    kw = oa._recipient_ids.await_args.kwargs
    assert kw["include_admins"] is True and kw["exclude_active_drivers"] is True
    assert db.rows["gw-1"].offline_notified_at == T0
    assert db.rows["gw-1"].last_operator_alert_at == T0

    # Flap inside the 3 h default cooldown: nothing more.
    db.rows["gw-1"].offline_notified_at = None      # answered by a back-online
    clock[0] = T0 + timedelta(hours=1)
    await oa.alert_gateway_offline(db, "gw-1")
    assert notify_mock.await_count == 2
    # Another gateway is independent.
    await oa.alert_gateway_offline(db, "gw-2")
    assert notify_mock.await_count == 4
    # After the cooldown it alerts again.
    oa._reset_state()                               # (drop the armed re-check)
    clock[0] = T0 + timedelta(hours=4)
    await oa.alert_gateway_offline(db, "gw-1")
    assert notify_mock.await_count == 6
    email_mock.assert_not_called()  # OPERATOR_ALERT_EMAIL unset


@pytest.mark.asyncio
async def test_online_followup_only_after_an_offline_alert_was_sent(mocks, db, clock):
    notify_mock, _, _ = mocks
    db.gw("gw-1", GatewayStatus.ONLINE)
    await oa.alert_gateway_online(db, "gw-1")  # nothing sent before
    notify_mock.assert_not_awaited()

    db.gw("gw-1", GatewayStatus.OFFLINE)
    await oa.alert_gateway_offline(db, "gw-1")
    notify_mock.reset_mock()
    db.gw("gw-1", GatewayStatus.ONLINE)
    await oa.alert_gateway_online(db, "gw-1")
    assert [c.args[1] for c in notify_mock.await_args_list] == ["gateway_online"] * 2
    assert db.rows["gw-1"].offline_notified_at is None
    notify_mock.reset_mock()
    await oa.alert_gateway_online(db, "gw-1")  # once per offline alert
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_offline_and_back_online_survive_a_restart(mocks, db, clock):
    """F3 known limit closed: the alert state is on the gateway row, so a
    backend restart (fresh process memory) neither re-alerts inside the
    cooldown nor forgets the back-online follow-up."""
    notify_mock, _, _ = mocks
    db.gw("gw-1")
    await oa.alert_gateway_offline(db, "gw-1")
    assert _titles(notify_mock) == ["Gateway offline"] * 2

    oa._reset_state()                               # restart
    clock[0] = T0 + timedelta(minutes=30)
    await oa.alert_gateway_offline(db, "gw-1")      # retained replay + debounce
    assert notify_mock.await_count == 2             # no repeat

    oa._reset_state()                               # restart again
    db.gw("gw-1", GatewayStatus.ONLINE)
    await oa.alert_gateway_online(db, "gw-1")
    assert _titles(notify_mock)[-2:] == ["Gateway back online"] * 2


@pytest.mark.asyncio
async def test_no_offline_alert_once_an_online_committed_first(mocks, db, clock):
    """Decided under the row lock: an `online` stored before the offline
    decision means neither the "offline" nor its "back online" goes out."""
    notify_mock, _, _ = mocks
    db.gw("gw-1", GatewayStatus.ONLINE)
    await oa.alert_gateway_offline(db, "gw-1")
    await oa.alert_gateway_online(db, "gw-1")
    notify_mock.assert_not_awaited()
    assert db.rows["gw-1"].offline_notified_at is None
    assert db.rows["gw-1"].last_operator_alert_at is None


@pytest.mark.asyncio
async def test_back_online_never_overtakes_the_offline_fan_out(mocks, db, clock):
    """F3 ordering race closed: an `online` landing while the offline fan-out
    is still awaiting notify() waits for it, so every "Gateway offline" is
    delivered before any "Gateway back online"."""
    notify_mock, _, _ = mocks
    db.gw("gw-1")
    gate = asyncio.Event()
    entered = asyncio.Event()
    order = []

    async def slow_notify(uid, kind, *_a, **_k):
        if kind == "gateway_offline" and uid == 7:
            entered.set()
            await gate.wait()
        order.append((kind, uid))

    notify_mock.side_effect = slow_notify
    offline = asyncio.ensure_future(oa.alert_gateway_offline(db, "gw-1"))
    await entered.wait()                    # offline claimed, mid fan-out
    db.gw("gw-1", GatewayStatus.ONLINE)     # the status handler stored ONLINE
    online = asyncio.ensure_future(oa.alert_gateway_online(db, "gw-1"))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(offline, online)
    assert order == [("gateway_offline", 7), ("gateway_offline", 8),
                     ("gateway_online", 7), ("gateway_online", 8)]


@pytest.mark.asyncio
async def test_kill_switch_silences_everything(mocks, db, monkeypatch):
    notify_mock, email_mock, _ = mocks
    monkeypatch.setenv("OPERATOR_ALERTS", "off")
    monkeypatch.setenv("OPERATOR_ALERT_EMAIL", "me@example.test")
    db.gw("gw-1")
    await oa.alert_gateway_offline(db, "gw-1")
    await oa.alert_safety_fault(MagicMock(), "gw-1", 1, "THERMAL_CUTOFF", "critical", None)
    await _drain()
    notify_mock.assert_not_awaited()
    email_mock.assert_not_called()


@pytest.mark.asyncio
async def test_kill_switch_still_clears_an_outstanding_offline_alert(mocks, db, monkeypatch):
    notify_mock, _, _ = mocks
    row = db.gw("gw-1", GatewayStatus.ONLINE)
    row.offline_notified_at = T0
    monkeypatch.setenv("OPERATOR_ALERTS", "off")
    await oa.alert_gateway_online(db, "gw-1")
    notify_mock.assert_not_awaited()
    assert row.offline_notified_at is None


@pytest.mark.asyncio
async def test_safety_fault_dedup_per_gateway_plug_event(mocks):
    notify_mock, _, recip = mocks
    f = oa.alert_safety_fault
    await f(MagicMock(), "gw-1", 1, "THERMAL_CUTOFF", "critical", "hot")
    assert notify_mock.await_count == 2
    assert notify_mock.await_args_list[0].kwargs["plug_id"] == 1
    assert recip.await_args.kwargs["include_admins"] is False
    await f(MagicMock(), "gw-1", 1, "THERMAL_CUTOFF", "critical", "hot")  # dup
    assert notify_mock.await_count == 2
    await f(MagicMock(), "gw-1", 2, "THERMAL_CUTOFF", "critical", "hot")  # other plug
    await f(MagicMock(), "gw-1", 1, "OVERCURRENT_CUTOFF", "critical", "amps")  # other event
    assert notify_mock.await_count == 6


@pytest.mark.asyncio
async def test_safety_fault_skips_the_driver_who_got_the_cutoff_notice(mocks, monkeypatch):
    """An owner charging from an operator account gets one notice per fault:
    the driver cutoff notice, not also the gateway_fault bell. The email and
    every other recipient are unchanged."""
    notify_mock, email_mock, _ = mocks
    monkeypatch.setenv("OPERATOR_ALERT_EMAIL", "owner@example.test")
    await oa.alert_safety_fault(
        MagicMock(), "gw-1", 1, "THERMAL_CUTOFF", "critical", "hot", exclude_user_ids=(7,),
    )
    await _drain()
    assert [c.args[0] for c in notify_mock.await_args_list] == [8]
    email_mock.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["OVERCURRENT_CAP", "UNMETERED_CONSUMPTION",
                                   "GATEWAY_BOOT", "OTA_STARTED", "LOCAL_LIMIT_CUTOFF"])
async def test_non_fault_events_do_not_alert(mocks, event):
    notify_mock, _, _ = mocks
    await oa.alert_safety_fault(MagicMock(), "gw-1", 1, event, "warning", None)
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_sent_only_when_configured(mocks, monkeypatch):
    _, email_mock, _ = mocks
    monkeypatch.setenv("OPERATOR_ALERT_EMAIL", "owner@example.test")
    await oa.alert_safety_fault(MagicMock(), "gw-1", 3, "OTA_FAILED", "warning", "boom")
    await _drain()
    email_mock.assert_called_once()
    to_addr, subject, body = email_mock.call_args.args
    assert to_addr == "owner@example.test"
    assert "Firmware update failed" in subject and "gw-1" in body


@pytest.mark.asyncio
async def test_notify_failure_never_raises(mocks, db):
    notify_mock, _, _ = mocks
    notify_mock.side_effect = RuntimeError("db down")
    db.gw("gw-1")
    await oa.alert_gateway_offline(db, "gw-1")  # swallowed and logged
    await oa.alert_safety_fault(MagicMock(), "gw-1", 1, "THERMAL_CUTOFF", "critical", None)


@pytest.mark.asyncio
async def test_db_failure_never_raises(mocks, monkeypatch):
    notify_mock, _, _ = mocks
    monkeypatch.setattr(oa, "_locked_gateway", AsyncMock(side_effect=RuntimeError("db down")))
    await oa.alert_gateway_offline(_GwDB(), "gw-1")
    await oa.alert_gateway_online(_GwDB(), "gw-1")
    notify_mock.assert_not_awaited()


# ------------------------------------------------------------------ wiring --

class _Session:
    """Async-context session whose every query returns `result`."""

    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        return self._result

    def add(self, row):
        row.id = 5
        row.created_at = None

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_deferred_offline_notice_alerts_operators():
    from backend.database.models import GatewayStatus

    MQTTManager._instance = None
    gw = MagicMock()
    gw.status = GatewayStatus.OFFLINE
    result = MagicMock()
    result.scalar_one_or_none.return_value = gw

    mgr = MQTTManager(db_session_factory=lambda: _Session(result))
    mgr._notify_drivers_gateway_offline = AsyncMock()
    mgr._broadcast_plug_connectivity = AsyncMock()
    with patch.object(oa, "alert_gateway_offline", AsyncMock()) as alert:
        await mgr._deferred_offline_notice("gw-1", 0)
        alert.assert_awaited_once_with(mgr.db_session_factory, "gw-1")
        # Gateway back before the notice came due: no alert.
        alert.reset_mock()
        gw.status = GatewayStatus.ONLINE
        await mgr._deferred_offline_notice("gw-1", 0)
        alert.assert_not_awaited()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_gateway_event_alerts_operators():
    MQTTManager._instance = None
    gw = MagicMock()
    gw.tenant_id = 1
    result = MagicMock()
    result.scalar_one_or_none.return_value = gw

    mgr = MQTTManager(db_session_factory=lambda: _Session(result))
    with patch.object(oa, "alert_safety_fault", AsyncMock()) as alert, \
         patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-1", None, "UNAUTHORIZED_ON", "critical", "x")
    alert.assert_awaited_once_with(
        mgr.db_session_factory, "gw-1", None, "UNAUTHORIZED_ON", "critical", "x",
        exclude_user_ids=(),
    )
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_cutoff_driver_is_left_out_of_the_fault_bell():
    """A cutoff that finalized a session passes that session's driver as the
    exclusion; a cutoff that finalized nothing excludes nobody."""
    MQTTManager._instance = None
    gw = MagicMock()
    gw.tenant_id = 1
    gw.gateway_id = "gw-1"
    result = MagicMock()
    result.scalar_one_or_none.return_value = gw
    mgr = MQTTManager(db_session_factory=lambda: _Session(result))
    mgr._auto_enter_maintenance = AsyncMock()
    for finalized_driver, excluded in ((3, (3,)), (None, ())):
        mgr._finalize_session_after_cutoff = AsyncMock(return_value=finalized_driver)
        with patch.object(oa, "alert_safety_fault", AsyncMock()) as alert,              patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
            await mgr._persist_gateway_event("gw-1", 2, "THERMAL_CUTOFF", "critical", "hot")
        assert alert.await_args.kwargs["exclude_user_ids"] == excluded
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_finalize_after_cutoff_returns_the_driver_only_when_it_finalized():
    MQTTManager._instance = None
    result = MagicMock()
    result.first.return_value = (42, 3)
    mgr = MQTTManager(db_session_factory=lambda: _Session(result))
    for outcome, expected in (({"status": "completed"}, 3), (None, None)):
        with patch("backend.services.session_lifecycle.finalize_charging_session",
                   AsyncMock(return_value=outcome)):
            assert await mgr._finalize_session_after_cutoff(2, "THERMAL_CUTOFF") == expected
    result.first.return_value = None      # no ACTIVE session on the plug
    assert await mgr._finalize_session_after_cutoff(2, "THERMAL_CUTOFF") is None
    MQTTManager._instance = None


# ---------------------------------------------- cooldown re-check (fix round) --

@pytest.mark.asyncio
async def test_short_outage_then_long_outage_is_reported_at_cooldown_expiry(mocks, db, clock, monkeypatch):
    """offline -> online (10 min) -> offline again inside the cooldown and
    down for good: the second offline alert is suppressed, so at cooldown
    expiry a "Gateway still offline" must go out, not leave "back online" as
    the operator's last message."""
    notify_mock, _, _ = mocks
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(oa.asyncio, "sleep", fake_sleep)

    db.gw("gw-1")
    await oa.alert_gateway_offline(db, "gw-1")  # t=0
    clock[0] = T0 + timedelta(seconds=600)
    db.gw("gw-1", GatewayStatus.ONLINE)
    await oa.alert_gateway_online(db, "gw-1")
    clock[0] = T0 + timedelta(seconds=1200)
    db.gw("gw-1", GatewayStatus.OFFLINE)
    await oa.alert_gateway_offline(db, "gw-1")  # suppressed
    assert _titles(notify_mock) == ["Gateway offline"] * 2 + ["Gateway back online"] * 2

    await oa.alert_gateway_offline(db, "gw-1")  # still one re-check only
    task = oa._rechecks["gw-1"]
    await task
    assert sleeps == [10800 - 1200]
    assert _titles(notify_mock)[-2:] == ["Gateway still offline"] * 2
    assert "gw-1" not in oa._rechecks
    # The follow-up is armed again for the new offline alert.
    db.gw("gw-1", GatewayStatus.ONLINE)
    await oa.alert_gateway_online(db, "gw-1")
    assert notify_mock.await_args_list[-1].args[2] == "Gateway back online"


@pytest.mark.asyncio
async def test_recheck_silent_when_gateway_recovered_or_already_alerted(mocks, db, clock, monkeypatch):
    notify_mock, _, _ = mocks
    monkeypatch.setattr(oa.asyncio, "sleep", AsyncMock())

    db.gw("gw-1")
    await oa.alert_gateway_offline(db, "gw-1")
    db.gw("gw-1", GatewayStatus.ONLINE)
    await oa.alert_gateway_online(db, "gw-1")
    notify_mock.reset_mock()
    clock[0] = T0 + timedelta(seconds=100)
    db.gw("gw-1", GatewayStatus.OFFLINE)
    await oa.alert_gateway_offline(db, "gw-1")  # suppressed, re-check armed
    db.gw("gw-1", GatewayStatus.ONLINE)         # back online by then
    await oa._rechecks["gw-1"]
    notify_mock.assert_not_awaited()

    # An unanswered offline alert needs no re-check at all.
    db.gw("gw-2")
    await oa.alert_gateway_offline(db, "gw-2")
    clock[0] = T0 + timedelta(seconds=200)
    await oa.alert_gateway_offline(db, "gw-2")
    assert "gw-2" not in oa._rechecks


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScanDB:
    def __init__(self, rows):
        self.rows = rows
        self.stmts = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return _Rows(self.rows)


@pytest.mark.asyncio
async def test_startup_rearms_a_recheck_a_restart_dropped(mocks, clock, monkeypatch):
    """The suppressed-alert re-check was only a task in memory; at startup it
    is re-armed from the stored row for the rest of the cooldown, and never
    sooner than the offline debounce."""
    import backend.services.mqtt_manager as mm

    monkeypatch.setattr(mm, "GATEWAY_OFFLINE_NOTIFY_DELAY_SEC", 120.0)
    armed = []
    monkeypatch.setattr(oa, "_arm_recheck", lambda _f, gid, wait: armed.append((gid, wait)))
    scan = _ScanDB([("gw-1", T0 - timedelta(hours=1)), ("gw-2", T0 - timedelta(hours=5))])
    assert await oa.rearm_offline_rechecks(scan) == 2
    assert armed == [("gw-1", 7200.0), ("gw-2", 120.0)]
    sql = str(scan.stmts[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "offline_notified_at is null" in sql
    assert "last_operator_alert_at is not null" in sql
    assert "limit" in sql


@pytest.mark.asyncio
async def test_startup_rearm_is_off_with_the_kill_switch_and_never_raises(mocks, monkeypatch):
    monkeypatch.setenv("OPERATOR_ALERTS", "off")
    assert await oa.rearm_offline_rechecks(_ScanDB([("gw-1", T0)])) == 0
    monkeypatch.delenv("OPERATOR_ALERTS")

    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *exc):
            return False

    assert await oa.rearm_offline_rechecks(lambda: _Broken()) == 0


@pytest.mark.asyncio
async def test_recipients_query_filters_cpo_of_tenant_and_admins(monkeypatch):
    """_recipients is the only real SQL here: check the filters it builds."""
    from backend.database.models import UserRole

    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def scalar_one_or_none(self):
            return self._rows[0]

        def scalars(self):
            return self

        def all(self):
            return self._rows

    stmts = []

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            stmts.append(stmt)
            return _Res([42]) if len(stmts) == 1 else _Res([7, 8])

    ids = await oa._recipients(_S, "gw-1", include_admins=True, exclude_active_drivers=True)
    assert ids == [7, 8]
    params = list(stmts[1].compile().params.values())
    assert UserRole.CPO in params and UserRole.ADMIN in params and 42 in params
    assert "NOT IN" in str(stmts[1]).upper()

    stmts.clear()
    await oa._recipients(_S, "gw-1", include_admins=False)
    assert UserRole.ADMIN not in stmts[1].compile().params.values()
    assert "NOT IN" not in str(stmts[1]).upper()


@pytest.mark.asyncio
async def test_online_status_calls_alert_gateway_online():
    from backend.database.models import GatewayStatus

    MQTTManager._instance = None
    gw = MagicMock()
    gw.status = GatewayStatus.OFFLINE
    result = MagicMock()
    result.scalar_one_or_none.return_value = gw
    mgr = MQTTManager(db_session_factory=lambda: _Session(result))
    mgr._republish_off_for_orphaned_plugs = AsyncMock()
    mgr._publish_roster_for_gateway = AsyncMock()
    mgr._broadcast_plug_connectivity = AsyncMock()
    with patch.object(oa, "alert_gateway_online", AsyncMock()) as alert:
        await mgr._persist_gateway_status("gw-1", "online")
    alert.assert_awaited_once_with(mgr.db_session_factory, "gw-1")
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_immediate_offline_path_alerts_operators_before_driver_notice(monkeypatch):
    """delay<=0 path: operator alert fires, and ahead of the driver awaits so
    a racing `online` finds the follow-up armed."""
    import backend.services.mqtt_manager as mm
    from backend.database.models import GatewayStatus

    monkeypatch.setattr(mm, "GATEWAY_OFFLINE_NOTIFY_DELAY_SEC", 0)
    MQTTManager._instance = None
    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE
    result = MagicMock()
    result.scalar_one_or_none.return_value = gw
    mgr = MQTTManager(db_session_factory=lambda: _Session(result))
    order = []
    mgr._notify_drivers_gateway_offline = AsyncMock(side_effect=lambda *_: order.append("drivers"))
    mgr._broadcast_plug_connectivity = AsyncMock()

    async def fake_alert(*_a):
        order.append("operators")

    with patch.object(oa, "alert_gateway_offline", fake_alert):
        await mgr._persist_gateway_status("gw-1", "offline")
    assert order == ["operators", "drivers"]
    MQTTManager._instance = None
