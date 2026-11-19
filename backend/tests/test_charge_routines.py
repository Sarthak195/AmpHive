"""
Recurring weekly charge routines (feature C5): services/routines.py,
routers/routines.py and the reaper hooks.

Two tiers, like test_payouts.py:
1. DB-free: the pure due-ness rule (incl. a tick across local midnight), the
   materialize and settle branches on mocked sessions, the row a routine
   queues compared field by field with the row POST /api/sessions/queue's
   scheduled path creates, and the owner-only API.
2. DB-gated (TEST_DATABASE_URL, CI only): the same rules against real rows,
   including two concurrent ticks queueing exactly one row.
"""
import asyncio
import itertools
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from backend.database.models import PlugStatus, QueuedChargeStatus
from backend.routers.routines import (
    RoutineCreateRequest,
    RoutineUpdateRequest,
    create_routine,
    delete_routine,
    list_routines,
    update_routine,
)
from backend.routers.sessions import queue_charge
from backend.schemas import QueueChargeRequest
from backend.services import routines as svc
from backend.services.session_reaper import SessionReaperService

IST = timezone(timedelta(hours=5, minutes=30))
MONDAY = date(2026, 11, 16)
MON_BIT, TUE_BIT, ALL_DAYS = 1, 2, 127


def test_monday_fixture_is_a_monday():
    assert MONDAY.weekday() == 0
    assert svc.weekday_bit(MONDAY) == MON_BIT
    assert svc.weekday_bit(MONDAY + timedelta(days=6)) == 64


# --------------------------------------------------------------------------
# Pure due-ness
# --------------------------------------------------------------------------

def _ist(d, hh, mm):
    return datetime.combine(d, time(hh, mm), tzinfo=IST).astimezone(timezone.utc)


def test_tick_across_local_midnight_fires_for_yesterday():
    """A Monday 23:58 routine ticked at Tuesday 00:01 local is Monday's
    occurrence: Monday's weekday bit, Monday's date stamped."""
    now = _ist(MONDAY + timedelta(days=1), 0, 1)
    occ = svc.due_occurrence(MON_BIT, time(23, 58), None, IST, now)
    assert occ == (MONDAY, _ist(MONDAY, 23, 58))
    # A Tuesday-only routine is not due: Tuesday's 23:58 is a day away.
    assert svc.due_occurrence(TUE_BIT, time(23, 58), None, IST, now) is None
    # Already stamped for Monday: nothing (the double-tick guard).
    assert svc.due_occurrence(MON_BIT, time(23, 58), MONDAY, IST, now) is None


def test_occurrence_only_inside_the_window():
    start = _ist(MONDAY, 7, 30)
    assert svc.due_occurrence(ALL_DAYS, time(7, 30), None, IST, start - timedelta(seconds=1)) is None
    assert svc.due_occurrence(ALL_DAYS, time(7, 30), None, IST, start)[0] == MONDAY
    late = start + svc.MATERIALIZE_WINDOW
    assert svc.due_occurrence(ALL_DAYS, time(7, 30), None, IST, late) is None
    # Yesterday's stamp does not block today.
    assert svc.due_occurrence(
        ALL_DAYS, time(7, 30), MONDAY - timedelta(days=1), IST, start
    ) == (MONDAY, start)


# --------------------------------------------------------------------------
# Mocks
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


def _s1(v):
    r = MagicMock()
    r.scalar_one.return_value = v
    return r


def _s1n(v):
    r = MagicMock()
    r.scalar_one_or_none.return_value = v
    return r


def _one(v):
    r = MagicMock()
    r.one_or_none.return_value = v
    return r


def _compiling(results):
    """execute() side effect that compiles each statement for Postgres first,
    so a malformed query fails here and not only in production."""
    it = iter(results)

    async def _execute(stmt, *args, **kwargs):
        stmt.compile(dialect=postgresql.dialect())
        return next(it)
    return _execute


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_compiling(results))
    db.add = MagicMock()
    db.begin_nested = MagicMock(return_value=_cm(None))
    return db


def _tenant(enabled=True):
    t = MagicMock()
    t.id = 7
    t.queued_charging_enabled = enabled
    t.auto_start_delay_min = 2
    t.queue_ttl_min = 720
    t.timezone = "Asia/Kolkata"
    return t


def _plug(status=PlugStatus.AVAILABLE):
    p = MagicMock()
    p.id = 1
    p.name = "Bay 1"
    p.gateway_id = "gw-1"
    p.group_id = None
    p.status = status
    p.queued_charging_enabled = None
    p.last_telemetry_at = datetime.now(timezone.utc)
    p.powered_since = datetime.now(timezone.utc)
    return p


def _gateway():
    from backend.database.models import GatewayStatus

    g = MagicMock()
    g.status = GatewayStatus.ONLINE
    g.last_seen_at = datetime.now(timezone.utc)
    g.tenant_id = 7
    return g


def _routine(start=time(7, 30), weekdays=ALL_DAYS, failures=0, enabled=True,
             last_qc=None, auto_stop=False):
    r = MagicMock()
    r.id = 3
    r.user_id = 5
    r.plug_id = 1
    r.tenant_id = 7
    r.weekdays = weekdays
    r.start_time = start
    r.max_kwh = 8.0
    r.max_duration_seconds = 3600
    r.auto_stop_when_full = auto_stop
    r.enabled = enabled
    r.last_materialized_date = None
    r.consecutive_failures = failures
    r.gate_notified = False
    r.last_queued_charge_id = last_qc
    return r


def _live_user():
    u = MagicMock()
    u.is_disabled = False
    u.deleted_at = None
    return u


def _head(routine, tenant=None):
    """lock routine, plug, gateway, tenant: the head of _materialize_one."""
    return (_s1n(routine), _s1(_plug()), _s1(_gateway()), _s1(tenant or _tenant()))


def _patched_zone():
    return patch("backend.services.pricing._zone", lambda _tz: IST)


# --------------------------------------------------------------------------
# Materialize (DB-free)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_materialized_row_equals_what_queue_charge_creates():
    now = datetime.now(timezone.utc)
    nb = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
    # 1. The scheduled path of POST /api/sessions/queue.
    qdb = _db(
        _s1n(_plug()), _s1(_gateway()), _s1(_tenant()),
        _s1n(None), _s1n(None),                     # no booking, no other driver
        _one((Decimal("100"), Decimal("0"))),       # available balance
        _s1(0), _s1n(None),                         # under the cap, no own row
    )
    user = MagicMock()
    user.id = 5
    req = QueueChargeRequest(plug_id=1, not_before=nb, max_kwh=8.0,
                             max_duration_seconds=3600, auto_stop_when_full=True)
    with _patched_zone(), \
         patch("backend.services.pricing.zone_name", lambda _tz: "Asia/Kolkata"), \
         patch("backend.services.notifications.notify", AsyncMock()):
        await queue_charge(req, user, qdb)
    expected = qdb.add.call_args.args[0]

    # 2. A routine due at the same local instant.
    routine = _routine(start=nb.astimezone(IST).time(), auto_stop=True)
    mdb = _db(*_head(routine), _one(_live_user()), _s1n(None), _s1n(None), _s1(0))
    with _patched_zone(), patch("backend.services.notifications.notify", AsyncMock()):
        queued = await svc.materialize_routines(
            _factory(_scan([routine]), mdb), now=nb + timedelta(minutes=1)
        )
    assert queued == 1
    got = mdb.add.call_args.args[0]
    for field in ("tenant_id", "user_id", "plug_id", "max_kwh", "max_duration_seconds",
                  "auto_stop_when_full", "status", "not_before", "expires_at"):
        assert getattr(got, field) == getattr(expected, field), field
    assert routine.last_materialized_date == nb.astimezone(IST).date()

    # A second tick sees the stamp in its scan and queues nothing.
    with _patched_zone():
        again = await svc.materialize_routines(
            _factory(_scan([routine])), now=nb + timedelta(minutes=2)
        )
    assert again == 0


def _scan(routines):
    rows = []
    for r in routines:
        row = MagicMock()
        row.id = r.id
        row.weekdays = r.weekdays
        row.start_time = r.start_time
        row.last_materialized_date = r.last_materialized_date
        row.timezone = "Asia/Kolkata"
        rows.append(row)
    res = MagicMock()
    res.all.return_value = rows
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_compiling([res]))
    return db


@pytest.mark.asyncio
async def test_locked_or_disabled_routine_queues_nothing():
    """The row lock re-checks enabled with SKIP LOCKED: a routine turned off
    since the scan (or held by another tick) is left alone."""
    routine = _routine()
    now = _ist(MONDAY, 7, 31)
    mdb = _db(_s1n(None))
    with _patched_zone():
        queued = await svc.materialize_routines(_factory(_scan([routine]), mdb), now=now)
    assert queued == 0
    mdb.add.assert_not_called()


@pytest.mark.asyncio
async def test_existing_waiting_row_is_skipped_and_stamped():
    routine = _routine()
    now = _ist(MONDAY, 7, 31)
    mdb = _db(*_head(routine), _one(_live_user()), _s1n(42))   # own WAITING row
    with _patched_zone(), patch("backend.services.notifications.notify", AsyncMock()) as notify:
        queued = await svc.materialize_routines(_factory(_scan([routine]), mdb), now=now)
    assert queued == 0
    mdb.add.assert_not_called()
    notify.assert_not_awaited()
    assert routine.last_materialized_date == MONDAY
    assert routine.consecutive_failures == 0


@pytest.mark.asyncio
async def test_queued_gate_off_skips_and_tells_the_driver_once():
    """The routine stays on through a gate outage: each occurrence is skipped
    and stamped, the driver hears about it once, and the next queued row
    re-arms the notice."""
    routine = _routine()
    with _patched_zone(), patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for day in range(2):
            mdb = _db(*_head(routine, _tenant(enabled=False)), _one(_live_user()))
            queued = await svc.materialize_routines(
                _factory(_scan([routine]), mdb), now=_ist(MONDAY + timedelta(days=day), 7, 31)
            )
            assert queued == 0
            mdb.add.assert_not_called()
        assert routine.enabled is True
        assert routine.last_materialized_date == MONDAY + timedelta(days=1)
        notify.assert_awaited_once()
        assert notify.await_args.args[1] == "routine_skipped"

        # Gate back on: the routine queues again and the notice re-arms.
        mdb = _db(*_head(routine), _one(_live_user()), _s1n(None), _s1n(None), _s1(0))
        assert await svc.materialize_routines(
            _factory(_scan([routine]), mdb), now=_ist(MONDAY + timedelta(days=2), 7, 31)
        ) == 1
    assert routine.gate_notified is False


@pytest.mark.asyncio
async def test_second_gate_outage_is_noticed_even_when_nothing_queued_between():
    """The gate coming back on re-arms the notice on the next materialize
    pass, even when that occurrence is skipped for another reason (here the
    driver's own WAITING row), so a second outage is noticed too."""
    routine = _routine()
    with _patched_zone(), patch("backend.services.notifications.notify", AsyncMock()) as notify:
        # Day 0: gate off, the driver is told.
        mdb = _db(*_head(routine, _tenant(enabled=False)), _one(_live_user()))
        await svc.materialize_routines(_factory(_scan([routine]), mdb), now=_ist(MONDAY, 7, 31))
        assert routine.gate_notified is True
        # Day 1: gate on, but the occurrence is skipped (own WAITING row).
        mdb = _db(*_head(routine), _one(_live_user()), _s1n(42))
        assert await svc.materialize_routines(
            _factory(_scan([routine]), mdb), now=_ist(MONDAY + timedelta(days=1), 7, 31)
        ) == 0
        mdb.add.assert_not_called()
        assert routine.gate_notified is False
        # Day 2: gate off again, a second notice.
        mdb = _db(*_head(routine, _tenant(enabled=False)), _one(_live_user()))
        await svc.materialize_routines(
            _factory(_scan([routine]), mdb), now=_ist(MONDAY + timedelta(days=2), 7, 31)
        )
    assert notify.await_count == 2
    assert [c.args[1] for c in notify.await_args_list] == ["routine_skipped", "routine_skipped"]
    assert routine.gate_notified is True


@pytest.mark.asyncio
async def test_materialize_locks_the_plug_like_the_scheduled_path():
    """queue_charge's scheduled path takes the plug FOR UPDATE; the routine's
    other-driver check must run under the same lock."""
    routine = _routine()
    mdb = _db(*_head(routine), _one(_live_user()), _s1n(42))
    with _patched_zone():
        await svc.materialize_routines(_factory(_scan([routine]), mdb), now=_ist(MONDAY, 7, 31))
    plug_stmt = mdb.execute.call_args_list[1].args[0]
    sql = str(plug_stmt.compile(dialect=postgresql.dialect()))
    assert sql.startswith("SELECT plugs.") and sql.endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_closed_account_never_queues():
    routine = _routine()
    closed = _live_user()
    closed.deleted_at = datetime.now(timezone.utc)
    mdb = _db(*_head(routine), _one(closed))
    with _patched_zone():
        queued = await svc.materialize_routines(
            _factory(_scan([routine]), mdb), now=_ist(MONDAY, 7, 31)
        )
    assert queued == 0
    mdb.add.assert_not_called()


@pytest.mark.asyncio
async def test_other_drivers_waiting_row_counts_as_a_failed_occurrence():
    routine = _routine(failures=2)
    mdb = _db(*_head(routine), _one(_live_user()), _s1n(None), _s1n(99))
    with _patched_zone(), patch("backend.services.notifications.notify", AsyncMock()) as notify:
        await svc.materialize_routines(_factory(_scan([routine]), mdb), now=_ist(MONDAY, 7, 31))
    mdb.add.assert_not_called()
    assert routine.enabled is False
    assert notify.await_args.args[1] == "routine_disabled"


# --------------------------------------------------------------------------
# Settle (DB-free)
# --------------------------------------------------------------------------

def _qc(status, session_id=None):
    qc = MagicMock()
    qc.status = status
    qc.started_session_id = session_id
    return qc


def _settle_dbs(routine, qc, with_name=True):
    ids = MagicMock()
    ids.scalars.return_value.all.return_value = [routine.id]
    scan = AsyncMock()
    scan.execute = AsyncMock(side_effect=_compiling([ids]))
    results = [_s1n(routine), _s1n(qc)]
    if with_name:
        results.append(_s1("Bay 1"))
    return scan, _db(*results)


@pytest.mark.asyncio
async def test_third_consecutive_failure_disables_the_routine():
    routine = _routine(failures=2, last_qc=11)
    scan, db = _settle_dbs(routine, _qc(QueuedChargeStatus.EXPIRED))
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        await SessionReaperService(_factory(scan, db), AsyncMock()).settle_routines_once()
    assert routine.consecutive_failures == 3
    assert routine.enabled is False
    assert routine.last_queued_charge_id is None
    assert notify.await_args.args[1] == "routine_disabled"


@pytest.mark.asyncio
async def test_a_single_failure_only_counts():
    routine = _routine(failures=0, last_qc=11)
    scan, db = _settle_dbs(routine, _qc(QueuedChargeStatus.FAILED))
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        await svc.settle_routines(_factory(scan, db))
    assert routine.consecutive_failures == 1
    assert routine.enabled is True
    notify.assert_not_awaited()   # the reaper already told the driver


@pytest.mark.asyncio
async def test_start_resets_failures_and_sends_routine_started():
    routine = _routine(failures=2, last_qc=11)
    scan, db = _settle_dbs(routine, _qc(QueuedChargeStatus.STARTED, session_id=77))
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        await svc.settle_routines(_factory(scan, db))
    assert routine.consecutive_failures == 0
    assert routine.last_queued_charge_id is None
    assert notify.await_args.args[1] == "routine_started"
    assert notify.await_args.kwargs["session_id"] == 77


@pytest.mark.asyncio
async def test_closed_plug_occurrence_waits_then_expiry_counts_one_failure():
    """[C5 + C4] A routine row on a plug outside its opening hours stays
    WAITING (no start, no notice, no failure counted); if it is still closed
    when the row's TTL lapses, the sweep expires it and settle counts exactly
    one failed occurrence. The sweep's closed branch commits and moves on, so
    the row is only looked at once per reaper tick."""
    from backend.tests import test_plug_hours as ph

    qc = ph._qc(not_before=datetime.now(timezone.utc) - timedelta(seconds=30))
    plug = ph._plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    plug.opening_hours = ph._closed_today_hours()
    plug.max_session_minutes = None
    row_db = ph._row_db(
        ph._s1n(qc), ph._s1n(ph._user()), ph._s1(0), ph._s1n(plug),
        ph._s1(ph._tenant(enabled=True, delay=2)),
        ph._upd(), ph._s1n(None), ph._s1(ph._gateway()),
    )
    with patch("backend.services.session_start.begin_active_session", AsyncMock()) as begin,          patch("backend.services.notifications.notify", AsyncMock()) as notify:
        await SessionReaperService(
            ph._factory(ph._scan_db([1]), row_db), AsyncMock()
        ).reap_queued_starts_once()
    assert qc.status == QueuedChargeStatus.WAITING
    begin.assert_not_awaited()
    notify.assert_not_awaited()

    routine = _routine(failures=0, last_qc=11)
    scan, db = _settle_dbs(routine, qc, with_name=False)
    await svc.settle_routines(_factory(scan, db))
    assert routine.consecutive_failures == 0
    assert routine.last_queued_charge_id == 11          # still undecided

    qc.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with patch("backend.services.notifications.notify", AsyncMock()):
        await SessionReaperService(
            ph._factory(ph._scan_db([1]), ph._row_db(ph._s1n(qc))), AsyncMock()
        ).reap_queued_starts_once()
    assert qc.status == QueuedChargeStatus.EXPIRED

    scan, db = _settle_dbs(routine, qc)
    with patch("backend.services.notifications.notify", AsyncMock()):
        await svc.settle_routines(_factory(scan, db))
    assert routine.consecutive_failures == 1
    assert routine.enabled is True
    assert routine.last_queued_charge_id is None


@pytest.mark.asyncio
async def test_waiting_and_cancelled_rows_do_not_count():
    routine = _routine(failures=1, last_qc=11)
    scan, db = _settle_dbs(routine, _qc(QueuedChargeStatus.WAITING), with_name=False)
    await svc.settle_routines(_factory(scan, db))
    assert routine.last_queued_charge_id == 11          # still undecided
    scan, db = _settle_dbs(routine, _qc(QueuedChargeStatus.CANCELLED), with_name=False)
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        await svc.settle_routines(_factory(scan, db))
    assert routine.consecutive_failures == 1
    assert routine.last_queued_charge_id is None
    notify.assert_not_awaited()


# --------------------------------------------------------------------------
# API (DB-free)
# --------------------------------------------------------------------------

def _driver():
    u = MagicMock()
    u.id = 5
    return u


@pytest.mark.asyncio
async def test_list_returns_own_rows_with_the_tenant_zone():
    res = MagicMock()
    res.all.return_value = [(_routine(), "Bay 1", "Asia/Kolkata")]
    with patch("backend.services.pricing.zone_name", lambda tz: tz):
        body = await list_routines(_driver(), _db(res))
    assert body["max_routines"] == svc.MAX_ROUTINES_PER_USER
    row = body["routines"][0]
    assert row["plug_name"] == "Bay 1" and row["start_time"] == "07:30"
    assert row["tenant_timezone"] == "Asia/Kolkata"
    assert not {"local_ip", "gateway_id", "email"} & set(row)


def test_create_requires_a_limit_and_valid_days():
    with pytest.raises(ValidationError):
        RoutineCreateRequest(plug_id=1, weekdays=ALL_DAYS, start_time="07:30")
    with pytest.raises(ValidationError):
        RoutineCreateRequest(plug_id=1, weekdays=0, start_time="07:30", max_kwh=5)
    with pytest.raises(ValidationError):
        RoutineCreateRequest(plug_id=1, weekdays=128, start_time="07:30", max_kwh=5)
    with pytest.raises(ValidationError):
        RoutineCreateRequest(plug_id=1, weekdays=1, start_time="07:30+05:30", max_kwh=5)
    ok = RoutineCreateRequest(plug_id=1, weekdays=1, start_time="07:30:45", max_kwh=5)
    assert ok.start_time == time(7, 30)


@pytest.mark.asyncio
async def test_create_refuses_a_sixth_routine():
    db = _db(MagicMock(), _s1(svc.MAX_ROUTINES_PER_USER))
    with pytest.raises(HTTPException) as exc:
        await create_routine(
            RoutineCreateRequest(plug_id=1, weekdays=1, start_time="07:30", max_kwh=5),
            _driver(), db,
        )
    assert exc.value.detail["code"] == "routine_limit"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_refused_while_scheduled_charging_is_off():
    grouped = _plug()
    grouped.group_id = 9
    private = MagicMock()
    private.id, private.is_public = 9, False
    member = MagicMock()
    db = _db(MagicMock(), _s1(0), _one((grouped, _tenant(enabled=False))),
             _s1n(private), _s1n(member))
    with pytest.raises(HTTPException) as exc:
        await create_routine(
            RoutineCreateRequest(plug_id=1, weekdays=1, start_time="07:30", max_kwh=5),
            _driver(), db,
        )
    assert exc.value.detail["code"] == "queue_disabled"
    db.add.assert_not_called()


def _just_started():
    """A start time one to two minutes ago (IST wall clock) and its local date."""
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    local = start.astimezone(IST)
    return local.time().replace(second=0, microsecond=0), local.date()


@pytest.mark.asyncio
async def test_create_after_todays_start_waits_for_the_next_occurrence():
    """Saving a routine just after its start time must not queue a row whose
    not_before is already past (the scheduled path requires now < not_before)."""
    start, today = _just_started()
    db = _db(MagicMock(), _s1(0), _one((_plug(), _tenant())))
    with _patched_zone():
        await create_routine(
            RoutineCreateRequest(plug_id=1, weekdays=ALL_DAYS, start_time=start, max_kwh=5),
            _driver(), db,
        )
    routine = db.add.call_args.args[0]
    assert routine.last_materialized_date == today
    assert svc.due_occurrence(ALL_DAYS, start, routine.last_materialized_date, IST,
                              datetime.now(timezone.utc)) is None

    later = (datetime.now(timezone.utc) + timedelta(hours=1)).astimezone(IST).time()
    db = _db(MagicMock(), _s1(0), _one((_plug(), _tenant())))
    with _patched_zone():
        await create_routine(
            RoutineCreateRequest(plug_id=1, weekdays=ALL_DAYS, start_time=later, max_kwh=5),
            _driver(), db,
        )
    assert db.add.call_args.args[0].last_materialized_date is None


@pytest.mark.asyncio
async def test_reenable_or_retime_after_todays_start_waits_for_the_next_occurrence():
    start, today = _just_started()
    routine = _routine(start=start, enabled=False)
    with _patched_zone():
        await update_routine(3, RoutineUpdateRequest(enabled=True), _driver(),
                             _db(_s1n(routine), _one((_plug(), _tenant()))))
    assert routine.last_materialized_date == today

    routine = _routine()
    with _patched_zone():
        await update_routine(3, RoutineUpdateRequest(start_time=start), _driver(),
                             _db(_s1n(routine), _one((_plug(), _tenant()))))
    assert routine.last_materialized_date == today

    # A limits-only edit leaves a due occurrence alone.
    routine = _routine(start=start)
    with _patched_zone():
        await update_routine(3, RoutineUpdateRequest(max_kwh=6.0), _driver(),
                             _db(_s1n(routine), _one((_plug(), _tenant()))))
    assert routine.last_materialized_date is None


@pytest.mark.asyncio
async def test_other_drivers_routine_is_404():
    with pytest.raises(HTTPException) as exc:
        await update_routine(3, RoutineUpdateRequest(enabled=False), _driver(), _db(_s1n(None)))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await delete_routine(3, _driver(), _db(_s1n(None)))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_cannot_remove_the_last_limit():
    routine = _routine()
    routine.max_kwh = None
    db = _db(_s1n(routine), _one((_plug(), _tenant())))
    with pytest.raises(HTTPException) as exc:
        await update_routine(3, RoutineUpdateRequest(max_duration_seconds=None), _driver(), db)
    assert exc.value.status_code == 422
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reenable_resets_failures_and_disable_withdraws_the_pending_row():
    routine = _routine(enabled=False, failures=3)
    db = _db(_s1n(routine), _one((_plug(), _tenant())))
    await update_routine(3, RoutineUpdateRequest(enabled=True), _driver(), db)
    assert routine.enabled is True and routine.consecutive_failures == 0

    routine = _routine(enabled=True, last_qc=11)
    db = _db(_s1n(routine), _one((_plug(), _tenant())), MagicMock())
    await update_routine(3, RoutineUpdateRequest(enabled=False), _driver(), db)
    assert routine.enabled is False
    cancel = str(db.execute.call_args_list[2].args[0])
    assert cancel.startswith("UPDATE queued_charges")


def _weekday_hours_plug():
    """Open 08:00-20:00 Mon-Fri (tenant wall clock), closed at weekends."""
    plug = _plug()
    plug.opening_hours = [{"days": 31, "start": 480, "end": 1200}]
    return plug


@pytest.mark.asyncio
async def test_create_refuses_a_start_time_the_plug_is_closed_at():
    """[Plug hours] Every occurrence at a closed time would expire, count a
    failure and, on the third, turn the routine off."""
    db = _db(MagicMock(), _s1(0), _one((_weekday_hours_plug(), _tenant())))
    with pytest.raises(HTTPException) as exc:
        await create_routine(
            RoutineCreateRequest(plug_id=1, weekdays=ALL_DAYS, start_time="09:00", max_kwh=5),
            _driver(), db,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "plug_closed"
    assert "Sat, Sun" in exc.value.detail["message"]
    db.add.assert_not_called()

    db = _db(MagicMock(), _s1(0), _one((_weekday_hours_plug(), _tenant())))
    with _patched_zone():
        await create_routine(
            RoutineCreateRequest(plug_id=1, weekdays=31, start_time="09:00", max_kwh=5),
            _driver(), db,
        )
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_update_checks_hours_only_when_the_time_days_or_enabled_change():
    routine = _routine(start=time(9, 0), weekdays=31)
    db = _db(_s1n(routine), _one((_weekday_hours_plug(), _tenant())))
    with pytest.raises(HTTPException) as exc:
        await update_routine(3, RoutineUpdateRequest(start_time="21:00"), _driver(), db)
    assert exc.value.detail["code"] == "plug_closed"
    db.commit.assert_not_awaited()

    # Hours edited since: a limits-only edit or turning it off still saves.
    routine = _routine(start=time(21, 0), weekdays=31)
    db = _db(_s1n(routine), _one((_weekday_hours_plug(), _tenant())))
    await update_routine(3, RoutineUpdateRequest(max_kwh=6.0), _driver(), db)
    db.commit.assert_awaited()
    routine = _routine(start=time(21, 0), weekdays=31)
    db = _db(_s1n(routine), _one((_weekday_hours_plug(), _tenant())), MagicMock())
    await update_routine(3, RoutineUpdateRequest(enabled=False), _driver(), db)
    assert routine.enabled is False

    # Re-enabling it is checked.
    routine = _routine(start=time(21, 0), weekdays=31, enabled=False)
    db = _db(_s1n(routine), _one((_weekday_hours_plug(), _tenant())))
    with pytest.raises(HTTPException) as exc:
        await update_routine(3, RoutineUpdateRequest(enabled=True), _driver(), db)
    assert exc.value.detail["code"] == "plug_closed"


def test_routine_write_routes_are_in_the_viewer_walk():
    """The cpo_viewer route walk (test_cpo_viewer_role.py) asserts 403 on
    every write route it reaches; pin that it reaches these."""
    from backend.tests.test_cpo_viewer_role import _write_routes

    walked = {(m, p) for m, p, _ in _write_routes()}
    assert {
        ("POST", "/api/me/routines"),
        ("PATCH", "/api/me/routines/{routine_id}"),
        ("DELETE", "/api/me/routines/{routine_id}"),
    } <= walked


# ===========================================================================
# Tier 2: DB-gated (real Postgres, CI only)
# ===========================================================================

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)
ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status", "queued_charge_status",
]
_n = itertools.count(1)


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


async def _seed(factory, *, gate=True, weekdays=MON_BIT, start=time(23, 58), enabled=True):
    from backend.database.models import (
        ChargeRoutine,
        Gateway,
        GatewayStatus,
        Plug,
        Tenant,
        User,
        UserRole,
    )

    n = next(_n)
    async with factory() as db:
        tenant = Tenant(name=f"T{n}", timezone="Asia/Kolkata", queued_charging_enabled=gate)
        db.add(tenant)
        await db.flush()
        driver = User(email=f"d{n}@example.com", hashed_password="x",
                      full_name="Driver", role=UserRole.DRIVER)
        gw = Gateway(id=f"gw-r{n}", tenant_id=tenant.id, name="GW",
                     vpn_ip=f"10.9.0.{n}", status=GatewayStatus.ONLINE)
        db.add_all([driver, gw])
        await db.flush()
        plug = Plug(gateway_id=gw.id, name="Bay", local_ip="10.0.1.1",
                    status=PlugStatus.AVAILABLE)
        db.add(plug)
        await db.flush()
        routine = ChargeRoutine(user_id=driver.id, plug_id=plug.id, tenant_id=tenant.id,
                                weekdays=weekdays, start_time=start, max_kwh=8.0,
                                enabled=enabled)
        db.add(routine)
        await db.commit()
        return routine.id, driver.id, plug.id


async def _rows(factory, user_id):
    from sqlalchemy import select

    from backend.database.models import QueuedCharge

    async with factory() as db:
        return list((await db.execute(
            select(QueuedCharge).where(QueuedCharge.user_id == user_id)
        )).scalars().all())


async def _routine_row(factory, routine_id):
    from sqlalchemy import select

    from backend.database.models import ChargeRoutine

    async with factory() as db:
        return (await db.execute(
            select(ChargeRoutine).where(ChargeRoutine.id == routine_id)
        )).scalar_one()


TUE_0001 = _ist(MONDAY + timedelta(days=1), 0, 1)


@requires_db
@pytest.mark.asyncio
async def test_db_tick_across_midnight_and_double_tick_queue_one_row(factory):
    rid, uid, _ = await _seed(factory)
    with _patched_zone(), patch("backend.services.notifications.notify", AsyncMock()):
        results = await asyncio.gather(
            svc.materialize_routines(factory, now=TUE_0001),
            svc.materialize_routines(factory, now=TUE_0001),
        )
        again = await svc.materialize_routines(factory, now=TUE_0001 + timedelta(minutes=1))
    assert sum(results) == 1 and again == 0
    rows = await _rows(factory, uid)
    assert len(rows) == 1
    assert rows[0].status == QueuedChargeStatus.WAITING
    assert rows[0].not_before == _ist(MONDAY, 23, 58)
    routine = await _routine_row(factory, rid)
    assert routine.last_materialized_date == MONDAY
    assert routine.last_queued_charge_id == rows[0].id


@requires_db
@pytest.mark.asyncio
async def test_db_disabled_routine_and_gate_off(factory):
    _, uid_off, _ = await _seed(factory, enabled=False)
    rid_gate, uid_gate, _ = await _seed(factory, gate=False)
    with _patched_zone(), patch("backend.services.notifications.notify", AsyncMock()) as notify:
        assert await svc.materialize_routines(factory, now=TUE_0001) == 0
        assert await svc.materialize_routines(factory, now=TUE_0001 + timedelta(days=7)) == 0
    assert await _rows(factory, uid_off) == []
    assert await _rows(factory, uid_gate) == []
    gated = await _routine_row(factory, rid_gate)
    assert gated.enabled is True and gated.gate_notified is True
    assert [c.args[1] for c in notify.await_args_list] == ["routine_skipped"]


@requires_db
@pytest.mark.asyncio
async def test_db_existing_waiting_row_skips_and_stamps(factory):
    from backend.database.models import QueuedCharge

    rid, uid, pid = await _seed(factory)
    async with factory() as db:
        db.add(QueuedCharge(tenant_id=(await _routine_row(factory, rid)).tenant_id,
                            user_id=uid, plug_id=pid, max_kwh=5.0,
                            status=QueuedChargeStatus.WAITING,
                            expires_at=TUE_0001 + timedelta(hours=1)))
        await db.commit()
    with _patched_zone():
        assert await svc.materialize_routines(factory, now=TUE_0001) == 0
    assert len(await _rows(factory, uid)) == 1
    assert (await _routine_row(factory, rid)).last_materialized_date == MONDAY


@requires_db
@pytest.mark.asyncio
async def test_db_three_failed_occurrences_disable_the_routine(factory):
    from sqlalchemy import update

    from backend.database.models import QueuedCharge

    rid, uid, _ = await _seed(factory, weekdays=ALL_DAYS)
    with _patched_zone(), patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for day in range(3):
            assert await svc.materialize_routines(
                factory, now=TUE_0001 + timedelta(days=day)
            ) == 1
            async with factory() as db:   # the reaper refused it
                await db.execute(update(QueuedCharge).where(
                    QueuedCharge.status == QueuedChargeStatus.WAITING
                ).values(status=QueuedChargeStatus.FAILED))
                await db.commit()
            await svc.settle_routines(factory)
        assert await svc.materialize_routines(factory, now=TUE_0001 + timedelta(days=3)) == 0
    routine = await _routine_row(factory, rid)
    assert routine.enabled is False and routine.consecutive_failures == 3
    assert [c.args[1] for c in notify.await_args_list] == ["routine_disabled"]
    assert len(await _rows(factory, uid)) == 3
