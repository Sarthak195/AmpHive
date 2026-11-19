"""
[F8V] Vehicle profile and the monthly insights email.

Two tiers, mirroring the rest of this suite:

1. DB-free (always run): VehicleProfile bounds, the driver's figures flowing
   into estimate_impact / assumptions / GET /api/me/stats, the /api/me/vehicle
   routes, the 09:00-local gate, the email text and HTML, and the send / empty
   month / failure / give-up / idempotent-claim branches of
   send_monthly_insights against mocked sessions whose statements are compiled
   for Postgres first.
2. DB-gated (CI's postgres service, TEST_DATABASE_URL; skipped locally): the
   real claim UPDATE, so one send per user per month across repeated and
   concurrent passes, opt-out, empty month and retry-after-failure.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from backend.routers.notifications import VehicleSettings, get_vehicle, put_vehicle
from backend.services import insights
from backend.services import insights_email as ie
from backend.services.insights import VehicleProfile, load_vehicle
from backend.tests.test_charge_routines import requires_db
from backend.tests.test_reservations import (  # noqa: F401 (factory is a fixture)
    _seed_user,
    factory,
)

IST = timezone(timedelta(hours=5, minutes=30))
# 2026-12-01 09:01 IST, the first moment the November mail is due.
DUE = datetime(2026, 12, 1, 9, 1, tzinfo=IST)


@pytest.fixture(autouse=True)
def _ist_zone(monkeypatch):
    """The driver zone as a fixed +05:30 offset, so the tests do not need the
    IANA tz database (absent on some hosts; pricing._zone then falls back to
    UTC, which is why test_charge_routines patches it the same way)."""
    monkeypatch.setattr("backend.services.pricing._zone", lambda _tz: IST)


# ===========================================================================
# Profile validation and use in the insights
# ===========================================================================

@pytest.mark.parametrize("field,bad", [
    ("km_per_kwh", 0), ("km_per_kwh", 0.4), ("km_per_kwh", 61), ("km_per_kwh", -3),
    ("petrol_price_per_l", 0), ("petrol_price_per_l", 501),
    ("petrol_km_per_l", 0.9), ("petrol_km_per_l", 101),
])
def test_vehicle_profile_rejects_out_of_range(field, bad):
    with pytest.raises(ValidationError):
        VehicleProfile(**{field: bad})


def test_vehicle_profile_accepts_nulls_and_bounds_and_rejects_unknown_fields():
    assert VehicleProfile().km_per_kwh is None
    VehicleProfile(km_per_kwh=0.5, petrol_price_per_l=500, petrol_km_per_l=100)
    with pytest.raises(ValidationError):
        VehicleProfile(km_per_kwh=6, colour="red")


def test_load_vehicle_falls_back_to_defaults_on_bad_rows():
    assert load_vehicle(None) is None
    assert load_vehicle("junk") is None
    assert load_vehicle({"km_per_kwh": 9999}) is None
    assert load_vehicle({"km_per_kwh": 8}).km_per_kwh == 8


def test_estimate_impact_uses_the_drivers_figures_and_null_keeps_defaults():
    base = insights.estimate_impact(10)
    assert insights.estimate_impact(10, None) == base
    assert insights.estimate_impact(10, VehicleProfile()) == base

    mine = VehicleProfile(km_per_kwh=10, petrol_km_per_l=20, petrol_price_per_l=110)
    out = insights.estimate_impact(10, mine)
    assert out["distance_km"] == 100.0
    assert out["petrol_litres"] == 5.0
    assert out["petrol_equivalent_inr"] == 550.0
    assert out["petrol_co2_kg"] == round(5 * insights.PETROL_CO2_KG_PER_L, 2)

    # A partial profile overrides only what it sets.
    only_price = insights.estimate_impact(10, VehicleProfile(petrol_price_per_l=200))
    assert only_price["distance_km"] == base["distance_km"]
    assert only_price["petrol_equivalent_inr"] == round(base["petrol_litres"] * 200, 2)


def test_assumptions_and_co2_factor_follow_the_profile():
    default = insights.assumptions()
    assert default["personalised"] is False
    mine = VehicleProfile(km_per_kwh=12)
    a = insights.assumptions(mine)
    assert a["personalised"] is True
    assert a["ev_km_per_kwh"] == 12 and a["ice_km_per_l"] == insights.ICE_KM_PER_L
    assert insights.co2_avoided_kg_per_kwh(mine) > insights.co2_avoided_kg_per_kwh()


def _stats_db():
    """Result rows for get_my_stats: month, lifetime, series, refunds."""
    def first(v):
        r = MagicMock()
        r.first.return_value = v
        return r

    def all_(v):
        r = MagicMock()
        r.all.return_value = v
        return r

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        first((1, 4.0, Decimal("20"))), first((2, 10.0, Decimal("50"))), all_([]), all_([]),
    ])
    return db


@pytest.mark.asyncio
async def test_my_stats_impact_uses_the_drivers_vehicle():
    from backend.routers.sessions import get_my_stats

    user = MagicMock()
    user.id = 7
    user.vehicle_profile = {"km_per_kwh": 10, "petrol_km_per_l": 20, "petrol_price_per_l": 110}
    resp = await get_my_stats(user, _stats_db())
    assert resp["impact"]["assumptions"]["personalised"] is True
    assert resp["impact"]["lifetime"]["distance_km"] == 100.0
    assert resp["impact"]["month"]["petrol_equivalent_inr"] == 220.0  # 4 kWh -> 40 km -> 2 L

    user.vehicle_profile = None
    resp = await get_my_stats(user, _stats_db())
    assert resp["impact"]["assumptions"]["personalised"] is False
    assert resp["impact"]["lifetime"] == insights.estimate_impact(10.0)


# ===========================================================================
# GET / PUT /api/me/vehicle
# ===========================================================================

def _me(**kw):
    u = MagicMock()
    u.id = 7
    u.vehicle_profile = kw.get("vehicle_profile")
    u.monthly_insights_email = kw.get("monthly", False)
    u.insights_email_month = kw.get("month")
    return u


@pytest.mark.asyncio
async def test_get_vehicle_defaults_and_saved():
    out = await get_vehicle(_me())
    assert out["km_per_kwh"] is None and out["monthly_insights_email"] is False
    assert out["defaults"]["km_per_kwh"] == insights.EV_KM_PER_KWH
    assert out["co2_avoided_kg_per_kwh"] == round(insights.co2_avoided_kg_per_kwh(), 4)

    saved = await get_vehicle(_me(vehicle_profile={"km_per_kwh": 12}, monthly=True))
    assert saved["km_per_kwh"] == 12 and saved["monthly_insights_email"] is True
    assert saved["co2_avoided_kg_per_kwh"] > out["co2_avoided_kg_per_kwh"]


def test_put_body_validates_bounds():
    with pytest.raises(ValidationError):
        VehicleSettings(km_per_kwh=500)
    assert VehicleSettings().monthly_insights_email is None


def _values(db):
    return db.execute.call_args.args[0].compile(dialect=postgresql.dialect()).params


@pytest.mark.asyncio
async def test_put_vehicle_saves_the_profile_and_all_null_clears_it():
    db = AsyncMock()
    out = await put_vehicle(VehicleSettings(km_per_kwh=9, petrol_km_per_l=30), _me(), db)
    assert _values(db)["vehicle_profile"] == {
        "km_per_kwh": 9.0, "petrol_price_per_l": None, "petrol_km_per_l": 30.0,
    }
    assert "monthly_insights_email" not in _values(db)  # omitted = unchanged
    assert out["km_per_kwh"] == 9.0
    db.commit.assert_awaited()

    db = AsyncMock()
    await put_vehicle(VehicleSettings(), _me(vehicle_profile={"km_per_kwh": 9}), db)
    assert _values(db)["vehicle_profile"] is None


@pytest.mark.asyncio
async def test_turning_the_email_on_claims_the_finished_month():
    db = AsyncMock()
    now = datetime(2026, 12, 15, 10, 0, tzinfo=timezone.utc)
    with patch("backend.routers.notifications.datetime") as dt:
        dt.now.return_value = now
        out = await put_vehicle(VehicleSettings(monthly_insights_email=True), _me(), db)
    params = _values(db)
    assert out["monthly_insights_email"] is True
    assert params["monthly_insights_email"] is True
    assert params["insights_email_month"] == "2026-11"

    # Already on: no re-claim. Off: the flag only.
    db = AsyncMock()
    await put_vehicle(VehicleSettings(monthly_insights_email=True), _me(monthly=True), db)
    assert "insights_email_month" not in _values(db)
    db = AsyncMock()
    await put_vehicle(VehicleSettings(monthly_insights_email=False), _me(monthly=True), db)
    assert _values(db)["monthly_insights_email"] is False


def test_vehicle_routes_are_in_the_viewer_walk():
    """A cpo_viewer's PUT answers 403 through the global guard (the route walk
    in test_cpo_viewer_role.py covers every write route); pin that it is hit."""
    from backend.tests.test_cpo_viewer_role import _write_routes

    assert ("PUT", "/api/me/vehicle") in {(m, p) for m, p, _ in _write_routes()}


# ===========================================================================
# The email: gate, text, send, empty month, failure, idempotent claim
# ===========================================================================

def test_previous_month_key_wraps_the_year():
    assert ie.previous_month_key(datetime(2027, 1, 3, 12, tzinfo=timezone.utc)) == "2026-12"
    assert ie.previous_month_key(DUE) == "2026-11"


def test_nothing_is_due_before_nine_local_on_the_first():
    assert ie.due_month(datetime(2026, 12, 1, 8, 59, tzinfo=IST)) is None
    assert ie.due_month(datetime(2026, 12, 1, 9, 0, tzinfo=IST)) == "2026-11"
    # Later days (the backend was down on the 1st) still send the same month.
    assert ie.due_month(datetime(2026, 12, 4, 0, 5, tzinfo=IST)) == "2026-11"


def test_render_has_text_html_the_drivers_figures_and_the_off_switch():
    mine = VehicleProfile(km_per_kwh=10, petrol_km_per_l=20, petrol_price_per_l=110)
    summary = {"sessions": 3, "energy_kwh": 10.0, "spend_inr": 61.5}
    subject, text, html_body = ie.render("Asha <b>", "2026-11", summary, mine,
                                         "https://amphive.app")
    assert subject == "Your AmpHive charging in November 2026"
    assert "10.000 kWh" in text and "₹61.50" in text and "100.0 km" in text
    assert "10 km per kWh" in text and "₹110/L" in text
    assert "https://amphive.app/account" in text and "turn it off" in text
    assert "&lt;b&gt;" in html_body and "<b>" not in html_body  # escaped
    assert "turn it off" in html_body
    assert "—" not in text


def _cm(db):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _compiling(results):
    it = iter(results)

    async def _execute(stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect())
        return next(it)
    return _execute


def _res(**attrs):
    r = MagicMock()
    for k, v in attrs.items():
        getattr(r, k).return_value = v
    return r


def _db(*results, scalar=0):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_compiling(results))
    db.scalar = AsyncMock(return_value=scalar)
    return db


def _factory(*dbs):
    f = MagicMock()
    f.side_effect = [_cm(d) for d in dbs]
    return f


CLAIM_ROW = ("a@example.com", "Asha", {"km_per_kwh": 10})


def _month(count, energy=4.0, coins=Decimal("20")):
    return _res(one=(count, energy, coins))


def _scan(*rows):
    return _db(_res(all=list(rows)))


@pytest.fixture()
def smtp(monkeypatch):
    sender = MagicMock()
    monkeypatch.setattr(ie.email, "smtp_configured", lambda: True)
    monkeypatch.setattr(ie.email, "send_email_or_raise", sender)
    return sender


@pytest.mark.asyncio
async def test_pass_before_nine_or_without_smtp_does_no_work(smtp, monkeypatch):
    factory_ = MagicMock()
    assert await ie.send_monthly_insights(factory_, datetime(2026, 12, 1, 8, 59, tzinfo=IST)) == 0
    monkeypatch.setattr(ie.email, "smtp_configured", lambda: False)
    assert await ie.send_monthly_insights(factory_, DUE) == 0
    factory_.assert_not_called()
    smtp.assert_not_called()


@pytest.mark.asyncio
async def test_candidate_query_only_selects_opted_in_unsent_open_accounts(smtp):
    scan = _scan()
    await ie.send_monthly_insights(_factory(scan), DUE)
    sql = str(scan.execute.await_args.args[0].compile(dialect=postgresql.dialect(),
                                                      compile_kwargs={"literal_binds": True}))
    assert "monthly_insights_email IS true" in sql
    assert "deleted_at IS NULL" in sql
    assert "insights_email_month < '2026-11'" in sql and "LIMIT 25" in sql


@pytest.mark.asyncio
async def test_sends_once_with_text_and_html_for_the_previous_month(smtp):
    claim = _db(_res(first=CLAIM_ROW), _month(3))
    sent = await ie.send_monthly_insights(_factory(_scan((7, None, 0)), claim), DUE)
    assert sent == 1
    claim.commit.assert_awaited_once()
    to, subject, text, html_body = smtp.call_args.args
    assert to == "a@example.com" and "November 2026" in subject
    assert "3" in text and "<table" in html_body
    # The driver's saved vehicle shaped the estimate: 4 kWh x 10 km/kWh.
    assert "40.0 km" in text


@pytest.mark.asyncio
async def test_empty_month_is_claimed_and_skipped(smtp):
    claim = _db(_res(first=CLAIM_ROW), _month(0, 0.0, Decimal("0")))
    sent = await ie.send_monthly_insights(_factory(_scan((7, None, 0)), claim), DUE)
    assert sent == 0
    claim.commit.assert_awaited_once()  # stamped, so it is not asked again
    smtp.assert_not_called()


@pytest.mark.asyncio
async def test_second_pass_loses_the_claim_and_sends_nothing(smtp):
    # The scan still saw the row, but another pass claimed it first.
    lost = _db(_res(first=None))
    sent = await ie.send_monthly_insights(_factory(_scan((7, None, 0)), lost), DUE)
    assert sent == 0
    lost.rollback.assert_awaited_once()
    smtp.assert_not_called()


@pytest.mark.asyncio
async def test_a_send_failure_puts_the_old_month_back_for_a_retry(smtp):
    smtp.side_effect = OSError("smtp down")
    claim = _db(_res(first=CLAIM_ROW), _month(2))
    revert = _db(MagicMock())
    sent = await ie.send_monthly_insights(
        _factory(_scan((7, "2026-10", 0)), claim, revert), DUE)
    assert sent == 0
    params = revert.execute.await_args.args[0].compile(dialect=postgresql.dialect()).params
    assert params["insights_email_month"] == "2026-10"
    assert params["insights_email_failures"] == 1
    revert.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_third_failure_gives_the_month_up(smtp):
    smtp.side_effect = OSError("smtp down")
    claim = _db(_res(first=CLAIM_ROW), _month(2))
    giveup = _db(MagicMock())
    await ie.send_monthly_insights(_factory(_scan((7, "2026-10", 2)), claim, giveup), DUE)
    params = giveup.execute.await_args.args[0].compile(dialect=postgresql.dialect()).params
    assert "insights_email_month" not in params  # stays claimed
    assert params["insights_email_failures"] == 0


# ===========================================================================
# DB-gated: the real claim
# ===========================================================================

async def _enable(factory, uid):  # noqa: F811
    from sqlalchemy import update

    from backend.database.models import User

    async with factory() as db:
        await db.execute(update(User).where(User.id == uid).values(
            monthly_insights_email=True, vehicle_profile={"km_per_kwh": 10}))
        await db.commit()


async def _session(factory, uid, started):  # noqa: F811
    from backend.database.models import ChargingSession, Gateway, GatewayStatus, Plug, PlugStatus, SessionStatus, Tenant

    async with factory() as db:
        t = Tenant(name=f"T{uid}-{started.month}")
        db.add(t)
        await db.flush()
        gw = Gateway(id=f"gw-v{uid}-{started.month}", tenant_id=t.id, name="GW",
                     vpn_ip=f"vpn-{uid}-{started.month}", status=GatewayStatus.ONLINE)
        db.add(gw)
        await db.flush()
        plug = Plug(gateway_id=gw.id, name="P", local_ip="10.0.0.9", status=PlugStatus.AVAILABLE)
        db.add(plug)
        await db.flush()
        db.add(ChargingSession(
            tenant_id=t.id, user_id=uid, plug_id=plug.id, started_at=started,
            ended_at=started + timedelta(hours=1), energy_kwh=2.0,
            coins_spent=Decimal("10.00"), status=SessionStatus.COMPLETED))
        await db.commit()


async def _row(factory, uid):  # noqa: F811
    from sqlalchemy import select

    from backend.database.models import User

    async with factory() as db:
        return (await db.execute(
            select(User.insights_email_month, User.insights_email_failures)
            .where(User.id == uid))).one()


NOV = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


@requires_db
@pytest.mark.asyncio
async def test_db_one_send_per_month_across_repeated_and_concurrent_passes(factory, smtp):  # noqa: F811
    uid = await _seed_user(factory)
    await _enable(factory, uid)
    await _session(factory, uid, NOV)
    results = await asyncio.gather(
        ie.send_monthly_insights(factory, DUE), ie.send_monthly_insights(factory, DUE))
    again = await ie.send_monthly_insights(factory, DUE + timedelta(days=2))
    assert sum(results) == 1 and again == 0
    assert smtp.call_count == 1
    assert tuple(await _row(factory, uid)) == ("2026-11", 0)


@requires_db
@pytest.mark.asyncio
async def test_db_opted_out_and_empty_month_send_nothing(factory, smtp):  # noqa: F811
    off = await _seed_user(factory)
    await _session(factory, off, NOV)          # sessions, but not opted in
    empty = await _seed_user(factory)
    await _enable(factory, empty)              # opted in, no sessions in November
    assert await ie.send_monthly_insights(factory, DUE) == 0
    smtp.assert_not_called()
    assert tuple(await _row(factory, off)) == (None, 0)
    assert (await _row(factory, empty))[0] == "2026-11"   # claimed, not asked again


@requires_db
@pytest.mark.asyncio
async def test_db_failure_is_retried_then_stamped_on_success(factory, smtp):  # noqa: F811
    uid = await _seed_user(factory)
    await _enable(factory, uid)
    await _session(factory, uid, NOV)
    smtp.side_effect = OSError("down")
    assert await ie.send_monthly_insights(factory, DUE) == 0
    assert tuple(await _row(factory, uid)) == (None, 1)
    smtp.side_effect = None
    assert await ie.send_monthly_insights(factory, DUE + timedelta(minutes=15)) == 1
    assert tuple(await _row(factory, uid)) == ("2026-11", 0)
