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
    t.timezone = "Asia/Kolkata"
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


def _req(plug_id=1, **kw):
    return QueueChargeRequest(plug_id=plug_id, **kw)


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
        _s1n(None),                             # no other driver's schedule
    )
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        resp = await queue_charge(_req(), _user(), db)

    assert resp["status"] == "waiting"
    assert resp["plug_id"] == 1
    # [Opt-in charging limits] no limit chosen -> NULL snapshotted, not a
    # hidden default; the eventual auto-start charges until stopped.
    assert resp["max_kwh"] is None
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
    qc.not_before = None
    rows.all.return_value = [(qc, "Bay 1", "Asia/Kolkata")]
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


def _qc(status=QueuedChargeStatus.WAITING, expires_in_min=60, plug_id=1, user_id=5,
        not_before=None):
    qc = MagicMock()
    qc.id = 1
    qc.status = status
    qc.plug_id = plug_id
    qc.user_id = user_id
    qc.tenant_id = 7
    qc.max_kwh = 30.0
    qc.max_duration_seconds = 14400
    qc.expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_in_min)
    qc.not_before = not_before   # None = outage queue (a MagicMock attr would be truthy)
    qc.auto_stop_when_full = None
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
        _upd(), _s1n(None), _s1n(None), _s1(gateway),
        _s1n(None),  # [C5] routine lookup: no routine owns this row
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
    assert begin.await_args.kwargs["auto_stop_when_full"] is False
    assert qc.status == QueuedChargeStatus.STARTED
    assert qc.started_session_id == 99
    assert notify.await_args.args[1] == "queued_charge_started"


@pytest.mark.asyncio
async def test_sweep_carries_the_auto_stop_opt_in_to_the_session():
    """[Charge complete] A queued/scheduled charge's auto_stop_when_full
    reaches begin_active_session, so the auto-started session keeps it."""
    qc = _qc()
    qc.auto_stop_when_full = True
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    row_db = _row_db(
        _s1n(qc), _s1n(_user()), _s1(0), _s1n(plug), _s1(_tenant(delay=2)),
        _upd(), _s1n(None), _s1n(None), _s1(_gateway()),
        _s1n(None),  # [C5] routine lookup: no routine owns this row
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session",
               AsyncMock(return_value=MagicMock(id=99))) as begin,          patch("backend.services.notifications.notify", AsyncMock()):
        await svc.reap_queued_starts_once()

    assert begin.await_args.kwargs["auto_stop_when_full"] is True


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
        _upd(), _s1n(None), _s1n(None), _s1(_gateway()),
        _s1n(None),  # [C5] routine lookup: no routine owns this row
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
        _upd(), _s1n(None), _s1n(None), _s1(_gateway()),
        _s1n(None),  # [C5] routine lookup: no routine owns this row
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
        _upd(), _s1n(someone_elses_reservation), _s1n(None),
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
async def test_sweep_passes_own_covering_reservation_to_start_helper():
    """[U0] The queue owner's OWN covering reservation must be handed to
    begin_active_session (as the manual start route does) so it flips to
    FULFILLED with the session linked; otherwise it stays BOOKED during the
    session and lazily expires as a no-show."""
    qc = _qc(user_id=5)
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    own_reservation = _reservation(user_id=5)
    session = MagicMock()
    session.id = 99
    row_db = _row_db(
        _s1n(qc), _s1n(_user(user_id=5)), _s1(0), _s1n(plug), _s1(_tenant(delay=2)),
        _upd(), _s1n(own_reservation), _s1n(None), _s1(_gateway()),
        _s1n(None),  # [C5] routine lookup: no routine owns this row
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session",
               AsyncMock(return_value=session)) as begin, \
         patch("backend.services.notifications.notify", AsyncMock()):
        started = await svc.reap_queued_starts_once()

    assert started == 1
    assert begin.await_args.kwargs.get("covering_reservation") is own_reservation


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
        _s1n(plug), _s1(tenant), _upd(), _s1n(None), _s1n(None), _s1(gateway),
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
    queued charge still gets its OFF (existing behavior preserved). This runs
    on the retained-replay path (gateway already ONLINE), so no operator
    alert fires — the transition-gated `orphan_off` bell is covered in
    test_notifications / test_mqtt_manager."""
    from backend.services.mqtt_manager import MQTTManager

    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE   # not a transition -> no connectivity broadcast
    queued_plug = _plug(powered=False, plug_id=1)   # unpowered -> mid-debounce
    tenant = _tenant(delay=2)

    mgr = _manager([
        _result_scalar_one_or_none(gw),                                  # gateway lookup
        # plugs: (plug_id, local_ip, name, current_power_w)
        _result_rows([(1, "10.0.0.11", "Bay 1", 0.0), (2, "10.0.0.12", "Bay 2", 0.0)]),
        _result_scalars([]),                                             # no ACTIVE sessions
        _result_rows([(queued_plug, tenant)]),                           # plug 1 has a WAITING queue
        # No 5th result: on a retained replay the CPO-users query is not issued.
    ])

    with patch("backend.services.mqtt.status.notify", AsyncMock()) as notify_mock:
        await mgr._persist_gateway_status("gw-1", "online")

    # Plug 2 (no queue) gets OFF; plug 1 (mid-debounce queue) is skipped.
    called = [c.args[1] for c in mgr.send_plug_command.call_args_list]
    assert called == [2]

    # Retained replay: the OFF sweep runs, the operator bell does not.
    notify_mock.assert_not_awaited()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_orphan_off_still_offs_eligible_queued_plug():
    """A queued plug that is already powered PAST its debounce is not held back
    (the sweep will do a proper start); it still gets the safety OFF here.
    Retained-replay path, so no operator bell (see the transition-gated
    tests in test_notifications / test_mqtt_manager)."""
    from backend.services.mqtt_manager import MQTTManager

    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE
    eligible_plug = _plug(powered=True, plug_id=1)
    eligible_plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    tenant = _tenant(delay=2)

    mgr = _manager([
        _result_scalar_one_or_none(gw),
        _result_rows([(1, "10.0.0.11", "Bay 1", 0.0)]),
        _result_scalars([]),
        _result_rows([(eligible_plug, tenant)]),
        # No 5th result: on a retained replay the CPO-users query is not issued.
    ])

    with patch("backend.services.mqtt.status.notify", AsyncMock()) as notify_mock:
        await mgr._persist_gateway_status("gw-1", "online")

    called = [c.args[1] for c in mgr.send_plug_command.call_args_list]
    assert called == [1]

    notify_mock.assert_not_awaited()
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
        _result_rows([(1, "10.0.0.11", "Bay 1", 0.0)]),  # plugs
        _result_scalars([1]),                            # plug 1 has an ACTIVE session
        _result_rows([]),                                # no queued charges
        # No 5th result: the CPO-users query must not be issued.
    ])

    with patch("backend.services.mqtt.status.notify", AsyncMock()) as notify_mock:
        await mgr._persist_gateway_status("gw-1", "online")

    mgr.send_plug_command.assert_not_called()
    notify_mock.assert_not_awaited()
    MQTTManager._instance = None


# --------------------------------------------------------------------------
# [Scheduled start] POST /api/sessions/queue with not_before (powered plug)
# --------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))


def _soon(hours=2):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _sched_req(**kw):
    kw.setdefault("not_before", _soon())
    kw.setdefault("max_duration_seconds", 3600)
    return _req(**kw)


def _sched_prefix(plug=None, tenant=None):
    """plug (locked), gateway, tenant: the shared head of the endpoint."""
    return (
        _s1n(plug if plug is not None else _plug(powered=True)),
        _s1(_gateway()),
        _s1(tenant if tenant is not None else _tenant(enabled=True, ttl=720)),
    )


def test_schedule_request_rejects_naive_not_before():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        QueueChargeRequest(plug_id=1, not_before=datetime(2026, 11, 15, 22, 0))
    ok = QueueChargeRequest(plug_id=1, not_before="2026-11-15T22:00:00+05:30")
    assert ok.not_before.utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.asyncio
async def test_schedule_rejects_unpowered_plug():
    db = _db(*_sched_prefix(plug=_plug(powered=False)))
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_sched_req(), _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "plug_unpowered"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [
    (PlugStatus.OCCUPIED, "plug_in_use"),
    (PlugStatus.MAINTENANCE, "plug_out_of_service"),
])
async def test_schedule_rejects_busy_or_out_of_service_plug(status, code):
    db = _db(*_sched_prefix(plug=_plug(powered=True, status=status)))
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_sched_req(), _user(), db)
    assert exc.value.detail["code"] == code


@pytest.mark.asyncio
async def test_schedule_requires_a_limit():
    """Nobody is at the plug when a schedule fires: an unlimited session on an
    absent car would keep the socket live and the whole wallet held."""
    db = _db(*_sched_prefix())
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_req(not_before=_soon()), _user(), db)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "schedule_needs_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [timedelta(minutes=-1), timedelta(hours=24, minutes=1)])
async def test_schedule_rejects_out_of_range_time(offset):
    db = _db(*_sched_prefix())
    with pytest.raises(HTTPException) as exc:
        await queue_charge(
            _sched_req(not_before=datetime.now(timezone.utc) + offset), _user(), db
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "schedule_out_of_range"


def _hours_plug(nb, open_at_nb):
    """A powered plug with opening hours that are open all day on every
    weekday, or closed all day on not_before's local weekday only."""
    from backend.services.pricing import _to_local, _zone

    plug = _plug(powered=True)
    days = 127
    if not open_at_nb:
        days &= ~(1 << _to_local(nb, _zone("Asia/Kolkata")).weekday())
    plug.opening_hours = [{"days": days, "start": 0, "end": 1440}]
    return plug


@pytest.mark.asyncio
async def test_schedule_rejects_a_start_time_outside_opening_hours():
    """[Plug hours] A schedule for a closed slot would hold the socket against
    other drivers and then expire; refuse it like a reservation."""
    nb = _soon()
    db = _db(*_sched_prefix(plug=_hours_plug(nb, open_at_nb=False)))
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_sched_req(not_before=nb), _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "plug_closed"
    assert "closed at that time" in exc.value.detail["message"]
    db.add.assert_not_called()

    # Open at that time: admission goes on to the booking check.
    db = _db(*_sched_prefix(plug=_hours_plug(nb, open_at_nb=True)), _s1n(11))
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_sched_req(not_before=nb), _user(), db)
    assert exc.value.detail["code"] == "plug_reserved"


@pytest.mark.asyncio
async def test_schedule_rejects_other_drivers_overlapping_booking():
    db = _db(*_sched_prefix(), _s1n(11))         # a BOOKED window overlaps
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_sched_req(), _user(), db)
    assert exc.value.detail["code"] == "plug_reserved"


@pytest.mark.asyncio
async def test_schedule_rejects_booking_between_now_and_not_before():
    """The scheduled car holds the socket from now, and the walk-up refusal
    blocks other drivers from now, so a booking made earlier that sits wholly
    between now and not_before must refuse the schedule (else its holder is
    locked out of their own window)."""
    import operator

    now = datetime.now(timezone.utc)
    nb = now + timedelta(hours=6)
    b_start, b_end = now + timedelta(hours=1), now + timedelta(hours=2)
    db = _db(*_sched_prefix(), _s1n(11))
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_sched_req(not_before=nb), _user(), db)
    assert exc.value.detail["code"] == "plug_reserved"

    # The mocked DB returns the row regardless, so check the overlap window the
    # reservation query actually asks for against this booking.
    stmt = db.execute.call_args_list[3].args[0]
    bounds = {}
    for clause in stmt.whereclause.clauses:
        key = getattr(clause.left, "key", None)
        if key in ("start_at", "end_at"):
            bounds[(key, clause.operator)] = clause.right.effective_value
    start_lt = bounds[("start_at", operator.lt)]
    end_gt = bounds[("end_at", operator.gt)]
    assert end_gt <= datetime.now(timezone.utc)      # window opens now, not at nb
    assert b_start < start_lt and b_end > end_gt     # the booking matches


@pytest.mark.asyncio
async def test_schedule_rejects_when_another_driver_holds_the_socket():
    db = _db(*_sched_prefix(), _s1n(None), _s1n(12))   # other user's WAITING row
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_sched_req(), _user(), db)
    assert exc.value.detail["code"] == "plug_scheduled"


@pytest.mark.asyncio
async def test_schedule_success_persists_not_before_and_grace_expiry():
    now = datetime.now(timezone.utc)
    nb = now.replace(hour=16, minute=30, second=0, microsecond=0)
    if nb <= now + timedelta(minutes=1):
        nb += timedelta(days=1)                   # next 16:30Z = 22:00 IST
    db = _db(
        *_sched_prefix(),
        _s1n(None),                               # no overlapping booking
        _s1n(None),                               # no other driver's row
        _one((Decimal("100"), Decimal("0"))),     # available balance
        _s1(0),                                   # under the cap
        _s1n(None),                               # no own WAITING row
    )
    # The venv has no IANA database, so pin the tenant zone to +05:30.
    with patch("backend.services.pricing._zone", lambda _tz: IST), \
         patch("backend.services.pricing.zone_name", lambda _tz: "Asia/Kolkata"), \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        resp = await queue_charge(_sched_req(not_before=nb), _user(), db)

    queued = db.add.call_args.args[0]
    assert queued.not_before == nb
    # min(queue_ttl 720, SCHEDULE_START_GRACE_MIN 30): a late start is bounded.
    assert queued.expires_at == nb + timedelta(minutes=30)
    assert queued.max_duration_seconds == 3600
    assert resp["tenant_timezone"] == "Asia/Kolkata"
    body = notify.await_args.args[3]
    assert "22:00" in body and "Asia/Kolkata" in body


@pytest.mark.asyncio
async def test_outage_queue_refused_when_another_driver_scheduled_the_socket():
    db = _db(
        _s1n(_plug()),
        _s1(_gateway()),
        _s1(_tenant(enabled=True)),
        _one((Decimal("100"), Decimal("0"))),
        _s1(0),
        _s1n(None),                               # no own WAITING row
        _s1n(12),                                 # another driver's schedule
    )
    with pytest.raises(HTTPException) as exc:
        await queue_charge(_req(), _user(), db)
    assert exc.value.detail["code"] == "plug_scheduled"
    db.add.assert_not_called()


def _has_tz_db():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/London")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_tz_db(), reason="no IANA tz database on this host")
def test_zone_name_resolves_like_billing():
    from backend.services.pricing import zone_name
    assert zone_name("Europe/London") == "Europe/London"
    assert zone_name("Bad/Zone") == "Asia/Kolkata"      # billing's fallback


def test_zone_name_last_resort_is_utc():
    from backend.services.pricing import zone_name
    with patch("backend.services.pricing._zone", lambda _tz: timezone.utc):
        assert zone_name("Asia/Kolkata") == "UTC"


# --------------------------------------------------------------------------
# [Scheduled start] Sweep: the not_before gate and scheduled-row handling
# --------------------------------------------------------------------------

def _due_plug(powered_for_min=120, status=PlugStatus.AVAILABLE):
    plug = _plug(powered=True, status=status)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=powered_for_min)
    return plug


def _started_session(rate=Decimal("4.00")):
    session = MagicMock()
    session.id = 99
    session.rate_coins_per_kwh = rate
    return session


async def _sweep(row_db, begin=None):
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())
    begin = begin or AsyncMock(return_value=_started_session())
    with patch("backend.services.session_start.begin_active_session", begin), \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        started = await svc.reap_queued_starts_once()
    return started, begin, notify


def _scheduled_start_chain(qc, plug, tenant=None, gateway=None):
    """qc, user, active count, plug, tenant, reservation expiry, covering
    booking, gateway. (No outage-row guard: the row is scheduled.)"""
    return _row_db(
        _s1n(qc), _s1n(_user()), _s1(0), _s1n(plug),
        _s1(tenant or _tenant(enabled=True, delay=2)),
        _upd(), _s1n(None), _s1(gateway or _gateway()),
        _s1n(None),  # [C5] routine lookup: no routine owns this row
    )


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


@pytest.mark.asyncio
async def test_sweep_leaves_not_yet_due_schedule_without_user_lock():
    qc = _qc(not_before=_soon(hours=3), expires_in_min=210)
    row_db = _row_db(_s1n(qc))
    started, begin, _ = await _sweep(row_db)
    assert started == 0
    assert qc.status == QueuedChargeStatus.WAITING
    assert row_db.execute.await_count == 1        # only the qc row lock
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_starts_schedule_at_not_before_when_power_never_dropped():
    qc = _qc(not_before=_ago(seconds=30))
    started, begin, notify = await _sweep(_scheduled_start_chain(qc, _due_plug()))
    assert started == 1
    assert begin.await_args.kwargs == {
        "max_kwh": 30.0, "max_duration_seconds": 14400, "covering_reservation": None,
        "auto_stop_when_full": False, "announce_start": True,
    }
    assert qc.status == QueuedChargeStatus.STARTED
    assert notify.await_args.args[1] == "queued_charge_started"
    assert notify.await_args.args[2] == "Your scheduled charge started"


@pytest.mark.asyncio
async def test_sweep_routine_row_starts_with_no_start_notice_of_its_own():
    """[C5 + C7] A row a charge routine owns: neither queued_charge_started nor
    session_started fires (settle_routines sends the one routine_started)."""
    qc = _qc(not_before=_ago(seconds=30))
    chain = _scheduled_start_chain(qc, _due_plug())
    chain.execute.side_effect = list(chain.execute.side_effect)[:-1] + [_s1n(42)]
    started, begin, notify = await _sweep(chain)
    assert started == 1
    assert qc.status == QueuedChargeStatus.STARTED
    assert begin.await_args.kwargs["announce_start"] is False
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_debounce_runs_from_powered_since_not_from_not_before():
    """Power back 1 min before not_before, 2 min debounce. A Wi-Fi blip that
    re-baselines powered_since (telemetry gap > 90 s) behaves the same way."""
    # At not_before: powered only 1 min, so still debouncing.
    qc = _qc(not_before=_ago(seconds=5))
    row_db = _row_db(_s1n(qc), _s1n(_user()), _s1(0), _s1n(_due_plug(powered_for_min=1)),
                     _s1(_tenant(enabled=True, delay=2)))
    started, begin, _ = await _sweep(row_db)
    assert started == 0 and qc.status == QueuedChargeStatus.WAITING
    begin.assert_not_awaited()
    # At not_before + 1 min: powered 2 min, so it starts with no fresh
    # debounce counted from not_before.
    qc = _qc(not_before=_ago(minutes=1))
    started, _, _ = await _sweep(_scheduled_start_chain(qc, _due_plug(powered_for_min=2)))
    assert started == 1


@pytest.mark.asyncio
async def test_sweep_expires_schedule_past_its_grace_with_scheduled_copy():
    qc = _qc(not_before=_ago(minutes=31), expires_in_min=-1)
    _, _, notify = await _sweep(_row_db(_s1n(qc)))
    assert qc.status == QueuedChargeStatus.EXPIRED
    assert notify.await_args.args[1] == "queued_charge_expired"
    assert "in time" in notify.await_args.args[3]


@pytest.mark.asyncio
@pytest.mark.parametrize("holder,status,notified", [
    (5, QueuedChargeStatus.CANCELLED, False),    # the owner started now
    (77, QueuedChargeStatus.FAILED, True),       # someone else's car
])
async def test_sweep_scheduled_row_on_occupied_plug(holder, status, notified):
    qc = _qc(not_before=_ago(seconds=30))
    row_db = _row_db(_s1n(qc), _s1n(_user()), _s1(0),
                     _s1n(_due_plug(status=PlugStatus.OCCUPIED)),
                     _s1(_tenant(enabled=True, delay=2)), _s1n(holder))
    _, begin, notify = await _sweep(row_db)
    assert qc.status == status
    assert notify.await_count == (1 if notified else 0)
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_fails_schedule_when_another_driver_booked_the_window():
    qc = _qc(not_before=_ago(seconds=30))
    row_db = _row_db(_s1n(qc), _s1n(_user()), _s1(0), _s1n(_due_plug()),
                     _s1(_tenant(enabled=True, delay=2)), _upd(),
                     _s1n(_reservation(user_id=99)), _s1(_gateway()))
    _, begin, notify = await _sweep(row_db)
    assert qc.status == QueuedChargeStatus.FAILED
    assert notify.await_args.args[1] == "queued_charge_failed"
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_fails_schedule_when_cpo_disabled_queued_charging():
    qc = _qc(not_before=_ago(seconds=30))
    _, begin, _ = await _sweep(
        _scheduled_start_chain(qc, _due_plug(), tenant=_tenant(enabled=False, delay=2))
    )
    assert qc.status == QueuedChargeStatus.FAILED
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_never_starts_on_a_dead_gateway():
    qc = _qc(not_before=_ago(seconds=30))
    started, begin, _ = await _sweep(
        _scheduled_start_chain(qc, _due_plug(), gateway=_gateway(online=False))
    )
    assert started == 0 and qc.status == QueuedChargeStatus.WAITING
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_outage_row_waits_while_another_driver_is_scheduled():
    qc = _qc()                                     # outage row (not_before None)
    row_db = _row_db(_s1n(qc), _s1n(_user()), _s1(0), _s1n(_due_plug()),
                     _s1(_tenant(delay=2)), _upd(), _s1n(None),
                     _s1n(12),                     # another driver's schedule
                     _s1(_gateway()))              # reached only if the guard fails
    _, begin, _ = await _sweep(row_db)
    assert qc.status == QueuedChargeStatus.WAITING
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_claims_started_before_the_helper_commits():
    """A cancel can land once the helper's commit releases the qc row lock, so
    STARTED must already be what that commit writes."""
    qc = _qc(not_before=_ago(seconds=30))
    seen = {}

    async def begin(*_a, **_kw):
        seen["status"] = qc.status
        return _started_session()

    started, _, _ = await _sweep(_scheduled_start_chain(qc, _due_plug()),
                                 begin=AsyncMock(side_effect=begin))
    assert seen["status"] == QueuedChargeStatus.STARTED
    assert started == 1 and qc.started_session_id == 99


@pytest.mark.asyncio
async def test_sweep_publish_failure_overwrites_the_started_claim():
    qc = _qc(not_before=_ago(seconds=30))
    started, _, notify = await _sweep(
        _scheduled_start_chain(qc, _due_plug()),
        begin=AsyncMock(side_effect=HTTPException(status_code=500, detail="pub")),
    )
    assert started == 0
    assert qc.status == QueuedChargeStatus.FAILED
    assert notify.await_args.args[2] == "Scheduled charge couldn't start"


@pytest.mark.asyncio
async def test_orphan_off_hold_ignores_scheduled_rows():
    """The reconnect OFF is never held for a scheduled row: an OFF to an idle
    plug can't fight the sweep's later ON, and a stale, self-resumed relay
    would otherwise charge unbilled. Only outage rows (not_before IS NULL) are
    considered for the hold."""
    from backend.services.mqtt_manager import MQTTManager

    gw = MagicMock()
    gw.status = GatewayStatus.ONLINE
    mgr = _manager([
        _result_scalar_one_or_none(gw),
        _result_rows([(1, "10.0.0.11", "Bay 1", 0.0)]),
        _result_scalars([]),
        _result_rows([]),                 # the hold query (SQL asserted below)
    ])
    with patch("backend.services.mqtt.status.notify", AsyncMock()):
        await mgr._persist_gateway_status("gw-1", "online")
    db = mgr.db_session_factory.return_value.__aenter__.return_value
    hold_sql = str(db.execute.await_args_list[3].args[0])
    assert "queued_charges.not_before IS NULL" in hold_sql
    assert [c.args[1] for c in mgr.send_plug_command.call_args_list] == [1]
    MQTTManager._instance = None


# --------------------------------------------------------------------------
# [Scheduled start] POST /api/sessions/start contention
# --------------------------------------------------------------------------

def _start_db(plug, scheduled_rows, *tail):
    rows = MagicMock()
    rows.all.return_value = scheduled_rows
    return _db(
        _s1(_user()), _s1(0), _s1n(plug),      # user lock, active count, plug lock
        _upd(), _s1n(None),                    # reservation expiry, no covering booking
        _s1(_gateway()),                       # live gateway
        rows,                                  # WAITING scheduled rows on the plug
        *tail,
    )


def _row(qc_id, user_id):
    r = MagicMock()
    r.id = qc_id
    r.user_id = user_id
    return r


@pytest.mark.asyncio
async def test_start_refused_when_another_driver_scheduled_the_plug():
    from backend.routers.sessions import start_charging_session
    from backend.schemas import SessionStartRequest
    db = _start_db(_plug(powered=True), [_row(3, 77)])
    with patch("backend.routers.sessions.begin_active_session", AsyncMock()) as begin:
        with pytest.raises(HTTPException) as exc:
            await start_charging_session(SessionStartRequest(plug_id=1), _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "plug_scheduled"
    begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_start_now_cancels_own_schedule_only_after_success():
    from backend.routers.sessions import start_charging_session
    from backend.schemas import SessionStartRequest
    session = _started_session()
    session.max_kwh = None
    session.max_duration_seconds = None
    db = _start_db(_plug(powered=True), [_row(3, 5)], _upd(), _s1n(None))
    with patch("backend.routers.sessions.begin_active_session",
               AsyncMock(return_value=session)):
        resp = await start_charging_session(SessionStartRequest(plug_id=1), _user(), db)
    assert resp["status"] == "started"
    cancel_sql = str(db.execute.await_args_list[7].args[0])
    assert cancel_sql.startswith("UPDATE queued_charges")

    # A failed start (publish 500) must leave the schedule alone.
    db = _start_db(_plug(powered=True), [_row(3, 5)])
    with patch("backend.routers.sessions.begin_active_session",
               AsyncMock(side_effect=HTTPException(status_code=500, detail="pub"))):
        with pytest.raises(HTTPException):
            await start_charging_session(SessionStartRequest(plug_id=1), _user(), db)
    assert db.execute.await_count == 7          # no UPDATE after the failure


@pytest.mark.asyncio
async def test_sweep_late_scheduled_start_reports_time_and_rate():
    qc = _qc(not_before=_ago(minutes=20))
    started, _, notify = await _sweep(_scheduled_start_chain(qc, _due_plug()))
    assert started == 1
    assert notify.await_args.args[2] == "Your scheduled charge started late"
    assert "4.00 coins/kWh" in notify.await_args.args[3]


# --- C13: CPO plug edit can clear the per-plug overrides back to "inherit" ---


def _override_plug():
    p = MagicMock()
    p.id = 7
    p.name = "Bay 1"
    p.status = PlugStatus.AVAILABLE
    p.group_id = None
    p.queued_charging_enabled = True
    p.auto_start_delay_min = 5
    return p


async def _put_plug_update(**body):
    from backend.routers.cpo import cpo_update_plug
    from backend.schemas import CpoPlugUpdateRequest

    plug = _override_plug()
    res = MagicMock()
    res.scalar_one_or_none.return_value = plug
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    user = MagicMock(tenant_id=1, id=42, email="cpo@amphive.test")
    await cpo_update_plug(7, CpoPlugUpdateRequest(**body), user, db)
    return plug


@pytest.mark.asyncio
async def test_plug_update_explicit_null_clears_queued_overrides():
    plug = await _put_plug_update(queued_charging_enabled=None, auto_start_delay_min=None)
    assert plug.queued_charging_enabled is None
    assert plug.auto_start_delay_min is None


@pytest.mark.asyncio
async def test_plug_update_omitted_queued_fields_are_left_unchanged():
    plug = await _put_plug_update(name="Renamed")
    assert plug.queued_charging_enabled is True
    assert plug.auto_start_delay_min == 5


@pytest.mark.asyncio
async def test_plug_update_sets_queued_overrides_and_zero_still_clears_delay():
    plug = await _put_plug_update(queued_charging_enabled=False, auto_start_delay_min=10)
    assert plug.queued_charging_enabled is False
    assert plug.auto_start_delay_min == 10
    plug = await _put_plug_update(auto_start_delay_min=0)
    assert plug.auto_start_delay_min is None
