"""
Operator alerts (services/operator_alerts.py): gateway offline while idle and
safety faults reach the tenant's CPOs (+ admins for offline) via notify(),
with in-memory cooldowns, a kill switch and an optional email. DB-free: the
recipient lookup, notify() and send_email() are mocked; no mail is sent.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services import operator_alerts as oa
from backend.services.mqtt_manager import MQTTManager


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    oa._reset_state()
    for name in ("OPERATOR_ALERTS", "OPERATOR_ALERT_EMAIL",
                 "GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC", "OPERATOR_ALERT_COOLDOWN_SEC"):
        monkeypatch.delenv(name, raising=False)
    yield
    oa._reset_state()


@pytest.fixture
def mocks(monkeypatch):
    notify_mock = AsyncMock()
    email_mock = MagicMock()
    recip = AsyncMock(return_value=[7, 8])
    monkeypatch.setattr(oa, "notify", notify_mock)
    monkeypatch.setattr(oa, "send_email", email_mock)
    monkeypatch.setattr(oa, "_recipients", recip)
    return notify_mock, email_mock, recip


async def _drain():
    if oa._pending:
        await asyncio.gather(*list(oa._pending))


@pytest.mark.asyncio
async def test_offline_alert_notifies_each_recipient_with_cooldown(mocks, monkeypatch):
    notify_mock, email_mock, recip = mocks
    clock = [1000.0]
    monkeypatch.setattr(oa, "_now", lambda: clock[0])

    await oa.alert_gateway_offline(MagicMock(), "gw-1")
    assert [c.args[0] for c in notify_mock.await_args_list] == [7, 8]
    assert notify_mock.await_args_list[0].args[1] == "gateway_offline"
    assert recip.await_args.kwargs["include_admins"] is True

    # Flap inside the 3 h default cooldown: nothing more.
    clock[0] += 3600
    await oa.alert_gateway_offline(MagicMock(), "gw-1")
    assert notify_mock.await_count == 2
    # Another gateway is independent.
    await oa.alert_gateway_offline(MagicMock(), "gw-2")
    assert notify_mock.await_count == 4
    # After the cooldown it alerts again.
    clock[0] += 3 * 3600
    await oa.alert_gateway_offline(MagicMock(), "gw-1")
    assert notify_mock.await_count == 6
    email_mock.assert_not_called()  # OPERATOR_ALERT_EMAIL unset


@pytest.mark.asyncio
async def test_online_followup_only_after_an_offline_alert_was_sent(mocks):
    notify_mock, _, _ = mocks
    await oa.alert_gateway_online(MagicMock(), "gw-1")  # nothing sent before
    notify_mock.assert_not_awaited()

    await oa.alert_gateway_offline(MagicMock(), "gw-1")
    notify_mock.reset_mock()
    await oa.alert_gateway_online(MagicMock(), "gw-1")
    assert [c.args[1] for c in notify_mock.await_args_list] == ["gateway_online"] * 2
    notify_mock.reset_mock()
    await oa.alert_gateway_online(MagicMock(), "gw-1")  # once per offline alert
    notify_mock.assert_not_awaited()

    # A second offline inside the cooldown is suppressed and arms no follow-up.
    await oa.alert_gateway_offline(MagicMock(), "gw-1")
    notify_mock.assert_not_awaited()
    await oa.alert_gateway_online(MagicMock(), "gw-1")
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_kill_switch_silences_everything(mocks, monkeypatch):
    notify_mock, email_mock, _ = mocks
    monkeypatch.setenv("OPERATOR_ALERTS", "off")
    monkeypatch.setenv("OPERATOR_ALERT_EMAIL", "me@example.test")
    await oa.alert_gateway_offline(MagicMock(), "gw-1")
    await oa.alert_safety_fault(MagicMock(), "gw-1", 1, "THERMAL_CUTOFF", "critical", None)
    await _drain()
    notify_mock.assert_not_awaited()
    email_mock.assert_not_called()


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
async def test_notify_failure_never_raises(mocks):
    notify_mock, _, _ = mocks
    notify_mock.side_effect = RuntimeError("db down")
    await oa.alert_gateway_offline(MagicMock(), "gw-1")  # swallowed and logged
    await oa.alert_safety_fault(MagicMock(), "gw-1", 1, "THERMAL_CUTOFF", "critical", None)


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
        mgr.db_session_factory, "gw-1", None, "UNAUTHORIZED_ON", "critical", "x"
    )
    MQTTManager._instance = None


# ---------------------------------------------- cooldown re-check (fix round) --

@pytest.mark.asyncio
async def test_short_outage_then_long_outage_is_reported_at_cooldown_expiry(mocks, monkeypatch):
    """offline -> online (10 min) -> offline again inside the cooldown and
    down for good: the second offline alert is suppressed, so at cooldown
    expiry a "Gateway still offline" must go out, not leave "back online" as
    the operator's last message."""
    notify_mock, _, _ = mocks
    clock = [0.0]
    monkeypatch.setattr(oa, "_now", lambda: clock[0])
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(oa.asyncio, "sleep", fake_sleep)
    still_down = AsyncMock(return_value=True)
    monkeypatch.setattr(oa, "_is_offline", still_down)

    await oa.alert_gateway_offline(MagicMock(), "gw-1")  # t=0
    clock[0] = 600
    await oa.alert_gateway_online(MagicMock(), "gw-1")
    clock[0] = 1200
    await oa.alert_gateway_offline(MagicMock(), "gw-1")  # suppressed
    titles = [c.args[2] for c in notify_mock.await_args_list]
    assert titles == ["Gateway offline"] * 2 + ["Gateway back online"] * 2

    await oa.alert_gateway_offline(MagicMock(), "gw-1")  # still one re-check only
    task = oa._rechecks["gw-1"]
    await task
    assert sleeps == [10800 - 1200]
    titles = [c.args[2] for c in notify_mock.await_args_list]
    assert titles[-2:] == ["Gateway still offline"] * 2
    assert "gw-1" not in oa._rechecks
    # The follow-up is armed again for the new offline alert.
    await oa.alert_gateway_online(MagicMock(), "gw-1")
    assert notify_mock.await_args_list[-1].args[2] == "Gateway back online"


@pytest.mark.asyncio
async def test_recheck_silent_when_gateway_recovered_or_already_alerted(mocks, monkeypatch):
    notify_mock, _, _ = mocks
    clock = [0.0]
    monkeypatch.setattr(oa, "_now", lambda: clock[0])
    monkeypatch.setattr(oa.asyncio, "sleep", AsyncMock())

    await oa.alert_gateway_offline(MagicMock(), "gw-1")
    await oa.alert_gateway_online(MagicMock(), "gw-1")
    notify_mock.reset_mock()
    clock[0] = 100
    await oa.alert_gateway_offline(MagicMock(), "gw-1")  # suppressed, re-check armed
    monkeypatch.setattr(oa, "_is_offline", AsyncMock(return_value=False))
    await oa._rechecks["gw-1"]
    notify_mock.assert_not_awaited()  # back online by then

    # An unanswered offline alert needs no re-check at all.
    await oa.alert_gateway_offline(MagicMock(), "gw-2")
    clock[0] = 200
    await oa.alert_gateway_offline(MagicMock(), "gw-2")
    assert "gw-2" not in oa._rechecks


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
