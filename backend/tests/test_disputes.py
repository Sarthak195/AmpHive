"""
Session dispute / refund flow tests — need a real PostgreSQL (CI's
postgres:15 service, exported as TEST_DATABASE_URL). Skipped locally: this
repo's dev boxes run no database by policy (see test_wallet.py, the same
pattern).

What's proven here:

1. Dispute creation rules (POST /api/sessions/{id}/dispute): only the
   session's own driver may file, only against a finished (non-ACTIVE)
   session, and at most one OPEN dispute per session — both via the router's
   check-then-insert (409) AND via a direct concurrent-insert race against
   the partial unique index itself (DB-level backstop, not just app logic).
2. Approving a dispute (POST /api/cpo/disputes/{id}/resolve) credits the
   driver's coin wallet atomically (services/wallet.credit_wallet) and
   writes a REFUND LedgerTransaction that reconciles: `amount` matches the
   credited delta and `balance_after` matches the wallet's new balance
   (IMPLEMENTATION_STATUS.md §3.30's ledger-reconciliation bar).
3. The cumulative refund cap: a session's APPROVED refund_coins can never
   exceed its coins_spent, enforced both for a single over-large request and
   cumulatively across two different disputes on the same session.
4. Double-resolution is impossible: a resolved dispute 409s on a second
   resolve attempt, proven both sequentially and under real concurrent
   resolves of the *same* dispute (only one may ever credit the wallet).
5. Tenant scoping: a CPO can neither list nor resolve another tenant's
   dispute.
"""
import asyncio
import os
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional

import pytest
import pytest_asyncio
from fastapi import HTTPException

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "dispute_status",
]


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory."""
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

    # Mirror the app factory's expire_on_commit=False (backend/database/db.py).
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _user_obj(user_id: int, email: str, tenant_id: Optional[int] = None):
    """A stand-in for the get_current_user-injected ORM instance: the
    dispute routers only ever read .id / .email / .tenant_id off it."""
    return SimpleNamespace(id=user_id, email=email, tenant_id=tenant_id)


async def _seed_world(
    factory,
    *,
    coins_spent: str = "20.00",
    session_status=None,
    driver_balance: str = "0.00",
) -> dict:
    """One tenant with a CPO + driver + a charging session, ready to dispute.
    Returns the ids tests need. `session_status` defaults to COMPLETED."""
    from backend.database.models import (
        ChargingSession, Gateway, Plug, SessionStatus, Tenant, User, UserRole,
    )

    session_status = session_status or SessionStatus.COMPLETED
    tag = uuid.uuid4().hex[:10]

    async with factory() as db:
        tenant = Tenant(name=f"tenant-{tag}")
        db.add(tenant)
        await db.flush()

        cpo = User(
            email=f"cpo-{tag}@amphive.test", hashed_password="x",
            full_name="CPO Test", role=UserRole.CPO, tenant_id=tenant.id,
        )
        driver = User(
            email=f"driver-{tag}@amphive.test", hashed_password="x",
            full_name="Driver Test", role=UserRole.DRIVER,
            coin_balance=Decimal(driver_balance),
        )
        db.add_all([cpo, driver])
        await db.flush()

        # Three variable octets (~15.6M combinations): with a single random
        # octet (250 values), the tests that seed two worlds would collide on
        # the UNIQUE gateways.vpn_ip constraint in ~0.4% of runs — a flake.
        u = uuid.uuid4().int
        gateway = Gateway(
            id=f"gw-{tag}", tenant_id=tenant.id, name="Test GW",
            vpn_ip=f"10.{u % 250 + 1}.{(u >> 8) % 250 + 1}.{(u >> 16) % 250 + 1}",
        )
        db.add(gateway)
        await db.flush()

        plug = Plug(gateway_id=gateway.id, name="Test Plug", local_ip="10.0.1.1")
        db.add(plug)
        await db.flush()

        session = ChargingSession(
            tenant_id=tenant.id, user_id=driver.id, plug_id=plug.id,
            status=session_status, coins_spent=Decimal(coins_spent),
        )
        db.add(session)
        await db.commit()

        return {
            "tenant_id": tenant.id,
            "cpo_id": cpo.id,
            "driver_id": driver.id,
            "gateway_id": gateway.id,
            "plug_id": plug.id,
            "session_id": session.id,
        }


async def _seed_extra_driver(factory) -> int:
    from backend.database.models import User, UserRole

    async with factory() as db:
        driver = User(
            email=f"intruder-{uuid.uuid4().hex[:10]}@amphive.test",
            hashed_password="x", full_name="Intruder Driver", role=UserRole.DRIVER,
        )
        db.add(driver)
        await db.commit()
        return driver.id


async def _seed_open_dispute(factory, world: dict, reason: str = None) -> int:
    from backend.database.models import DisputeStatus, SessionDispute

    async with factory() as db:
        dispute = SessionDispute(
            session_id=world["session_id"],
            tenant_id=world["tenant_id"],
            driver_user_id=world["driver_id"],
            reason=reason or "Energy billed did not match what my car actually drew.",
            status=DisputeStatus.OPEN,
        )
        db.add(dispute)
        await db.commit()
        return dispute.id


async def _fresh_balance(factory, user_id: int) -> Decimal:
    from sqlalchemy import select

    from backend.database.models import User

    async with factory() as db:
        result = await db.execute(select(User.coin_balance).where(User.id == user_id))
        return result.scalar_one()


async def _fresh_dispute(factory, dispute_id: int):
    from sqlalchemy import select

    from backend.database.models import SessionDispute

    async with factory() as db:
        result = await db.execute(select(SessionDispute).where(SessionDispute.id == dispute_id))
        return result.scalar_one()


async def _ledger_rows(factory, session_id: int):
    from sqlalchemy import select

    from backend.database.models import LedgerTransaction

    async with factory() as db:
        result = await db.execute(
            select(LedgerTransaction)
            .where(LedgerTransaction.session_id == session_id)
            .order_by(LedgerTransaction.id)
        )
        return list(result.scalars().all())


# --- Pure schema validation (no DB needed, but kept alongside its peers) ---

def test_dispute_create_request_rejects_short_reason():
    import pydantic

    from backend.schemas import DisputeCreateRequest

    with pytest.raises(pydantic.ValidationError):
        DisputeCreateRequest(reason="short")


# --- Driver-side creation rules ---------------------------------------------

@pytest.mark.asyncio
async def test_dispute_requires_ownership(factory):
    from backend.routers.sessions import dispute_session
    from backend.schemas import DisputeCreateRequest

    world = await _seed_world(factory)
    intruder_id = await _seed_extra_driver(factory)
    intruder = _user_obj(intruder_id, "intruder@amphive.test")

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await dispute_session(
                world["session_id"],
                DisputeCreateRequest(reason="Trying to dispute a session I don't own."),
                intruder, db,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_dispute_requires_finished_session(factory):
    from backend.database.models import SessionStatus
    from backend.routers.sessions import dispute_session
    from backend.schemas import DisputeCreateRequest

    world = await _seed_world(factory, session_status=SessionStatus.ACTIVE)
    driver = _user_obj(world["driver_id"], "driver@amphive.test")

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await dispute_session(
                world["session_id"],
                DisputeCreateRequest(reason="This session is still actively charging."),
                driver, db,
            )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_dispute_creation_succeeds_for_finished_own_session(factory):
    from backend.routers.sessions import dispute_session
    from backend.schemas import DisputeCreateRequest

    world = await _seed_world(factory, coins_spent="12.50")
    driver = _user_obj(world["driver_id"], "driver@amphive.test")

    async with factory() as db:
        res = await dispute_session(
            world["session_id"],
            DisputeCreateRequest(reason="Billed for energy my car never actually received."),
            driver, db,
        )

    assert res.session_id == world["session_id"]
    assert res.tenant_id == world["tenant_id"]
    assert res.driver_user_id == world["driver_id"]
    assert res.status == "open"
    assert res.refund_coins is None
    assert res.resolved_at is None


@pytest.mark.asyncio
async def test_single_open_dispute_per_session_rejected_with_409(factory):
    from backend.routers.sessions import dispute_session
    from backend.schemas import DisputeCreateRequest

    world = await _seed_world(factory)
    driver = _user_obj(world["driver_id"], "driver@amphive.test")

    async with factory() as db:
        await dispute_session(
            world["session_id"],
            DisputeCreateRequest(reason="First dispute filed on this session."),
            driver, db,
        )

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await dispute_session(
                world["session_id"],
                DisputeCreateRequest(reason="Second attempt while the first is still open."),
                driver, db,
            )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_open_dispute_partial_unique_index_blocks_concurrent_duplicate(factory):
    """DB-level proof (not just the router's check-then-insert): two
    concurrent inserts of an OPEN dispute for the same session must not both
    succeed, even racing straight past any app-level pre-check."""
    from sqlalchemy.exc import IntegrityError

    from backend.database.models import DisputeStatus, SessionDispute

    world = await _seed_world(factory)

    async def _insert():
        async with factory() as db:
            db.add(SessionDispute(
                session_id=world["session_id"],
                tenant_id=world["tenant_id"],
                driver_user_id=world["driver_id"],
                reason="Concurrent duplicate-open-dispute race probe.",
                status=DisputeStatus.OPEN,
            ))
            try:
                await db.commit()
                return "ok"
            except IntegrityError:
                await db.rollback()
                return "blocked"

    outcomes = await asyncio.gather(_insert(), _insert())
    assert sorted(outcomes) == ["blocked", "ok"]


# --- CPO-side resolution: approve credits + ledger reconciliation ----------

@pytest.mark.asyncio
async def test_approve_default_refund_on_zero_cost_session_gives_clear_error(factory):
    """A session that never billed anything (e.g. CANCELLED, coins_spent=0)
    can't default-refund to 0 with a generic 'must be > 0' message — the CPO
    gets told why. Nothing is refundable at all on such a session (coins_spent
    IS the cap, unconditionally) — an explicit override doesn't bypass that
    either, it just fails via the (separately tested) cap-exceeded path
    instead of the default-refund-amount path."""
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="0.00", driver_balance="0.00")
    dispute_id = await _seed_open_dispute(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_dispute(dispute_id, CpoDisputeResolveRequest(action="approve"), cpo, db)
    assert exc.value.status_code == 400
    assert "nothing to refund" in exc.value.detail

    # An explicit override on the same zero-cost session hits the cap check
    # instead (0 refundable), not the default-amount message above.
    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_dispute(
                dispute_id, CpoDisputeResolveRequest(action="approve", refund_coins=2.00), cpo, db,
            )
    assert exc.value.status_code == 400
    assert "exceed" in exc.value.detail
    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("0.00")


@pytest.mark.asyncio
async def test_approve_credits_wallet_and_ledger_reconciles(factory):
    from backend.database.models import TransactionType
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="20.00", driver_balance="5.00")
    dispute_id = await _seed_open_dispute(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        res = await cpo_resolve_dispute(
            dispute_id,
            CpoDisputeResolveRequest(action="approve", note="Verified against telemetry."),
            cpo, db,
        )

    assert res.status == "approved"
    assert res.refund_coins == 20.00
    assert res.resolution_note == "Verified against telemetry."
    assert res.resolved_by_user_id == world["cpo_id"]
    assert res.resolved_at is not None

    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("25.00")

    rows = await _ledger_rows(factory, world["session_id"])
    assert len(rows) == 1
    assert rows[0].transaction_type == TransactionType.REFUND
    assert rows[0].amount == Decimal("20.00")
    assert rows[0].balance_after == Decimal("25.00")
    assert rows[0].user_id == world["driver_id"]


@pytest.mark.asyncio
async def test_approve_with_explicit_partial_refund(factory):
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="20.00", driver_balance="0.00")
    dispute_id = await _seed_open_dispute(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        res = await cpo_resolve_dispute(
            dispute_id, CpoDisputeResolveRequest(action="approve", refund_coins=5.00), cpo, db,
        )

    assert res.refund_coins == 5.00
    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("5.00")


@pytest.mark.asyncio
async def test_reject_does_not_move_money(factory):
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="10.00", driver_balance="3.00")
    dispute_id = await _seed_open_dispute(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        res = await cpo_resolve_dispute(
            dispute_id, CpoDisputeResolveRequest(action="reject", note="Telemetry matches the bill."), cpo, db,
        )

    assert res.status == "rejected"
    assert res.refund_coins is None
    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("3.00")
    assert await _ledger_rows(factory, world["session_id"]) == []


# --- Refund cap ---------------------------------------------------------

@pytest.mark.asyncio
async def test_refund_cap_rejects_amount_exceeding_session_cost(factory):
    from backend.database.models import DisputeStatus
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="10.00", driver_balance="0.00")
    dispute_id = await _seed_open_dispute(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_dispute(
                dispute_id, CpoDisputeResolveRequest(action="approve", refund_coins=15.00), cpo, db,
            )
    assert exc.value.status_code == 400

    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("0.00")
    dispute = await _fresh_dispute(factory, dispute_id)
    assert dispute.status == DisputeStatus.OPEN


@pytest.mark.asyncio
async def test_refund_cap_enforced_cumulatively_across_disputes(factory):
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="10.00", driver_balance="0.00")
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    dispute1 = await _seed_open_dispute(factory, world, reason="First partial-refund dispute on this session.")
    async with factory() as db:
        await cpo_resolve_dispute(
            dispute1, CpoDisputeResolveRequest(action="approve", refund_coins=6.00), cpo, db,
        )

    # dispute1 is resolved (no longer OPEN), so a second dispute may now be
    # filed on the same session — its refund is capped by what's left: 4.00.
    dispute2 = await _seed_open_dispute(factory, world, reason="Second, separate dispute on the same session.")

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_dispute(
                dispute2, CpoDisputeResolveRequest(action="approve", refund_coins=5.00), cpo, db,
            )
    assert exc.value.status_code == 400

    async with factory() as db:
        res = await cpo_resolve_dispute(
            dispute2, CpoDisputeResolveRequest(action="approve", refund_coins=4.00), cpo, db,
        )
    assert res.refund_coins == 4.00

    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("10.00")
    rows = await _ledger_rows(factory, world["session_id"])
    assert sum((r.amount for r in rows), Decimal("0.00")) == Decimal("10.00")


# --- Double-resolution is impossible ----------------------------------------

@pytest.mark.asyncio
async def test_double_resolve_blocked(factory):
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="10.00")
    dispute_id = await _seed_open_dispute(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        await cpo_resolve_dispute(
            dispute_id, CpoDisputeResolveRequest(action="reject", note="Not valid."), cpo, db,
        )

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_dispute(dispute_id, CpoDisputeResolveRequest(action="approve"), cpo, db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_double_resolve_credits_wallet_exactly_once(factory):
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="20.00", driver_balance="0.00")
    dispute_id = await _seed_open_dispute(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async def _approve():
        async with factory() as db:
            try:
                await cpo_resolve_dispute(dispute_id, CpoDisputeResolveRequest(action="approve"), cpo, db)
                return "ok"
            except HTTPException as exc:
                assert exc.status_code == 409
                return "blocked"

    outcomes = await asyncio.gather(_approve(), _approve())
    assert sorted(outcomes) == ["blocked", "ok"]
    # Credited exactly once, never twice.
    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("20.00")


@pytest.mark.asyncio
async def test_resolve_rejects_invalid_action(factory):
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory)
    dispute_id = await _seed_open_dispute(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_dispute(dispute_id, CpoDisputeResolveRequest(action="delete"), cpo, db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_404_for_unknown_dispute(factory):
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_dispute(999999, CpoDisputeResolveRequest(action="approve"), cpo, db)
    assert exc.value.status_code == 404


# --- Tenant scoping ----------------------------------------------------

@pytest.mark.asyncio
async def test_cpo_list_disputes_is_tenant_scoped(factory):
    from backend.routers.cpo import cpo_list_disputes

    world1 = await _seed_world(factory, coins_spent="10.00")
    world2 = await _seed_world(factory, coins_spent="10.00")
    dispute1 = await _seed_open_dispute(factory, world1)
    dispute2 = await _seed_open_dispute(factory, world2)

    cpo1 = _user_obj(world1["cpo_id"], "cpo1@amphive.test", tenant_id=world1["tenant_id"])
    cpo2 = _user_obj(world2["cpo_id"], "cpo2@amphive.test", tenant_id=world2["tenant_id"])

    async with factory() as db:
        tenant1_disputes = await cpo_list_disputes(cpo1, db)
    async with factory() as db:
        tenant2_disputes = await cpo_list_disputes(cpo2, db)

    assert [d.id for d in tenant1_disputes] == [dispute1]
    assert [d.id for d in tenant2_disputes] == [dispute2]


@pytest.mark.asyncio
async def test_cpo_list_disputes_filters_by_status(factory):
    from backend.routers.cpo import cpo_list_disputes, cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world = await _seed_world(factory, coins_spent="10.00")
    rejected_id = await _seed_open_dispute(factory, world, reason="Will be rejected shortly.")
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        await cpo_resolve_dispute(rejected_id, CpoDisputeResolveRequest(action="reject", note="No."), cpo, db)

    still_open_id = await _seed_open_dispute(factory, world, reason="Second, still-open dispute on session.")

    async with factory() as db:
        open_only = await cpo_list_disputes(cpo, db, status_filter="open")
    async with factory() as db:
        rejected_only = await cpo_list_disputes(cpo, db, status_filter="rejected")

    assert [d.id for d in open_only] == [still_open_id]
    assert [d.id for d in rejected_only] == [rejected_id]


@pytest.mark.asyncio
async def test_cpo_list_disputes_rejects_invalid_status_filter(factory):
    from backend.routers.cpo import cpo_list_disputes

    world = await _seed_world(factory)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_list_disputes(cpo, db, status_filter="bogus")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_cpo_cannot_resolve_another_tenants_dispute(factory):
    from backend.database.models import DisputeStatus
    from backend.routers.cpo import cpo_resolve_dispute
    from backend.schemas import CpoDisputeResolveRequest

    world1 = await _seed_world(factory, coins_spent="10.00")
    world2 = await _seed_world(factory, coins_spent="10.00")
    dispute1 = await _seed_open_dispute(factory, world1)

    cpo2 = _user_obj(world2["cpo_id"], "cpo2@amphive.test", tenant_id=world2["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_dispute(dispute1, CpoDisputeResolveRequest(action="approve"), cpo2, db)
    assert exc.value.status_code == 404

    dispute = await _fresh_dispute(factory, dispute1)
    assert dispute.status == DisputeStatus.OPEN
    assert await _fresh_balance(factory, world1["driver_id"]) == Decimal("0.00")
