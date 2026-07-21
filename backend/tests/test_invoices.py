"""
Tests for GST tax invoices (services/invoices.py + routers/sessions.py
GET /api/sessions/{id}/invoice, routers/cpo.py GET /api/cpo/invoices).

Two tiers, mirroring test_payouts.py:

1. Pure-function + mocked-db tests (no DB, always run): the inclusive-GST
   split math (services.invoices.split_gst_inclusive / gst_rate_pct) and the
   access-control branches that don't need real data — a foreign driver, a
   cross-tenant CPO, and a nonexistent session all get rejected by the
   router's OWN session-select before it ever reaches the service, so these
   need only one mocked db.execute call each. Same style as
   test_payouts.py's mark_paid/cancel role-gate tests.
2. DB-gated tests (need a real PostgreSQL — CI's postgres:15 service,
   exported as TEST_DATABASE_URL; skipped locally, same as test_wallet.py /
   test_payouts.py): the GST split + seller/line snapshot end to end,
   idempotency (repeat calls return the same invoice_number), sequential
   numbering under the tenant row lock — both across two different sessions
   AND the harder case of two concurrent *first issues racing for the same
   session* (the UNIQUE constraint + IntegrityError catch, not just the
   pre-check, is what makes that safe) — the unfinished/nothing-billed 400,
   the html format, and CPO-side tenant-scoped listing.
"""
import asyncio
import itertools
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.database.models import SessionStatus, UserRole
from backend.routers.cpo import cpo_list_invoices
from backend.routers.sessions import get_session_invoice
from backend.services import invoices as invoices_service

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status",
]


# ===========================================================================
# Tier 1: pure math + mocked-db (no DB, always run)
# ===========================================================================

def test_split_gst_inclusive_18pct_round_number():
    taxable, gst = invoices_service.split_gst_inclusive(Decimal("118.00"), Decimal("18.0"))
    assert taxable == Decimal("100.00")
    assert gst == Decimal("18.00")
    assert taxable + gst == Decimal("118.00")


def test_split_gst_inclusive_rounds_half_up_and_foots_to_total():
    """117.50 doesn't divide evenly by 1.18 -- to_money's ROUND_HALF_UP must
    push the raw 99.5762... to 99.58, and taxable + gst must still foot back
    to the original total exactly (gst is DERIVED, not independently
    rounded) -- same invariant services/payouts.py compute_fee_and_net
    guarantees for fee/net."""
    taxable, gst = invoices_service.split_gst_inclusive(Decimal("117.50"), Decimal("18.0"))
    assert taxable == Decimal("99.58")
    assert gst == Decimal("17.92")
    assert taxable + gst == Decimal("117.50")


def test_split_gst_inclusive_defaults_to_env_rate(monkeypatch):
    monkeypatch.setenv("GST_RATE_PCT", "12")
    taxable, gst = invoices_service.split_gst_inclusive(Decimal("112.00"))
    assert taxable == Decimal("100.00")
    assert gst == Decimal("12.00")


def test_gst_rate_pct_default_when_unset(monkeypatch):
    monkeypatch.delenv("GST_RATE_PCT", raising=False)
    assert invoices_service.gst_rate_pct() == Decimal("18.0")


def test_gst_rate_pct_falls_back_on_garbage_env(monkeypatch):
    monkeypatch.setenv("GST_RATE_PCT", "not-a-number")
    assert invoices_service.gst_rate_pct() == Decimal("18.0")


# --- CPO profile GST seller config (schema — DB-free) -----------------------

def test_cpo_profile_update_accepts_gst_seller_fields():
    """PUT /api/cpo/profile now carries the GST seller identity (configured on
    the CPO Settings page) that services/invoices.py stamps onto invoices."""
    from backend.schemas import CpoProfileUpdateRequest
    req = CpoProfileUpdateRequest(
        gstin="22AAAAA0000A1Z5", legal_name="Acme Charging Pvt Ltd", invoice_prefix="ACME"
    )
    assert (req.gstin, req.legal_name, req.invoice_prefix) == (
        "22AAAAA0000A1Z5", "Acme Charging Pvt Ltd", "ACME"
    )
    # Omitted stays None (unchanged); empty string is allowed (the endpoint
    # clears it back to NULL).
    assert CpoProfileUpdateRequest().gstin is None
    assert CpoProfileUpdateRequest(gstin="").gstin == ""


def test_cpo_profile_update_enforces_gst_length_caps():
    from pydantic import ValidationError

    from backend.schemas import CpoProfileUpdateRequest
    with pytest.raises(ValidationError):
        CpoProfileUpdateRequest(gstin="x" * 16)            # GSTIN max 15
    with pytest.raises(ValidationError):
        CpoProfileUpdateRequest(invoice_prefix="x" * 13)   # prefix max 12
    with pytest.raises(ValidationError):
        CpoProfileUpdateRequest(legal_name="x" * 121)      # legal_name max 120


def _session_mock(id=1, user_id=1, tenant_id=1, status=SessionStatus.COMPLETED, coins_spent=Decimal("10.00")):
    s = MagicMock()
    s.id = id
    s.user_id = user_id
    s.tenant_id = tenant_id
    s.status = status
    s.coins_spent = coins_spent
    return s


def _stub_user(user_id, tenant_id, role):
    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.role = role
    u.email = f"user{user_id}@example.com"
    return u


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


@pytest.mark.asyncio
async def test_foreign_driver_gets_404():
    """A driver who doesn't own the session is rejected by the router's own
    session-select before the service (issue_invoice_for_session) is ever
    called -- one mocked db.execute call suffices."""
    session = _session_mock(id=5, user_id=1, tenant_id=1)
    db = _db(_result(session))
    intruder = _stub_user(user_id=2, tenant_id=None, role=UserRole.DRIVER)

    with pytest.raises(HTTPException) as exc:
        await get_session_invoice(session_id=5, format=None, user=intruder, db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_cpo_gets_404():
    session = _session_mock(id=5, user_id=1, tenant_id=1)
    db = _db(_result(session))
    other_cpo = _stub_user(user_id=9, tenant_id=2, role=UserRole.CPO)

    with pytest.raises(HTTPException) as exc:
        await get_session_invoice(session_id=5, format=None, user=other_cpo, db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_nonexistent_session_returns_404():
    db = _db(_result(None))
    driver = _stub_user(user_id=1, tenant_id=None, role=UserRole.DRIVER)

    with pytest.raises(HTTPException) as exc:
        await get_session_invoice(session_id=999, format=None, user=driver, db=db)
    assert exc.value.status_code == 404


# ===========================================================================
# Tier 2: DB-gated (real Postgres)
# ===========================================================================

_seed_counter = itertools.count(1)


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory. Mirrors
    test_payouts.py's fixture exactly."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
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


async def _seed_tenant(factory, name, gstin=None, legal_name=None, invoice_prefix=None):
    from backend.database.models import Tenant, User

    async with factory() as db:
        tenant = Tenant(name=name, gstin=gstin, legal_name=legal_name, invoice_prefix=invoice_prefix)
        db.add(tenant)
        await db.flush()
        cpo = User(
            tenant_id=tenant.id,
            email=f"cpo-{name.lower()}@example.com",
            hashed_password="x",
            full_name="CPO Test",
            role=UserRole.CPO,
        )
        db.add(cpo)
        await db.commit()
        return tenant.id, cpo.id


async def _seed_session(
    factory, tenant_id, coins_spent, status=SessionStatus.COMPLETED,
    ended_at=None, energy_kwh=1.0, rate=Decimal("5.00"),
):
    """A minimal finished (by default) session attributed to tenant_id, with
    its own throwaway driver/gateway/plug. Mirrors test_payouts.py's
    _seed_session, extended with energy_kwh/rate (needed for the invoice
    line snapshot) and returning driver_user_id (needed for ownership
    checks)."""
    from backend.database.models import (
        ChargingSession,
        Gateway,
        GatewayStatus,
        Plug,
        PlugStatus,
        User,
    )

    n = next(_seed_counter)
    if ended_at is None and status != SessionStatus.ACTIVE:
        ended_at = datetime.now(timezone.utc)
    started_at = (ended_at - timedelta(hours=1)) if ended_at else datetime.now(timezone.utc)

    async with factory() as db:
        driver = User(
            email=f"driver-{n}@example.com",
            hashed_password="x",
            full_name=f"Driver {n}",
            role=UserRole.DRIVER,
        )
        gw = Gateway(
            id=f"gw-inv-{n}",
            tenant_id=tenant_id,
            name="GW",
            vpn_ip=f"10.9.{n // 250}.{n % 250 + 1}",
            status=GatewayStatus.ONLINE,
        )
        plug = Plug(gateway_id=gw.id, name="Plug", local_ip="10.9.1.1", status=PlugStatus.AVAILABLE)
        session = ChargingSession(
            tenant_id=tenant_id,
            user=driver,
            plug=plug,
            started_at=started_at,
            ended_at=ended_at,
            energy_kwh=energy_kwh,
            coins_spent=Decimal(coins_spent),
            rate_coins_per_kwh=rate,
            status=status,
        )
        db.add(gw)
        db.add(driver)
        db.add(plug)
        db.add(session)
        await db.commit()
        return session.id, driver.id


def _user_stub(user_id, tenant_id, role):
    """A pre-authorized User stand-in for calling router functions directly
    (bypasses FastAPI DI, matches test_pricing.py / test_payouts.py)."""
    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.role = role
    u.email = f"user{user_id}@amphive.test"
    return u


# --- 1. GST split + seller/line snapshot, end to end ---------------------------

@requires_db
@pytest.mark.asyncio
async def test_issue_invoice_computes_gst_split_and_snapshots_seller_and_line(factory, monkeypatch):
    monkeypatch.delenv("GST_RATE_PCT", raising=False)  # default 18%
    tenant_id, _ = await _seed_tenant(
        factory, "AcmeCo", gstin="29ABCDE1234F1Z5", legal_name="Acme Charging Pvt Ltd",
        invoice_prefix="ACME",
    )
    session_id, driver_id = await _seed_session(
        factory, tenant_id, "118.00", energy_kwh=2.5, rate=Decimal("10.00"),
    )

    async with factory() as db:
        invoice = await invoices_service.issue_invoice_for_session(db, session_id)

    assert invoice.session_id == session_id
    assert invoice.tenant_id == tenant_id
    assert invoice.driver_user_id == driver_id
    assert invoice.amount_coins == Decimal("118.00")
    assert invoice.taxable_value_inr == Decimal("100.00")
    assert invoice.gst_rate_pct == Decimal("18.00")
    assert invoice.gst_amount_inr == Decimal("18.00")
    assert invoice.total_inr == Decimal("118.00")
    assert invoice.seller_legal_name == "Acme Charging Pvt Ltd"
    assert invoice.seller_gstin == "29ABCDE1234F1Z5"
    assert invoice.energy_kwh == pytest.approx(2.5)
    assert invoice.rate_coins_per_kwh == Decimal("10.00")
    assert invoice.invoice_number.startswith("ACME-")
    assert invoice.invoice_number.endswith("-00001")


@requires_db
@pytest.mark.asyncio
async def test_invoice_prefix_falls_back_to_tenant_scoped_default(factory):
    """No invoice_prefix configured -> falls back to "INV<tenant_id>", not a
    bare "INV": invoice_number is GLOBALLY unique, so two different
    unconfigured tenants must not collide on the same fallback prefix (see
    services/invoices.py issue_invoice_for_session)."""
    tenant_id, _ = await _seed_tenant(factory, "NoPrefixCo")
    session_id, _ = await _seed_session(factory, tenant_id, "50.00")

    async with factory() as db:
        invoice = await invoices_service.issue_invoice_for_session(db, session_id)

    assert invoice.invoice_number.startswith(f"INV{tenant_id}-")


@requires_db
@pytest.mark.asyncio
async def test_two_unconfigured_tenants_do_not_collide_on_invoice_number(factory):
    """The bug this fallback design specifically avoids: two DIFFERENT
    tenants, neither with a custom invoice_prefix, each issuing their FIRST
    invoice ever (seq=1) in the same financial year would, with a bare
    "INV" fallback, both compute "INV-<FY>-00001" -- a real collision on
    the globally-unique invoice_number column, not just a theoretical race."""
    tenant_a, _ = await _seed_tenant(factory, "UnconfigA")
    tenant_b, _ = await _seed_tenant(factory, "UnconfigB")
    session_a, _ = await _seed_session(factory, tenant_a, "50.00")
    session_b, _ = await _seed_session(factory, tenant_b, "75.00")

    async with factory() as db:
        invoice_a = await invoices_service.issue_invoice_for_session(db, session_a)
    async with factory() as db:
        invoice_b = await invoices_service.issue_invoice_for_session(db, session_b)

    assert invoice_a.invoice_number != invoice_b.invoice_number


# --- 2. Idempotency --------------------------------------------------------------

@requires_db
@pytest.mark.asyncio
async def test_second_call_is_idempotent_same_invoice_number(factory):
    tenant_id, _ = await _seed_tenant(factory, "IdemCo")
    session_id, _ = await _seed_session(factory, tenant_id, "59.00")

    async with factory() as db:
        first = await invoices_service.issue_invoice_for_session(db, session_id)
    async with factory() as db:
        second = await invoices_service.issue_invoice_for_session(db, session_id)

    assert first.id == second.id
    assert first.invoice_number == second.invoice_number

    async with factory() as db:
        from sqlalchemy import func, select

        from backend.database.models import Invoice
        count = (
            await db.execute(select(func.count(Invoice.id)).where(Invoice.session_id == session_id))
        ).scalar()
    assert count == 1


# --- 3. Sequential numbering under the tenant lock --------------------------------

@requires_db
@pytest.mark.asyncio
async def test_concurrent_first_issues_for_the_same_session_mint_only_one_invoice(factory):
    """The core idempotency race guarantee: two simultaneous FIRST-issue
    calls for the SAME session must not both mint an invoice. Both pre-check
    SELECTs can race and both miss -- the safety net is the UNIQUE
    session_id constraint + IntegrityError catch, not the pre-check. Exactly
    one Invoice row must exist afterwards, and the loser must return the
    winner's (identical) invoice_number."""
    tenant_id, _ = await _seed_tenant(factory, "RaceCo")
    session_id, _ = await _seed_session(factory, tenant_id, "50.00")

    outcomes = []

    async def attempt():
        async with factory() as db:
            inv = await invoices_service.issue_invoice_for_session(db, session_id)
            outcomes.append(inv.invoice_number)

    await asyncio.gather(attempt(), attempt())

    assert len(outcomes) == 2
    assert outcomes[0] == outcomes[1]

    async with factory() as db:
        from sqlalchemy import func, select

        from backend.database.models import Invoice
        count = (
            await db.execute(select(func.count(Invoice.id)).where(Invoice.session_id == session_id))
        ).scalar()
    assert count == 1


@requires_db
@pytest.mark.asyncio
async def test_concurrent_issues_for_different_sessions_get_sequential_numbers(factory):
    """Two DIFFERENT sessions of the SAME tenant, invoiced at the same
    instant, must still get distinct sequential numbers: the tenant row lock
    (SELECT ... FOR UPDATE on Tenant.next_invoice_seq) serializes ALL
    concurrent issues for one tenant, not just same-session racers. Mirrors
    test_payouts.py's test_concurrent_payout_requests_serialize_no_double_settlement."""
    from sqlalchemy import select

    from backend.database.models import Tenant

    tenant_id, _ = await _seed_tenant(factory, "SeqCo", invoice_prefix="SEQ")
    session_a, _ = await _seed_session(factory, tenant_id, "10.00")
    session_b, _ = await _seed_session(factory, tenant_id, "20.00")

    outcomes = []

    async def attempt(session_id):
        async with factory() as db:
            inv = await invoices_service.issue_invoice_for_session(db, session_id)
            outcomes.append(inv.invoice_number)

    await asyncio.gather(attempt(session_a), attempt(session_b))

    assert len(outcomes) == 2
    assert outcomes[0] != outcomes[1]
    seqs = sorted(int(number.rsplit("-", 1)[1]) for number in outcomes)
    assert seqs == [1, 2]

    async with factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
    assert tenant.next_invoice_seq == 3


# --- 4. Unfinished / nothing-billed sessions --------------------------------------

@requires_db
@pytest.mark.asyncio
async def test_active_session_is_not_invoiceable(factory):
    tenant_id, _ = await _seed_tenant(factory, "ActiveCo")
    session_id, _ = await _seed_session(
        factory, tenant_id, "0.00", status=SessionStatus.ACTIVE, ended_at=None,
    )

    async with factory() as db:
        with pytest.raises(invoices_service.SessionNotInvoiceableError):
            await invoices_service.issue_invoice_for_session(db, session_id)


@requires_db
@pytest.mark.asyncio
async def test_completed_session_with_nothing_billed_is_not_invoiceable(factory):
    tenant_id, _ = await _seed_tenant(factory, "FreeCo")
    session_id, _ = await _seed_session(factory, tenant_id, "0.00", status=SessionStatus.COMPLETED)

    async with factory() as db:
        with pytest.raises(invoices_service.SessionNotInvoiceableError):
            await invoices_service.issue_invoice_for_session(db, session_id)


@requires_db
@pytest.mark.asyncio
async def test_nonexistent_session_is_not_invoiceable(factory):
    async with factory() as db:
        with pytest.raises(invoices_service.SessionNotInvoiceableError):
            await invoices_service.issue_invoice_for_session(db, 999999)


@requires_db
@pytest.mark.asyncio
async def test_unfinished_session_returns_400_via_router(factory):
    tenant_id, _ = await _seed_tenant(factory, "Unfin400Co")
    session_id, driver_id = await _seed_session(
        factory, tenant_id, "0.00", status=SessionStatus.ACTIVE, ended_at=None,
    )
    driver = _user_stub(driver_id, tenant_id=None, role=UserRole.DRIVER)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await get_session_invoice(session_id=session_id, format=None, user=driver, db=db)
    assert exc.value.status_code == 400


# --- 5. Access control (positive controls) + html format --------------------------

@requires_db
@pytest.mark.asyncio
async def test_owning_driver_can_fetch_and_issue_invoice(factory):
    tenant_id, _ = await _seed_tenant(factory, "DriverOkCo")
    session_id, driver_id = await _seed_session(factory, tenant_id, "25.00")
    driver = _user_stub(driver_id, tenant_id=None, role=UserRole.DRIVER)

    async with factory() as db:
        result = await get_session_invoice(session_id=session_id, format=None, user=driver, db=db)

    assert result["session_id"] == session_id
    assert result["total_inr"] == 25.0


@requires_db
@pytest.mark.asyncio
async def test_same_tenant_cpo_can_fetch_invoice(factory):
    tenant_id, cpo_id = await _seed_tenant(factory, "CpoOkCo")
    session_id, _ = await _seed_session(factory, tenant_id, "25.00")
    cpo = _user_stub(cpo_id, tenant_id=tenant_id, role=UserRole.CPO)

    async with factory() as db:
        result = await get_session_invoice(session_id=session_id, format=None, user=cpo, db=db)

    assert result["session_id"] == session_id


@requires_db
@pytest.mark.asyncio
async def test_admin_can_fetch_any_tenants_invoice(factory):
    tenant_id, _ = await _seed_tenant(factory, "AdminOkCo")
    session_id, _ = await _seed_session(factory, tenant_id, "25.00")
    admin = _user_stub(9999, tenant_id=None, role=UserRole.ADMIN)

    async with factory() as db:
        result = await get_session_invoice(session_id=session_id, format=None, user=admin, db=db)

    assert result["session_id"] == session_id


@requires_db
@pytest.mark.asyncio
async def test_html_format_renders_printable_invoice(factory):
    from fastapi.responses import HTMLResponse

    tenant_id, _ = await _seed_tenant(factory, "HtmlCo", legal_name="Html Charging Co")
    session_id, driver_id = await _seed_session(factory, tenant_id, "50.00")
    driver = _user_stub(driver_id, tenant_id=None, role=UserRole.DRIVER)

    async with factory() as db:
        result = await get_session_invoice(session_id=session_id, format="html", user=driver, db=db)

    assert isinstance(result, HTMLResponse)
    body = result.body.decode()
    assert "Tax Invoice" in body
    assert "Html Charging Co" in body


# --- 6. CPO invoice listing --------------------------------------------------------

@requires_db
@pytest.mark.asyncio
async def test_cpo_list_invoices_is_tenant_scoped_and_newest_first(factory):
    tenant_a, cpo_a_id = await _seed_tenant(factory, "ListCoA")
    tenant_b, _ = await _seed_tenant(factory, "ListCoB")

    session_1, _ = await _seed_session(factory, tenant_a, "10.00")
    session_2, _ = await _seed_session(factory, tenant_a, "20.00")
    session_3, _ = await _seed_session(factory, tenant_b, "99.00")

    async with factory() as db:
        await invoices_service.issue_invoice_for_session(db, session_1)
    async with factory() as db:
        await invoices_service.issue_invoice_for_session(db, session_2)
    async with factory() as db:
        await invoices_service.issue_invoice_for_session(db, session_3)

    cpo_a = _user_stub(cpo_a_id, tenant_id=tenant_a, role=UserRole.CPO)
    async with factory() as db:
        result = await cpo_list_invoices(user=cpo_a, db=db, limit=50, offset=0)

    # [redesign/ui-v3 contract §4] paginated {total, items} shape
    assert result["total"] == 2
    assert [r["session_id"] for r in result["items"]] == [session_2, session_1]
