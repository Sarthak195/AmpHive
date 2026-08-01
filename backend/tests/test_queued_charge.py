"""
Tests for the queued-charge feature (queue a charge during an outage → the
reaper auto-starts it when line power returns).

DB-free: the AsyncSession, the queued-charge config resolvers, the endpoint
handlers, the reaper sweep, and the orphan-OFF coordination are exercised with
mocked sessions (the same pattern as test_session_start_plug_status /
test_session_reaper / test_reconnect_off_republish). What's under test is the
branch logic, not the SQL.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.database.models import (
    GatewayStatus,
    PlugStatus,
    QueuedChargeStatus,
)
from backend.routers.sessions import (
    cancel_queued_charge,
    get_queued_charges,
    queue_charge,
)
from backend.schemas import QueueChargeRequest
from backend.services.session_reaper import SessionReaperService
from backend.services.session_start import (
    auto_start_delay,
    queue_ttl,
    queued_charging_enabled,
)

# --------------------------------------------------------------------------
# Config resolvers (Plug override -> Tenant default)
# --------------------------------------------------------------------------

def _tenant(enabled=False, delay=2, ttl=720):
    t = MagicMock()
    t.id = 7
    t.queued_charging_enabled = enabled
    t.auto_start_delay_min = delay
    t.queue_ttl_min = ttl
    return t


def _plug(enabled_override=None, delay_override=None, powered=False,
          status=PlugStatus.AVAILABLE, group_id=None, plug_id=1):
    p = MagicMock()
    p.id = plug_id
    p.name = f"Bay {plug_id}"
    p.gateway_id = "gw-1"
    p.group_id = group_id
    p.status = status
    p.queued_charging_enabled = enabled_override
    p.auto_start_delay_min = delay_override
    # plug_is_powered reads last_telemetry_at: None (never reported) = unpowered.
    p.last_telemetry_at = datetime.now(timezone.utc) if powered else None
    p.powered_since = datetime.now(timezone.utc) if powered else None
    return p


def test_queued_charging_enabled_plug_override_wins():
    tenant = _tenant(enabled=False)
    # Plug explicitly on beats the tenant-off default, and vice versa.
    assert queued_charging_enabled(tenant, _plug(enabled_override=True)) is True
    assert queued_charging_enabled(_tenant(enabled=True),
                                   _plug(enabled_override=False)) is False
    # No override -> inherit the tenant default.
    assert queued_charging_enabled(tenant, _plug(enabled_override=None)) is False
    assert queued_charging_enabled(_tenant(enabled=True), _plug()) is True


def test_auto_start_delay_and_ttl_resolution():
    tenant = _tenant(delay=5, ttl=600)
    assert auto_start_delay(tenant, _plug(delay_override=None)) == 5   # inherit
    assert auto_start_delay(tenant, _plug(delay_override=1)) == 1      # override
    # queue_ttl is tenant-level only.
    assert queue_ttl(tenant, _plug()) == 600


# --------------------------------------------------------------------------
# Endpoint mocks
# --------------------------------------------------------------------------

def _user(user_id=5, balance=100):
    u = MagicMock()
    u.id = user_id
    u.email = "driver@example.com"
    u.coin_balance = balance
    return u


def _gateway(online=True):
    g = MagicMock()
    g.status = GatewayStatus.ONLINE if online else GatewayStatus.OFFLINE
    g.last_seen_at = datetime.now(timezone.utc)
    g.tenant_id = 7
    return g


def _s1(value):
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _s1n(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _one(value):
    r = MagicMock()
    r.one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()
    return db


def _req(plug_id=1):
    return QueueChargeRequest(plug_id=plug_id)


# --------------------------------------------------------------------------
# POST /api/sessions/queue
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_rejects_offline_gateway():
    db = _db(
        _s1n(_plug()),          # plug
        _s1(_gateway(online=False)),  # gateway
        _s1(_tenant(enabled=True)),   # tenant
    )
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_req(), _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "gateway_offline"


@pytest.mark.asyncio
async def test_queue_rejects_powered_plug():
    db = _db(
        _s1n(_plug(powered=True)),
        _s1(_gateway()),
        _s1(_tenant(enabled=True)),
    )
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_req(), _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "plug_powered"


@pytest.mark.asyncio
async def test_queue_rejects_when_cpo_disabled():
    db = _db(
        _s1n(_plug()),
        _s1(_gateway()),
        _s1(_tenant(enabled=False)),   # CPO hasn't enabled queued charging
    )
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_req(), _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "queue_disabled"


@pytest.mark.asyncio
async def test_queue_rejects_on_low_balance():
    db = _db(
        _s1n(_plug()),
        _s1(_gateway()),
        _s1(_tenant(enabled=True)),
        _one((Decimal("10"), Decimal("0"))),   # available_balance = 10 < floor 50
    )
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_req(), _user(), db)
    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "insufficient_balance"


@pytest.mark.asyncio
async def test_queue_rejects_over_per_user_cap():
    db = _db(
        _s1n(_plug()),
        _s1(_gateway()),
        _s1(_tenant(enabled=True)),
        _one((Decimal("100"), Decimal("0"))),
        _s1(2),                                 # already 2 WAITING (== cap)
    )
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_req(), _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "queue_limit"


@pytest.mark.asyncio
async def test_queue_rejects_duplicate_waiting_row():
    db = _db(
        _s1n(_plug()),
        _s1(_gateway()),
        _s1(_tenant(enabled=True)),
        _one((Decimal("100"), Decimal("0"))),
        _s1(0),                                 # under the cap
        _s1n(42),                               # an existing WAITING row
    )
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_req(), _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "already_queued"


@pytest.mark.asyncio
async def test_queue_success_creates_waiting_row():
    db = _db(
        _s1n(_plug()),
        _s1(_gateway()),
        _s1(_tenant(enabled=True, ttl=720)),
        _one((Decimal("100"), Decimal("0"))),
        _s1(0),                                 # under the cap
        _s1n(None),                             # no existing WAITING row
    )
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        resp = await queue_charge(_req(), _user(), db)

    assert resp["status"] == "waiting"
    assert resp["plug_id"] == 1
    assert resp["max_kwh"] == 30.0            # schema default snapshotted
    db.add.assert_called_once()
    notify.assert_awaited_once()
    # The persisted row carries a TTL-derived expiry in the future.
    queued = db.add.call_args.args[0]
    assert queued.status == QueuedChargeStatus.WAITING
    assert queued.expires_at > datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# GET /api/sessions/queued  +  DELETE /api/sessions/queue/{id}
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_queued_returns_waiting_rows():
    now = datetime.now(timezone.utc)
    qc = MagicMock()
    qc.id = 3
    qc.plug_id = 1
    qc.status = QueuedChargeStatus.WAITING
    qc.created_at = now
    qc.expires_at = now + timedelta(hours=12)
    qc.max_kwh = 30.0
    qc.max_duration_seconds = 14400
    rows = MagicMock()
    rows.all.return_value = [(qc, "Bay 1")]
    db = _db(rows)

    out = await get_queued_charges(_user(), db)
    assert len(out) == 1
    assert out[0]["id"] == 3
    assert out[0]["plug_name"] == "Bay 1"
    assert out[0]["status"] == "waiting"


@pytest.mark.asyncio
async def test_cancel_missing_row_is_404():
    db = _db(_s1n(None))
    with pytest.raises(HTTPException) as exc:
        await cancel_queued_charge(99, _user(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_already_started_is_409():
    qc = MagicMock()
    qc.status = QueuedChargeStatus.STARTED
    db = _db(_s1n(qc))
    with pytest.raises(HTTPException) as exc:
        await cancel_queued_charge(3, _user(), db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_cancel_waiting_row_succeeds():
    qc = MagicMock()
    qc.id = 3
    qc.plug_id = 1
    qc.status = QueuedChargeStatus.WAITING
    db = _db(_s1n(qc))
    resp = await cancel_queued_charge(3, _user(), db)
    assert resp["status"] == "cancelled"
    assert qc.status == QueuedChargeStatus.CANCELLED


# --------------------------------------------------------------------------
# Reaper sweep: reap_queued_starts_once
# --------------------------------------------------------------------------

def _cm(db):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _factory(*dbs):
    f = MagicMock()
    f.side_effect = [_cm(db) for db in dbs]
    return f


def _scan_db(ids):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(ids)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=r)
    return db


def _row_db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


def _upd():
    """Stub result for an UPDATE statement (expire_lapsed_reservations) —
    the reaper never reads its return value."""
    return MagicMock()


def _reservation(user_id):
    r = MagicMock()
    r.user_id = user_id
    return r


def _qc(status=QueuedChargeStatus.WAITING, expires_in_min=60, plug_id=1, user_id=5):
    qc = MagicMock()
    qc.id = 1
    qc.status = status
    qc.plug_id = plug_id
    qc.user_id = user_id
    qc.tenant_id = 7
    qc.max_kwh = 30.0
    qc.max_duration_seconds = 14400
    qc.expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_in_min)
    return qc


@pytest.mark.asyncio
async def test_sweep_expires_ttl_lapsed_row():
    qc = _qc(expires_in_min=-1)   # already past its TTL
    svc = SessionReaperService(_factory(_scan_db([1]), _row_db(_s1n(qc))), AsyncMock())

    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        started = await svc.reap_queued_starts_once()

    assert started == 0
    assert qc.status == QueuedChargeStatus.EXPIRED
    assert notify.await_args.args[1] == "queued_charge_expired"


@pytest.mark.asyncio
async def test_sweep_leaves_waiting_mid_debounce():
    """Powered, but powered_since is inside the debounce window -> stays WAITING,
    begin_active_session is never called (a blip must not energize the plug)."""
    qc = _qc()
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc)   # just resumed
    tenant = _tenant(delay=5)                          # 5-min debounce
    row_db = _row_db(_s1n(qc), _s1n(_user()), _s1(0), _s1n(plug), _s1(tenant))
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session",
               AsyncMock()) as begin, \
         patch("backend.services.notifications.notify", AsyncMock()):
        started = await svc.reap_queued_starts_once()

    assert started == 0
    assert qc.status == QueuedChargeStatus.WAITING
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_starts_debounced_row():
    qc = _qc()
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)  # past delay
    tenant = _tenant(delay=2)
    user = _user()
    gateway = _gateway()
    row_db = _row_db(
        _s1n(qc), _s1n(user), _s1(0), _s1n(plug), _s1(tenant),
        _upd(), _s1n(None), _s1(gateway),
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    session = MagicMock()
    session.id = 99
    with patch("backend.services.session_start.begin_active_session",
               AsyncMock(return_value=session)) as begin, \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        started = await svc.reap_queued_starts_once()

    assert started == 1
    begin.assert_awaited_once()
    assert qc.status == QueuedChargeStatus.STARTED
    assert qc.started_session_id == 99
    assert notify.await_args.args[1] == "queued_charge_started"


@pytest.mark.asyncio
async def test_sweep_expires_on_balance_failure():
    """begin_active_session raising 402 (funds dropped since queue time — never
    locked) expires the queued charge and notifies, not a hard failure."""
    qc = _qc()
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    tenant = _tenant(delay=2)
    row_db = _row_db(
        _s1n(qc), _s1n(_user()), _s1(0), _s1n(plug), _s1(tenant),
        _upd(), _s1n(None), _s1(_gateway()),
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session",
               AsyncMock(side_effect=HTTPException(status_code=402, detail="broke"))), \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        started = await svc.reap_queued_starts_once()

    assert started == 0
    assert qc.status == QueuedChargeStatus.EXPIRED
    assert notify.await_args.args[1] == "queued_charge_expired"


@pytest.mark.asyncio
async def test_sweep_fails_on_caps_failure():
    """A caps/publish failure (non-402) marks the queued charge FAILED."""
    qc = _qc()
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    tenant = _tenant(delay=2)
    row_db = _row_db(
        _s1n(qc), _s1n(_user()), _s1(0), _s1n(plug), _s1(tenant),
        _upd(), _s1n(None), _s1(_gateway()),
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session",
               AsyncMock(side_effect=HTTPException(status_code=409, detail="full"))), \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        started = await svc.reap_queued_starts_once()

    assert started == 0
    assert qc.status == QueuedChargeStatus.FAILED
    assert notify.await_args.args[1] == "queued_charge_failed"


@pytest.mark.asyncio
async def test_sweep_defers_when_another_users_reservation_covers_plug():
    """[Reservation gate] A BOOKED reservation held by a DIFFERENT user that
    covers right now must not be auto-started into — that would steal a
    bookable slot the walk-up start route itself would 409 on
    (routers/sessions.py start_charging_session). The queued charge stays
    WAITING (retried next tick) and begin_active_session is never called.

    The mock DB is loaded with a result for every db.execute() the
    fall-through path would reach if this gate were deleted or inverted —
    including the gateway lookup that only happens AFTER the gate — up to
    and including a real begin_active_session award. That matters: with only
    as many results as the correct defer path consumes, a broken gate would
    make one extra, unmocked db.execute() that raises StopAsyncIteration,
    which the sweep's blanket per-row `except Exception` swallows silently —
    leaving qc.status untouched at WAITING and begin_active_session unawaited
    for the WRONG reason, so the assertions below would pass even with the
    gate gone. With the full mock chain present, a removed/inverted gate
    instead runs all the way through to a genuine STARTED and an actual
    begin_active_session await, which the assertions below will catch."""
    qc = _qc(user_id=5)
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    tenant = _tenant(delay=2)
    someone_elses_reservation = _reservation(user_id=99)
    gateway = _gateway()
    session = MagicMock()
    session.id = 99
    row_db = _row_db(
        _s1n(qc), _s1n(_user(user_id=5)), _s1(0), _s1n(plug), _s1(tenant),
        _upd(), _s1n(someone_elses_reservation),
        _s1(gateway),  # only reached if the reservation gate fails to defer
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session",
               AsyncMock(return_value=session)) as begin, \
         patch("backend.services.notifications.notify", AsyncMock()):
        started = await svc.reap_queued_starts_once()

    assert started == 0
    assert qc.status == QueuedChargeStatus.WAITING
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_defers_when_user_at_active_session_cap():
    """[Cap gate] A user already running MAX_ACTIVE_SESSIONS_PER_USER (2)
    ACTIVE sessions must not be auto-started past the cap the walk-up start
    route enforces (routers/sessions.py start_charging_session). The queued
    charge stays WAITING (retried next tick) and begin_active_session is
    never called.

    The mock DB is loaded with a result for every db.execute() the rest of
    the sweep would reach if this cap check were deleted or inverted — plug,
    tenant, the reservation-expiry UPDATE, the covering-reservation lookup,
    and the gateway lookup — all the way through to a genuine
    begin_active_session award. That matters: with only as many results as
    the correct defer path consumes (just the count query), a broken gate
    would make one extra, unmocked db.execute() (the plug lookup) that raises
    StopAsyncIteration, which the sweep's blanket per-row `except Exception`
    swallows silently — leaving qc.status untouched at WAITING and
    begin_active_session unawaited for the WRONG reason, so the assertions
    below would pass even with the gate gone. With the full mock chain
    present, a removed/inverted cap check instead runs all the way through to
    a genuine STARTED and an actual begin_active_session await, which the
    assertions below will catch."""
    qc = _qc(user_id=5)
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    tenant = _tenant(delay=2)
    gateway = _gateway()
    session = MagicMock()
    session.id = 99
    row_db = _row_db(
        _s1n(qc), _s1n(_user(user_id=5)), _s1(2),   # already at the cap
        _s1n(plug), _s1(tenant), _upd(), _s1n(None), _s1(gateway),
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session",
               AsyncMock(return_value=session)) as begin, \
         patch("backend.services.notifications.notify", AsyncMock()):
        started = await svc.reap_queued_starts_once()

    assert started == 0
    assert qc.status == QueuedChargeStatus.WAITING
    begin.assert_not_awaited()


# --------------------------------------------------------------------------
# Orphan-OFF coordination (MQTTManager._republish_off_for_orphaned_plugs)
# --------------------------------------------------------------------------

def _result_rows(rows):
    r = MagicMock()
    r.all.return_value = list(rows)
    return r


def _result_scalars(values):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(values)
    return r


def _result_scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _manager(execute_results):
    from backend.services.mqtt_manager import MQTTManager
    MQTTManager._instance = None
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_results)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    mgr = MQTTManager(db_session_factory=factory)
    mgr.send_plug_command = MagicMock(return_value=True)
    return mgr


@pytest.mark.asyncio
async def test_orphan_off_skips_mid_debounce_queued_plug():
    """A plug with a WAITING queued charge that is still mid-debounce is left
    OFF for the queue sweep to energize — NOT orphan-OFF'd. A plug with no
    queued charge still gets its OFF (existing behavior preserved), and each
    CPO of the tenant gets one orphan_off bell notification for it."""
    from backend.services.mqtt_manager import MQTTManager

    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE   # not a transition -> no connectivity broadcast
    queued_plug = _plug(powered=False, plug_id=1)   # unpowered -> mid-debounce
    tenant = _tenant(delay=2)

    mgr = _manager([
        _result_scalar_one_or_none(gw),                                  # gateway lookup
        _result_rows([(1, "10.0.0.11", "Bay 1"), (2, "10.0.0.12", "Bay 2")]),  # plugs
        _result_scalars([]),                                             # no ACTIVE sessions
        _result_rows([(queued_plug, tenant)]),                           # plug 1 has a WAITING queue
        _result_scalars([101, 102]),                                     # CPO user ids for the tenant
    ])

    with patch("backend.services.mqtt.status.notify", AsyncMock()) as notify_mock:
        await mgr._persist_gateway_status("gw-1", "online")

    # Plug 2 (no queue) gets OFF; plug 1 (mid-debounce queue) is skipped.
    called = [c.args[1] for c in mgr.send_plug_command.call_args_list]
    assert called == [2]

    # One orphan_off notify per (off'd plug, CPO) pair — here 1 plug x 2 CPOs.
    assert notify_mock.await_count == 2
    notified_cpos = [c.args[0] for c in notify_mock.await_args_list]
    assert notified_cpos == [101, 102]
    assert all(c.args[1] == "orphan_off" for c in notify_mock.await_args_list)
    assert all(c.kwargs.get("plug_id") == 2 for c in notify_mock.await_args_list)
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_orphan_off_still_offs_eligible_queued_plug():
    """A queued plug that is already powered PAST its debounce is not held back
    (the sweep will do a proper start); it still gets the safety OFF here,
    and the tenant's CPOs are notified."""
    from backend.services.mqtt_manager import MQTTManager

    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE
    eligible_plug = _plug(powered=True, plug_id=1)
    eligible_plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    tenant = _tenant(delay=2)

    mgr = _manager([
        _result_scalar_one_or_none(gw),
        _result_rows([(1, "10.0.0.11", "Bay 1")]),
        _result_scalars([]),
        _result_rows([(eligible_plug, tenant)]),
        _result_scalars([101]),   # one CPO for the tenant
    ])

    with patch("backend.services.mqtt.status.notify", AsyncMock()) as notify_mock:
        await mgr._persist_gateway_status("gw-1", "online")

    called = [c.args[1] for c in mgr.send_plug_command.call_args_list]
    assert called == [1]

    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.args[0] == 101
    assert notify_mock.await_args.args[1] == "orphan_off"
    assert notify_mock.await_args.kwargs.get("plug_id") == 1
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_orphan_off_no_orphans_skips_cpo_lookup_and_notify():
    """When every plug already has an ACTIVE session (nothing actually gets
    OFF'd), the CPO lookup query is never issued and notify() is never
    called — the common case (a clean reconnect) stays cheap."""
    from backend.services.mqtt_manager import MQTTManager

    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE

    mgr = _manager([
        _result_scalar_one_or_none(gw),                 # gateway lookup
        _result_rows([(1, "10.0.0.11", "Bay 1")]),       # plugs
        _result_scalars([1]),                            # plug 1 has an ACTIVE session
        _result_rows([]),                                # no queued charges
        # No 5th result: the CPO-users query must not be issued.
    ])

    with patch("backend.services.mqtt.status.notify", AsyncMock()) as notify_mock:
        await mgr._persist_gateway_status("gw-1", "online")

    mgr.send_plug_command.assert_not_called()
    notify_mock.assert_not_awaited()
    MQTTManager._instance = None
