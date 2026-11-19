"""
[Plug hours] DB-free tests for opening hours, max session length, amenities
and the access note (services/plug_hours.py, alembic 0049_plug_hours).

Pure resolution runs on fixed local datetimes (2026-11-16 is a Monday) so the
results never depend on the wall clock. The start, sweep and CPO-edit tests
pick windows by weekday (closed all of today) for the same reason.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.database.models import QueuedChargeStatus
from backend.services.plug_hours import (
    clamp_start_duration,
    driver_fields,
    is_open,
    next_open,
    normalize_amenities,
    normalize_hours,
    open_until,
    window_is_open,
)
from backend.services.pricing import _zone
from backend.services.session_reaper import SessionReaperService
from backend.tests.test_queued_charge import (
    _factory,
    _gateway,
    _plug,
    _qc,
    _row_db,
    _s1,
    _s1n,
    _scan_db,
    _tenant,
    _upd,
    _user,
)

TZ_NAME = "Asia/Kolkata"
# Resolved the same way the code resolves it (UTC where the host has no IANA
# database, such as a Windows box without tzdata), so both sides agree.
TZ = _zone(TZ_NAME)
MON, TUE, SUN, ALL = 1, 2, 64, 127


def _loc(day, hour, minute=0, second=0):
    """A local instant in November 2026 (the 16th is a Monday)."""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)


def _plug_ns(hours=None, max_minutes=None):
    return SimpleNamespace(opening_hours=hours, max_session_minutes=max_minutes,
                           amenities=None, access_note=None)


def _closed_today_hours():
    """Open only on a weekday three days from today (tenant local time), so
    the plug is closed now even if the test straddles local midnight."""
    wd = datetime.now(TZ).weekday()
    return [{"days": 1 << ((wd + 3) % 7), "start": 0, "end": 1440}]


# --- Normalisation and validation ---------------------------------------------

def test_normalize_splits_a_window_crossing_midnight():
    assert normalize_hours([{"days": MON, "start": 22 * 60, "end": 6 * 60}]) == [
        {"days": MON, "start": 1320, "end": 1440},
        {"days": TUE, "start": 0, "end": 360},
    ]
    # Sunday night wraps into Monday morning; end 0 reads as midnight.
    assert normalize_hours([{"days": SUN, "start": 1320, "end": 360}])[1]["days"] == MON
    assert normalize_hours([{"days": ALL, "start": 1320, "end": 0}]) == [
        {"days": ALL, "start": 1320, "end": 1440},
    ]
    # 00:00 to 00:00 is the whole day (the CPO form's "all day" entry).
    assert normalize_hours([{"days": 63, "start": 0, "end": 0}]) == [
        {"days": 63, "start": 0, "end": 1440},
    ]


def test_normalize_rejects_overlap_and_zero_length_and_clears_empty():
    with pytest.raises(ValueError, match="overlap"):
        normalize_hours([
            {"days": ALL, "start": 480, "end": 1200},
            {"days": MON, "start": 1140, "end": 1320},
        ])
    # The split tail of a midnight window overlaps a morning window.
    with pytest.raises(ValueError, match="overlap"):
        normalize_hours([
            {"days": MON, "start": 1320, "end": 360},
            {"days": TUE, "start": 300, "end": 600},
        ])
    with pytest.raises(ValueError):
        normalize_hours([{"days": ALL, "start": 600, "end": 600}])
    # Touching windows are fine (half-open, same as TariffSlot).
    assert len(normalize_hours([
        {"days": ALL, "start": 0, "end": 720}, {"days": ALL, "start": 720, "end": 1440},
    ])) == 2
    assert normalize_hours([]) is None
    assert normalize_hours(None) is None


def test_amenities_allowlist_dedupes_and_rejects_unknown():
    assert normalize_amenities(["wifi", "cafe", "wifi"]) == ["cafe", "wifi"]
    assert normalize_amenities([]) is None
    with pytest.raises(ValueError, match="Unknown amenity"):
        normalize_amenities(["<b>pool</b>"])


# --- Resolution -----------------------------------------------------------------

def test_hours_crossing_midnight_open_close_and_reopen():
    hours = normalize_hours([{"days": ALL, "start": 1320, "end": 360}])  # 22:00-06:00
    assert is_open(hours, _loc(16, 23, 30))
    assert is_open(hours, _loc(17, 3))
    assert not is_open(hours, _loc(17, 12))
    # Touching windows chain across midnight: closes at 06:00, not 24:00.
    assert open_until(hours, _loc(16, 23, 30)) == _loc(17, 6)
    assert next_open(hours, _loc(17, 12)) == _loc(17, 22)
    # Always open (one full-day window every day) never closes.
    assert open_until([{"days": ALL, "start": 0, "end": 1440}], _loc(16, 9)) is None


def test_start_near_closing_time_is_clamped():
    plug = _plug_ns([{"days": ALL, "start": 480, "end": 1320}])  # 08:00-22:00
    now = _loc(16, 21, 30).astimezone(timezone.utc)
    assert clamp_start_duration(plug, TZ_NAME, now, None) == 1800
    assert clamp_start_duration(plug, TZ_NAME, now, 7200) == 1800
    assert clamp_start_duration(plug, TZ_NAME, now, 600) == 600
    # Inside the last minute: refused as closed, never a near-zero limit.
    with pytest.raises(ValueError):
        clamp_start_duration(plug, TZ_NAME, _loc(16, 21, 59, 30).astimezone(timezone.utc), None)
    with pytest.raises(ValueError):
        clamp_start_duration(plug, TZ_NAME, _loc(16, 23).astimezone(timezone.utc), None)


def test_max_session_minutes_clamps_and_null_settings_change_nothing():
    now = _loc(16, 12).astimezone(timezone.utc)
    assert clamp_start_duration(_plug_ns(max_minutes=45), TZ_NAME, now, None) == 2700
    assert clamp_start_duration(_plug_ns(max_minutes=45), TZ_NAME, now, 600) == 600
    plain = _plug_ns()
    assert clamp_start_duration(plain, TZ_NAME, now, None) is None
    assert clamp_start_duration(plain, TZ_NAME, now, 3600) == 3600
    assert window_is_open(plain, TZ_NAME, now, now + timedelta(hours=4))
    assert driver_fields(plain, TZ_NAME, now) == {
        "opening_hours": None, "open_now": None, "hours_changes_at": None,
        "max_session_minutes": None, "amenities": None, "access_note": None,
    }


def test_reservation_window_must_fit_inside_the_hours():
    day = _plug_ns([{"days": ALL, "start": 480, "end": 1320}])  # 08:00-22:00
    utc = timezone.utc
    assert window_is_open(day, TZ_NAME, _loc(16, 10).astimezone(utc), _loc(16, 11).astimezone(utc))
    assert window_is_open(day, TZ_NAME, _loc(16, 21).astimezone(utc), _loc(16, 22).astimezone(utc))
    assert not window_is_open(day, TZ_NAME, _loc(16, 21).astimezone(utc), _loc(16, 23).astimezone(utc))
    assert not window_is_open(day, TZ_NAME, _loc(16, 7).astimezone(utc), _loc(16, 9).astimezone(utc))
    night = _plug_ns(normalize_hours([{"days": ALL, "start": 1320, "end": 360}]))
    assert window_is_open(night, TZ_NAME, _loc(16, 23).astimezone(utc), _loc(17, 1).astimezone(utc))


def test_driver_fields_say_open_now_and_closing_time():
    plug = _plug_ns([{"days": ALL, "start": 480, "end": 1320}])
    plug.amenities = ["cafe"]
    plug.access_note = "Gate on the left"
    fields = driver_fields(plug, TZ_NAME, _loc(16, 12).astimezone(timezone.utc))
    assert fields["open_now"] is True
    assert datetime.fromisoformat(fields["hours_changes_at"]) == _loc(16, 22)
    assert fields["amenities"] == ["cafe"]
    closed = driver_fields(plug, TZ_NAME, _loc(16, 23).astimezone(timezone.utc))
    assert closed["open_now"] is False
    assert datetime.fromisoformat(closed["hours_changes_at"]) == _loc(17, 8)


# --- Start choke point ------------------------------------------------------------

def _start_patches(session_start, mqtt):
    return (
        patch.object(session_start, "check_circuit_admission", AsyncMock()),
        patch.object(session_start, "resolve_rate_window",
                     AsyncMock(return_value=(Decimal("5.00"), None))),
        patch.object(session_start, "available_balance", AsyncMock(return_value=Decimal("500"))),
        patch.object(session_start, "max_rate_over_window",
                     AsyncMock(return_value=Decimal("5.00"))),
        patch.object(session_start, "set_plug_telemetry_interval", AsyncMock()),
        patch.object(session_start.state, "mqtt_manager", mqtt),
        patch.object(session_start.state, "telemetry_store", MagicMock()),
        patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()),
        patch("backend.services.caps.effective_plug_cap", return_value=16.0),
    )


@pytest.mark.asyncio
async def test_closed_plug_start_is_refused_before_admission():
    from contextlib import ExitStack

    from backend.services import session_start

    tz_result = MagicMock()
    tz_result.scalar_one_or_none.return_value = TZ_NAME
    db = MagicMock()
    db.execute = AsyncMock(return_value=tz_result)
    db.commit = AsyncMock()
    plug = MagicMock(id=1, gateway_id="gw-1", local_ip="10.0.0.2",
                     opening_hours=_closed_today_hours(), max_session_minutes=None)
    mqtt = MagicMock()
    patches = _start_patches(session_start, mqtt)
    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        with pytest.raises(HTTPException) as exc:
            await session_start.begin_active_session(
                db, MagicMock(id=3), plug, MagicMock(tenant_id=1),
                max_kwh=None, max_duration_seconds=None,
            )
    assert exc.value.status_code == 409
    assert "closed" in exc.value.detail
    mocks[0].assert_not_awaited()          # never reached circuit admission
    mqtt.send_plug_command.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_max_session_minutes_clamps_the_session_and_the_on_command():
    """No hours set: no tenant query (db.execute is a plain MagicMock here and
    would fail if awaited), and the persisted limit and the firmware's
    duration both carry the clamp."""
    from contextlib import ExitStack

    from backend.services import session_start

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    plug = MagicMock(id=1, gateway_id="gw-1", local_ip="10.0.0.2",
                     opening_hours=None, max_session_minutes=30)
    mqtt = MagicMock()
    mqtt.send_plug_command.return_value = True
    with ExitStack() as stack:
        for p in _start_patches(session_start, mqtt):
            stack.enter_context(p)
        session = await session_start.begin_active_session(
            db, MagicMock(id=3, coin_balance=Decimal("500")), plug,
            MagicMock(tenant_id=1), max_kwh=None, max_duration_seconds=3600,
        )
    assert session.max_duration_seconds == 1800
    assert mqtt.send_plug_command.call_args.kwargs["max_duration"] == 1800


# --- Queued / scheduled sweep ---------------------------------------------------

@pytest.mark.asyncio
async def test_sweep_leaves_a_closed_plugs_queued_row_waiting():
    qc = _qc()
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    plug.opening_hours = _closed_today_hours()
    plug.max_session_minutes = None
    row_db = _row_db(
        _s1n(qc), _s1n(_user()), _s1(0), _s1n(plug), _s1(_tenant(delay=2)),
        _upd(), _s1n(None), _s1n(None), _s1(_gateway()),
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session", AsyncMock()) as begin, \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        started = await svc.reap_queued_starts_once()

    assert started == 0
    assert qc.status == QueuedChargeStatus.WAITING
    begin.assert_not_awaited()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_leaves_the_row_waiting_when_the_helper_finds_it_closed():
    """The sweep's own check (the tick's clock) passed, but the plug closed
    before begin_active_session's fresh clock: still WAITING, no notice."""
    from backend.services.session_start import PlugClosedError

    qc = _qc()
    plug = _plug(powered=True)
    plug.powered_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    plug.opening_hours = None
    plug.max_session_minutes = None
    row_db = _row_db(
        _s1n(qc), _s1n(_user()), _s1(0), _s1n(plug), _s1(_tenant(delay=2)),
        _upd(), _s1n(None), _s1n(None), _s1(_gateway()),
    )
    svc = SessionReaperService(_factory(_scan_db([1]), row_db), AsyncMock())

    with patch("backend.services.session_start.begin_active_session",
               AsyncMock(side_effect=PlugClosedError("Closed, opens 08:00"))) as begin,          patch("backend.services.notifications.notify", AsyncMock()) as notify:
        started = await svc.reap_queued_starts_once()

    assert started == 0
    begin.assert_awaited_once()
    assert qc.status == QueuedChargeStatus.WAITING
    notify.assert_not_awaited()


# --- CPO edit --------------------------------------------------------------------

def _cpo_db(plug):
    result = MagicMock()
    result.scalar_one_or_none.return_value = plug
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_cpo_edit_rejects_overlapping_hours_and_unknown_amenity():
    from backend.routers.cpo._plugs import cpo_update_plug
    from backend.schemas import CpoPlugUpdateRequest

    user = MagicMock(id=1, tenant_id=1)
    for req in (
        CpoPlugUpdateRequest(opening_hours=[
            {"days": ALL, "start": 480, "end": 1200},
            {"days": ALL, "start": 1000, "end": 1300},
        ]),
        CpoPlugUpdateRequest(amenities=["pool"]),
    ):
        db = _cpo_db(MagicMock(id=7))
        with pytest.raises(HTTPException) as exc:
            await cpo_update_plug(7, req, user, db)
        assert exc.value.status_code == 400
        db.commit.assert_not_awaited()

    with pytest.raises(ValueError):
        CpoPlugUpdateRequest(access_note="x" * 281)


@pytest.mark.asyncio
async def test_cpo_edit_stores_normalized_fields_and_null_clears():
    from backend.database.models import PlugStatus
    from backend.routers.cpo import _plugs
    from backend.schemas import CpoPlugUpdateRequest

    user = MagicMock(id=1, tenant_id=1)
    plug = MagicMock(id=7, status=PlugStatus.AVAILABLE)
    with patch.object(_plugs, "_publish_gateway_roster", AsyncMock()):
        await _plugs.cpo_update_plug(7, CpoPlugUpdateRequest(
            opening_hours=[{"days": MON, "start": 1320, "end": 360}],
            max_session_minutes=90, amenities=["wifi", "cafe"],
            access_note="  Gate code at the desk  ",
        ), user, _cpo_db(plug))
        assert plug.opening_hours == [
            {"days": MON, "start": 1320, "end": 1440}, {"days": TUE, "start": 0, "end": 360},
        ]
        assert plug.max_session_minutes == 90
        assert plug.amenities == ["cafe", "wifi"]
        assert plug.access_note == "Gate code at the desk"

        await _plugs.cpo_update_plug(7, CpoPlugUpdateRequest(
            opening_hours=None, max_session_minutes=0, amenities=[], access_note="",
        ), user, _cpo_db(plug))
    assert plug.opening_hours is None
    assert plug.max_session_minutes is None
    assert plug.amenities is None
    assert plug.access_note is None


@pytest.mark.asyncio
async def test_cpo_plug_list_masks_the_access_note_for_a_viewer():
    from backend.database.models import PlugStatus, UserRole
    from backend.routers.cpo._plugs import cpo_list_plugs

    plug = MagicMock(id=7, gateway_id="gw-1", local_ip="10.0.0.5",
                     plug_model="P110", status=PlugStatus.AVAILABLE, current_power_w=0.0,
                     opening_hours=[{"days": ALL, "start": 480, "end": 1320}],
                     max_session_minutes=None, amenities=["cafe"],
                     access_note="Call 9876543210 for the gate", last_seen_at=None, created_at=None)
    result = MagicMock()
    result.all.return_value = [(plug, None)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    viewer = MagicMock(id=2, tenant_id=1, role=UserRole.CPO_VIEWER)
    row = (await cpo_list_plugs(viewer, db, limit=50, offset=0))[0]
    assert "9876543210" not in row["access_note"]
    assert row["amenities"] == ["cafe"]
    assert row["opening_hours"] == [{"days": ALL, "start": 480, "end": 1320}]

    owner = MagicMock(id=1, tenant_id=1, role=UserRole.CPO)
    assert (await cpo_list_plugs(owner, db, limit=50, offset=0))[0]["access_note"] == (
        "Call 9876543210 for the gate"
    )


def test_limits_patch_cap_matches_the_start_bounds():
    from backend.services.plug_hours import duration_cap

    plug = _plug_ns([{"days": ALL, "start": 480, "end": 1320}], max_minutes=600)
    started = _loc(16, 20).astimezone(timezone.utc)
    assert duration_cap(plug, TZ_NAME, started) == 7200              # ends by 22:00
    assert duration_cap(_plug_ns(max_minutes=45), TZ_NAME, started) == 2700
    assert duration_cap(_plug_ns(), TZ_NAME, started) is None
    # Started while the (since edited) hours say closed: only the max length.
    assert duration_cap(plug, TZ_NAME, _loc(16, 23).astimezone(timezone.utc)) == 36000


def test_limits_patch_cap_only_bounds_a_raise():
    """A CPO cut to the hours or max length made after the start leaves the
    cap below the current limit: a raise keeps the current limit, never less."""
    from backend.services.plug_hours import capped_limit

    assert capped_limit(7200, 3600, 1800) == 3600     # cap below current: keep current
    assert capped_limit(7200, 1800, 3600) == 3600     # cap above current: raise to cap
    assert capped_limit(7200, None, 1800) == 7200     # unlimited before: any value lowers it
    assert capped_limit(600, 3600, 1800) == 600       # a lowering is the driver's choice
    assert capped_limit(7200, 3600, None) == 7200     # no hours, no max length


def test_driver_plug_routes_mask_the_access_note_for_a_viewer():
    from backend.database.models import UserRole
    from backend.routers.plugs import _hours_fields

    plug = _plug_ns()
    plug.access_note = "Ring 9876543210 at the gate"
    now = _loc(16, 12).astimezone(timezone.utc)
    viewer = MagicMock(role=UserRole.CPO_VIEWER)
    driver = MagicMock(role=UserRole.DRIVER)
    assert "9876543210" not in _hours_fields(plug, TZ_NAME, now, viewer)["access_note"]
    assert _hours_fields(plug, TZ_NAME, now, driver)["access_note"] == "Ring 9876543210 at the gate"
