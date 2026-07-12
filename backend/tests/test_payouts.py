"""
Tests for the CPO payout / settlement ledger (services/payouts.py +
routers/cpo.py GET /api/cpo/earnings, POST/GET /api/cpo/payouts,
POST /api/cpo/payouts/{id}/mark_paid|cancel).

Two tiers, mirroring the rest of this suite:

1. Pure-function + mocked-db tests (no DB, always run): fee-split math
   (services.payouts.compute_fee_and_net) and the RBAC/ownership branches
   that don't need real data — mark_paid's admin-only gate, cancel's
   cross-tenant block, and the "already settled" 409s. Same style as
   test_gateway_ota.py / test_max_active_sessions.py. Also the audit-trail
   shape: mark_paid/cancel stage a payout.* AuditLog row into the PAYOUT's
   tenant (not the admin actor's, which may be None), and the 409 paths
   stage none.
2. DB-gated tests (need a real PostgreSQL — CI's postgres:15 service,
   exported as TEST_DATABASE_URL; skipped locally, same as test_wallet.py /
   test_migrations.py): the earnings SQL aggregate itself (window + status
   filtering), the watermark (advances on request, CANCELLED frees it),
   double-request 409, and — the one that actually needs a real database —
   concurrent-request serialization via the tenant row lock, proving two
   simultaneous payout requests for the same tenant can't double-settle the
   same window. The lifecycle tests also assert the persisted audit trail:
   exactly one payout.request / payout.mark_paid / payout.cancel AuditLog
   row per successful transition (409 replays add none).
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

from backend.database.models import PayoutStatus, SessionStatus, UserRole
from backend.routers.cpo import cpo_cancel_payout, cpo_mark_payout_paid, cpo_request_payout
from backend.services import payouts as payout_service
from backend.services.rbac import require_role

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

def test_compute_fee_and_net_default_rate(monkeypatch):
    monkeypatch.delenv("PLATFORM_FEE_PCT", raising=False)
    fee, net = payout_service.compute_fee_and_net(Decimal("100.00"))
    assert fee == Decimal("10.00")
    assert net == Decimal("90.00")
    assert fee + net == Decimal("100.00")


def test_compute_fee_and_net_rounds_half_up(monkeypatch):
    """0.05 coins at a 50% fee rate = 0.025 raw fee -- to_money's ROUND_HALF_UP
    must push that to 0.03, not truncate to 0.02, and fee + net must still
    foot to the original gross to the cent."""
    monkeypatch.setenv("PLATFORM_FEE_PCT", "50")
    fee, net = payout_service.compute_fee_and_net(Decimal("0.05"))
    assert fee == Decimal("0.03")
    assert net == Decimal("0.02")
    assert fee + net == Decimal("0.05")


def test_platform_fee_pct_default_when_unset(monkeypatch):
    monkeypatch.delenv("PLATFORM_FEE_PCT", raising=False)
    assert payout_service.platform_fee_pct() == Decimal("10.0")


def test_platform_fee_pct_falls_back_on_garbage_env(monkeypatch):
    monkeypatch.setenv("PLATFORM_FEE_PCT", "not-a-number")
    assert payout_service.platform_fee_pct() == Decimal("10.0")


def _payout_mock(id=1, tenant_id=1, status=PayoutStatus.REQUESTED):
    p = MagicMock()
    p.id = id
    p.tenant_id = tenant_id
    p.status = status
    p.period_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p.period_end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    p.gross_coins = Decimal("10.00")
    p.platform_fee_coins = Decimal("1.00")
    p.net_coins = Decimal("9.00")
    p.requested_by_user_id = 1
    p.requested_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p.paid_at = None
    p.note = None
    return p


def _stub_user(tenant_id, role=UserRole.CPO, user_id=1):
    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.role = role
    u.email = "user@example.com"
    return u


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    # Session.add is synchronous — a bare AsyncMock attribute would mint an
    # un-awaited coroutine per call (RuntimeWarning noise in the audit tests).
    db.add = MagicMock()
    return db


def _added_audit_rows(db):
    """AuditLog instances staged on the mocked session via db.add — what
    try_record_audit would persist (its commit is a separate transaction,
    so the staged row IS the audit write from the route's point of view)."""
    from backend.database.models import AuditLog
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog)]


@pytest.mark.asyncio
async def test_mark_paid_role_gate_rejects_cpo():
    """The security-relevant part of 'cpo can't mark_paid' is the
    require_role("admin") dependency the route is wired to -- calling the
    route function directly (as the tests below do) bypasses FastAPI's DI
    entirely, so this exercises the actual gate."""
    checker = require_role("admin")
    cpo_user = _stub_user(tenant_id=1, role=UserRole.CPO)
    with pytest.raises(HTTPException) as exc:
        await checker(user=cpo_user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_mark_paid_role_gate_accepts_admin():
    checker = require_role("admin")
    admin_user = _stub_user(tenant_id=None, role=UserRole.ADMIN)
    result = await checker(user=admin_user)
    assert result is admin_user


@pytest.mark.asyncio
async def test_cancel_blocks_cross_tenant_cpo():
    payout = _payout_mock(tenant_id=2, status=PayoutStatus.REQUESTED)
    db = _db(_result(payout))
    intruder = _stub_user(tenant_id=1, role=UserRole.CPO)

    with pytest.raises(HTTPException) as exc:
        await cpo_cancel_payout(payout_id=payout.id, user=intruder, db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_allows_admin_across_tenants():
    payout = _payout_mock(tenant_id=2, status=PayoutStatus.REQUESTED)
    db = _db(_result(payout))
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN)

    result = await cpo_cancel_payout(payout_id=payout.id, user=admin, db=db)

    assert result["status"] == "cancelled"
    # Two commits: the cancel itself, then the payout.cancel audit row
    # (try_record_audit commits in its own transaction so an audit failure
    # can never undo the already-committed action).
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_cancel_audits_into_the_payouts_tenant_not_the_admins():
    """The payout.cancel audit row must land in the PAYOUT's tenant so the
    owning CPO sees it in GET /api/cpo/audit — the admin actor may have no
    tenant_id at all (AuditLog.tenant_id is NOT NULL, so using the actor's
    would also just break)."""
    payout = _payout_mock(id=7, tenant_id=2, status=PayoutStatus.REQUESTED)
    db = _db(_result(payout))
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN, user_id=42)

    await cpo_cancel_payout(payout_id=payout.id, user=admin, db=db)

    rows = _added_audit_rows(db)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.action == "payout.cancel"
    assert entry.target_type == "payout"
    assert entry.target_id == "7"
    assert entry.tenant_id == 2  # the payout's tenant, not the admin's (None)
    assert entry.actor_user_id == 42
    # Detail carries the settlement window + net, per the key=value convention.
    assert payout.period_start.isoformat() in entry.detail
    assert payout.period_end.isoformat() in entry.detail
    assert "net=9.00" in entry.detail


@pytest.mark.asyncio
async def test_mark_paid_audits_payout_mark_paid():
    payout = _payout_mock(id=11, tenant_id=3, status=PayoutStatus.REQUESTED)
    db = _db(_result(payout))
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN, user_id=99)

    result = await cpo_mark_payout_paid(payout_id=payout.id, user=admin, db=db)
    assert result["status"] == "paid"

    rows = _added_audit_rows(db)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.action == "payout.mark_paid"
    assert entry.target_type == "payout"
    assert entry.target_id == "11"
    assert entry.tenant_id == 3
    assert entry.actor_user_id == 99
    assert payout.period_start.isoformat() in entry.detail
    assert "net=9.00" in entry.detail


@pytest.mark.asyncio
async def test_mark_paid_409_when_already_paid():
    payout = _payout_mock(status=PayoutStatus.PAID)
    db = _db(_result(payout))
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN)

    with pytest.raises(HTTPException) as exc:
        await cpo_mark_payout_paid(payout_id=payout.id, user=admin, db=db)
    assert exc.value.status_code == 409
    assert _added_audit_rows(db) == []  # a rejected replay is not audited


@pytest.mark.asyncio
async def test_cancel_409_when_already_cancelled():
    payout = _payout_mock(tenant_id=1, status=PayoutStatus.CANCELLED)
    db = _db(_result(payout))
    owner = _stub_user(tenant_id=1, role=UserRole.CPO)

    with pytest.raises(HTTPException) as exc:
        await cpo_cancel_payout(payout_id=payout.id, user=owner, db=db)
    assert exc.value.status_code == 409
    assert _added_audit_rows(db) == []  # a rejected replay is not audited


@pytest.mark.asyncio
async def test_request_payout_400_without_a_tenant():
    """A bare admin (no tenant_id) hits the same guard cpo_setup uses."""
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await cpo_request_payout(user=admin, db=AsyncMock())
    assert exc.value.status_code == 400


# ===========================================================================
# Tier 2: DB-gated (real Postgres)
# ===========================================================================

_seed_counter = itertools.count(1)


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory. Mirrors
    test_wallet.py's fixture exactly (including the belt-and-braces enum
    drop loop, extended here with payout_status)."""
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


async def _seed_tenant(factory, name):
    from backend.database.models import Tenant, User

    async with factory() as db:
        tenant = Tenant(name=name)
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


async def _seed_session(factory, tenant_id, coins_spent, status, ended_at=None):
    """A minimal session attributed to tenant_id. Gateway/plug/driver rows
    are throwaway, just to satisfy FK constraints -- the earnings query only
    reads ChargingSession.tenant_id/status/ended_at/coins_spent (see
    services/payouts.py's module docstring for why no join is needed)."""
    from backend.database.models import ChargingSession, Gateway, GatewayStatus, Plug, PlugStatus, User

    n = next(_seed_counter)
    async with factory() as db:
        driver = User(
            email=f"driver-{n}@example.com",
            hashed_password="x",
            full_name="Driver Test",
            role=UserRole.DRIVER,
        )
        gw = Gateway(
            id=f"gw-{n}",
            tenant_id=tenant_id,
            name="GW",
            vpn_ip=f"10.0.{n // 250}.{n % 250 + 1}",
            status=GatewayStatus.ONLINE,
        )
        plug = Plug(
            gateway_id=gw.id,
            name="Plug",
            local_ip="10.0.1.1",
            status=PlugStatus.AVAILABLE,
        )
        started_at = (ended_at - timedelta(hours=1)) if ended_at else datetime.now(timezone.utc)
        session = ChargingSession(
            tenant_id=tenant_id,
            user=driver,
            plug=plug,
            started_at=started_at,
            ended_at=ended_at,
            energy_kwh=1.0,
            coins_spent=Decimal(coins_spent),
            status=status,
        )
        db.add(gw)
        db.add(driver)
        db.add(plug)
        db.add(session)
        await db.commit()
        return session.id


def _cpo_user(tenant_id, user_id, role=UserRole.CPO):
    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.role = role
    u.email = f"user{user_id}@example.com"
    return u


async def _audit_rows(factory, tenant_id, action):
    """Persisted AuditLog rows for one tenant + action (DB-gated tier)."""
    from sqlalchemy import and_, select

    from backend.database.models import AuditLog

    async with factory() as db:
        result = await db.execute(
            select(AuditLog).where(
                and_(AuditLog.tenant_id == tenant_id, AuditLog.action == action)
            )
        )
        return result.scalars().all()


@requires_db
@pytest.mark.asyncio
async def test_gross_earnings_sums_only_completed_sessions_in_window(factory):
    tenant_id, _ = await _seed_tenant(factory, "WindowCo")
    now = datetime.now(timezone.utc)
    in_window = now - timedelta(days=1)
    before_window = now - timedelta(days=10)

    await _seed_session(factory, tenant_id, "10.00", SessionStatus.COMPLETED, ended_at=in_window)
    await _seed_session(factory, tenant_id, "7.50", SessionStatus.COMPLETED, ended_at=in_window)
    # Outside the requested window -- must not be counted.
    await _seed_session(factory, tenant_id, "999.00", SessionStatus.COMPLETED, ended_at=before_window)
    # Not COMPLETED -- must not be counted even though it's "in window".
    await _seed_session(factory, tenant_id, "50.00", SessionStatus.ACTIVE, ended_at=None)

    async with factory() as db:
        gross = await payout_service.sum_completed_session_coins(
            db, tenant_id,
            window_start=now - timedelta(days=2),
            window_end=now + timedelta(minutes=1),
        )

    assert gross == Decimal("17.50")


@requires_db
@pytest.mark.asyncio
async def test_watermark_advances_after_payout_and_cancel_frees_window(factory):
    from backend.database.models import Payout

    tenant_id, user_id = await _seed_tenant(factory, "WatermarkCo")

    async with factory() as db:
        assert await payout_service.tenant_settlement_watermark(db, tenant_id) == payout_service.EPOCH

    period_end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with factory() as db:
        payout = Payout(
            tenant_id=tenant_id,
            period_start=payout_service.EPOCH,
            period_end=period_end,
            gross_coins=Decimal("10.00"),
            platform_fee_coins=Decimal("1.00"),
            net_coins=Decimal("9.00"),
            status=PayoutStatus.REQUESTED,
            requested_by_user_id=user_id,
        )
        db.add(payout)
        await db.commit()
        payout_id = payout.id

    async with factory() as db:
        assert await payout_service.tenant_settlement_watermark(db, tenant_id) == period_end

    async with factory() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Payout).where(Payout.id == payout_id))).scalar_one()
        p.status = PayoutStatus.CANCELLED
        await db.commit()

    async with factory() as db:
        # CANCELLED is excluded from the watermark -- the window is freed.
        assert await payout_service.tenant_settlement_watermark(db, tenant_id) == payout_service.EPOCH


@requires_db
@pytest.mark.asyncio
async def test_request_payout_400_when_nothing_unsettled(factory):
    tenant_id, user_id = await _seed_tenant(factory, "EmptyCo")
    user = _cpo_user(tenant_id, user_id)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_request_payout(user=user, db=db)
    assert exc.value.status_code == 400


@requires_db
@pytest.mark.asyncio
async def test_second_payout_request_conflicts_while_first_pending(factory):
    tenant_id, user_id = await _seed_tenant(factory, "DoubleReqCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "20.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    user = _cpo_user(tenant_id, user_id)

    async with factory() as db:
        first = await cpo_request_payout(user=user, db=db)
    assert first["status"] == "requested"
    assert first["net_coins"] == pytest.approx(18.00)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_request_payout(user=user, db=db)
    assert exc.value.status_code == 409

    # Exactly one payout.request audit row: the successful request wrote it
    # (window + money in the detail), the 409 replay did not.
    rows = await _audit_rows(factory, tenant_id, "payout.request")
    assert len(rows) == 1
    assert rows[0].target_type == "payout"
    assert rows[0].target_id == str(first["id"])
    assert rows[0].actor_user_id == user_id
    assert "gross=20.00" in rows[0].detail
    assert "net=18.00" in rows[0].detail


@requires_db
@pytest.mark.asyncio
async def test_concurrent_payout_requests_serialize_no_double_settlement(factory):
    """The core race guarantee: two simultaneous requests for the same
    tenant must not both snapshot the unsettled window. The tenant row lock
    (SELECT ... FOR UPDATE in cpo_request_payout) forces one to wait for the
    other's commit; exactly one Payout row must exist afterwards."""
    from sqlalchemy import func, select

    from backend.database.models import Payout

    tenant_id, user_id = await _seed_tenant(factory, "ConcurrentCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "40.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    user = _cpo_user(tenant_id, user_id)

    outcomes = []

    async def attempt():
        async with factory() as db:
            try:
                res = await cpo_request_payout(user=user, db=db)
                outcomes.append(("ok", res))
            except HTTPException as exc:
                outcomes.append(("err", exc.status_code))

    await asyncio.gather(attempt(), attempt())

    kinds = sorted(k for k, _ in outcomes)
    assert kinds == ["err", "ok"]
    assert [v for k, v in outcomes if k == "err"][0] == 409

    async with factory() as db:
        count = (
            await db.execute(select(func.count(Payout.id)).where(Payout.tenant_id == tenant_id))
        ).scalar()
    assert count == 1


@requires_db
@pytest.mark.asyncio
async def test_mark_paid_transitions_then_conflicts_on_replay(factory):
    tenant_id, user_id = await _seed_tenant(factory, "MarkPaidCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "30.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    cpo_user = _cpo_user(tenant_id, user_id)
    admin_user = _cpo_user(None, 999, role=UserRole.ADMIN)

    async with factory() as db:
        requested = await cpo_request_payout(user=cpo_user, db=db)

    async with factory() as db:
        paid = await cpo_mark_payout_paid(payout_id=requested["id"], user=admin_user, db=db)
    assert paid["status"] == "paid"
    assert paid["paid_at"] is not None

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_mark_payout_paid(payout_id=requested["id"], user=admin_user, db=db)
    assert exc.value.status_code == 409

    # Exactly one payout.mark_paid audit row — the 409 replay added none.
    rows = await _audit_rows(factory, tenant_id, "payout.mark_paid")
    assert len(rows) == 1
    assert rows[0].target_id == str(requested["id"])
    assert rows[0].actor_user_id == admin_user.id
    assert "net=" in rows[0].detail


@requires_db
@pytest.mark.asyncio
async def test_cancel_frees_window_for_a_new_request(factory):
    tenant_id, user_id = await _seed_tenant(factory, "CancelFreeCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "12.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    user = _cpo_user(tenant_id, user_id)

    async with factory() as db:
        first = await cpo_request_payout(user=user, db=db)

    async with factory() as db:
        cancelled = await cpo_cancel_payout(payout_id=first["id"], user=user, db=db)
    assert cancelled["status"] == "cancelled"

    # Window freed -- a new request re-covers the same unsettled earnings.
    async with factory() as db:
        second = await cpo_request_payout(user=user, db=db)
    assert second["status"] == "requested"
    assert second["net_coins"] == pytest.approx(first["net_coins"])

    # Full audit trail of the lifecycle: two requests, one cancel.
    cancel_rows = await _audit_rows(factory, tenant_id, "payout.cancel")
    assert [r.target_id for r in cancel_rows] == [str(first["id"])]
    request_rows = await _audit_rows(factory, tenant_id, "payout.request")
    assert sorted(r.target_id for r in request_rows) == sorted(
        [str(first["id"]), str(second["id"])]
    )
