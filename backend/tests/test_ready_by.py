"""DB-free tests for the "ready by T" planner (F6R, services/ready_by.py and
POST /api/sessions/queue/ready-by). The planner only picks a window; the row is
created by the existing scheduled path, so these also pin that equivalence."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.routers.sessions import queue_charge, queue_ready_by
from backend.schemas import QueueChargeRequest, ReadyByRequest
from backend.services import ready_by as rb
from backend.services.ready_by import choose_window, plan_ready_by, required_minutes
from backend.tests.test_queued_charge import (
    _db,
    _gateway,
    _one,
    _plug,
    _s1,
    _s1n,
    _tenant,
    _user,
)

IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: no tzdata on CI Windows boxes
FLAT = Decimal("10.00")


def _slot(start, end, price, mask=127):
    return SimpleNamespace(
        start_min=start, end_min=end, price_per_kwh=Decimal(str(price)), days_mask=mask
    )


def _at(h, mi=0, day=17):  # 2026-11-17 is a Tuesday
    return datetime(2026, 11, day, h, mi, tzinfo=IST)


# Night rate wrapping midnight is two slots: [22:00,24:00) + [00:00,06:00).
NIGHT = [_slot(1320, 1440, 4), _slot(0, 360, 4)]
PEAK = [_slot(1080, 1320, 15)]  # 18:00-22:00 at 15, flat 10 otherwise


@pytest.fixture(autouse=True)
def _ist_zone():
    with patch("backend.services.ready_by._zone", lambda _tz: IST), \
         patch("backend.services.plug_hours._zone", lambda _tz: IST):
        yield


def _plan(slots, ready_by, minutes=120, now=None, plug=None):
    return choose_window(
        plug=plug if plug is not None else _plug(powered=True),
        tz_name="Asia/Kolkata", slots=slots, base_rate=FLAT, power_w=2000.0,
        duration_min=minutes, now=now or _at(20), ready_by=ready_by,
    )


def _unreachable(**kw):
    with pytest.raises(HTTPException) as exc:
        _plan(**kw)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ready_by_unreachable"
    return exc.value.detail


# --- window choice ---------------------------------------------------------

def test_picks_the_cheap_night_window_across_the_tariff_change():
    # 2 h needed, ready by 08:00: the night rate starts at 22:00 and every
    # start up to 04:00 costs the same, so the earliest one wins.
    plan = _plan(NIGHT, _at(8, day=18))
    assert plan["start"] == _at(22)
    assert plan["finish"] == _at(0, day=18)
    assert plan["cost"] == Decimal("16.00")  # 4 kWh at 4


def test_window_straddling_the_change_is_priced_per_slot():
    # Ready by 23:30 forces the run to end by then: the cheapest start is the
    # latest allowed (21:30), 30 min at 10 then 90 min at 4, and not 20:01.
    plan = _plan(NIGHT, _at(23, 30))
    assert plan["start"] == _at(21, 30)
    assert plan["cost"] == Decimal("10.00") + Decimal("12.00")  # 1 kWh at 10, 3 kWh at 4
    assert plan["rate_changes"] == [(_at(22), Decimal("4.00"))]


def test_a_peak_is_avoided_by_waiting_it_out():
    plan = _plan(PEAK, _at(2, day=18), now=_at(17))
    assert plan["start"] == _at(22)  # peak 18-22 skipped: flat 10 beats 15


def test_flat_tariff_starts_as_soon_as_possible():
    plan = _plan([], _at(23), now=_at(20))
    assert plan["start"] == _at(20, 1)  # now + 60 s rounded up to the minute


# --- deadline --------------------------------------------------------------

def test_the_run_never_ends_after_the_deadline():
    # Ready by 22:30 with 1 h needed: the latest start (21:30) is half in the
    # cheap slot, and it finishes exactly at the deadline, never after it.
    plan = _plan(NIGHT, _at(22, 30), minutes=60)
    assert plan["start"] == _at(21, 30)
    assert plan["finish"] == _at(22, 30)
    # The cheap window opens at 22:00, after this deadline: it is not used.
    plan = _plan(NIGHT, _at(21, 30), minutes=60)
    assert plan["start"] == _at(20, 1)
    assert plan["finish"] <= _at(21, 30)


def test_deadline_exactly_fits():
    plan = _plan([], _at(22, 1), now=_at(20))
    assert plan["start"] == _at(20, 1) and plan["finish"] == _at(22, 1)


def test_impossible_deadline_reports_the_earliest_finish():
    detail = _unreachable(slots=NIGHT, ready_by=_at(21, 0), minutes=180)
    assert detail["earliest_finish_at"] == _at(23, 1).isoformat()
    assert "23:01" in detail["message"]


# --- opening hours ---------------------------------------------------------

def _hours(plug, start, end, mask=127, max_session=None):
    plug.opening_hours = [{"days": mask, "start": start, "end": end}]
    plug.max_session_minutes = max_session
    return plug


def test_closed_hours_push_the_start_to_opening_time():
    plug = _hours(_plug(powered=True), 9 * 60, 17 * 60)
    plan = _plan([], _at(12, day=18), plug=plug)  # now 20:00, closed until 09:00
    assert plan["start"] == _at(9, day=18)


def test_closed_hours_make_an_early_deadline_unreachable():
    plug = _hours(_plug(powered=True), 9 * 60, 17 * 60)
    detail = _unreachable(slots=[], ready_by=_at(10, day=18), plug=plug)
    assert detail["earliest_finish_at"] == _at(11, day=18).isoformat()


def test_a_run_must_fit_before_closing():
    # Open 09:00-17:00; at 16:00 only 1 h remains, so a 2 h run starts tomorrow.
    plug = _hours(_plug(powered=True), 9 * 60, 17 * 60)
    plan = _plan([], _at(12, day=18), now=_at(16), plug=plug)
    assert plan["start"] == _at(9, day=18)


def test_max_session_minutes_shorter_than_the_charge_is_refused():
    plug = _hours(_plug(powered=True), 0, 1440, max_session=60)
    detail = _unreachable(slots=[], ready_by=_at(23), plug=plug)
    assert "no opening long enough" in detail["message"]
    assert "earliest_finish_at" not in detail


# --- duration maths --------------------------------------------------------

def test_required_minutes_takes_the_limit_that_binds_first():
    assert required_minutes(2000.0, 4.0, None) == 120   # 4 kWh at 2 kW
    assert required_minutes(2000.0, None, 5400) == 90
    assert required_minutes(2000.0, 4.0, 3600) == 60    # the shorter wins
    assert required_minutes(2300.0, 1.0, None) == 27    # 26.09 min rounds up


# --- plan_ready_by glue ----------------------------------------------------

def _glue_patches():
    return (
        patch.object(rb, "resolve_power_basis", AsyncMock(return_value=(2000.0, "rated_power"))),
        patch.object(rb, "_resolve_tariff_and_tz", AsyncMock(return_value=(None, "Asia/Kolkata"))),
    )


@pytest.mark.asyncio
async def test_plan_requires_a_limit():
    with pytest.raises(HTTPException) as exc:
        await plan_ready_by(AsyncMock(), _plug(powered=True), 5,
                            datetime.now(timezone.utc) + timedelta(hours=3), None, None)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "ready_by_needs_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [timedelta(minutes=-5), timedelta(hours=25)])
async def test_plan_rejects_a_deadline_outside_the_next_24_hours(offset):
    with pytest.raises(HTTPException) as exc:
        await plan_ready_by(AsyncMock(), _plug(powered=True), 5,
                            datetime.now(timezone.utc) + offset, 4.0, None)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "ready_by_out_of_range"


@pytest.mark.asyncio
async def test_plan_caps_the_duration_at_the_deadline():
    ready_by = datetime.now(timezone.utc) + timedelta(hours=6)
    p1, p2 = _glue_patches()
    with p1, p2:
        plan = await plan_ready_by(AsyncMock(), _plug(powered=True), 5, ready_by, 4.0, None)
    assert plan["duration_min"] == 120 and plan["energy_kwh"] == 4.0
    # An energy-only target still stops by the deadline if the car draws less.
    assert plan["max_duration_seconds"] == int((ready_by - plan["not_before"]).total_seconds())
    assert plan["not_before"] + timedelta(minutes=120) <= ready_by

    p1, p2 = _glue_patches()
    with p1, p2:
        plan = await plan_ready_by(AsyncMock(), _plug(powered=True), 5, ready_by, None, 3600)
    assert plan["max_duration_seconds"] == 3600  # the driver's own, shorter limit


def test_request_rejects_a_naive_deadline():
    with pytest.raises(ValidationError):
        ReadyByRequest(plug_id=1, ready_by=datetime(2026, 11, 17, 22, 0), max_kwh=3)


# --- the created row is the scheduled path's row ---------------------------

@pytest.mark.asyncio
async def test_created_row_equals_the_scheduled_path_row():
    now = datetime.now(timezone.utc)
    nb = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
    plan = {
        "not_before": nb, "max_duration_seconds": 9000,
        "finish_at": nb + timedelta(minutes=120), "cost_coins": "16.00",
        "ready_by": nb + timedelta(hours=3),
    }

    def queue_dbs():
        return _db(
            _s1n(_plug(powered=True)), _s1(_gateway()), _s1(_tenant(enabled=True, ttl=720)),
            _s1n(None),                              # no overlapping booking
            _s1n(None),                              # no other driver's row
            _one((Decimal("100"), Decimal("0"))),    # available balance
            _s1(0),                                  # under the cap
            _s1n(None),                              # no own WAITING row
        )

    direct_db = queue_dbs()
    with patch("backend.services.notifications.notify", AsyncMock()):
        await queue_charge(
            QueueChargeRequest(plug_id=1, max_kwh=4.0, max_duration_seconds=9000, not_before=nb),
            _user(), direct_db,
        )
    direct = direct_db.add.call_args.args[0]

    # The route: plug lookup first, then the planner, then the same queue path.
    route_db = _db(_s1n(_plug(powered=True)), *queue_dbs().execute.side_effect)
    with patch("backend.services.ready_by.plan_ready_by", AsyncMock(return_value=plan)), \
         patch("backend.services.notifications.notify", AsyncMock()):
        body = await queue_ready_by(
            ReadyByRequest(plug_id=1, ready_by=plan["ready_by"], max_kwh=4.0), _user(), route_db
        )
    made = route_db.add.call_args.args[0]

    for field in ("tenant_id", "user_id", "plug_id", "max_kwh", "max_duration_seconds",
                  "auto_stop_when_full", "status", "not_before"):
        assert getattr(made, field) == getattr(direct, field), field
    assert made.expires_at - made.not_before == direct.expires_at - direct.not_before
    assert body["planned_finish_at"] == plan["finish_at"].isoformat()
    assert body["not_before"] == nb.isoformat()


@pytest.mark.asyncio
async def test_unreachable_deadline_creates_nothing():
    db = _db(_s1n(_plug(powered=True)))
    err = HTTPException(status_code=409, detail={"code": "ready_by_unreachable", "message": "x"})
    with patch("backend.services.ready_by.plan_ready_by", AsyncMock(side_effect=err)):
        with pytest.raises(HTTPException) as exc:
            await queue_ready_by(
                ReadyByRequest(plug_id=1, ready_by=datetime.now(timezone.utc) + timedelta(hours=1),
                               max_kwh=3.0),
                _user(), db,
            )
    assert exc.value.detail["code"] == "ready_by_unreachable"
    db.add.assert_not_called()
