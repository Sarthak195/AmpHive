"""
Wallet write-path integrity tests — need a real PostgreSQL (CI's postgres:15
service, exported as TEST_DATABASE_URL). Skipped locally: this repo's dev
boxes run no database by policy.

What's proven here:

1. The stale-identity-map lost-update fix (services/wallet.py): a request
   session that loaded the User at auth time holds a cached instance whose
   balance predates concurrent writes. The old read-modify-write under
   ``select(User).with_for_update()`` re-read that cached value (SQLAlchemy
   returns the identity-mapped instance without refreshing attributes) and
   overwrote balance changes committed since auth. credit_wallet /
   debit_wallet_clamped must compute from the committed DB value even with a
   stale instance sitting in the session.
2. _credit_topup stays idempotent: the loser of a duplicate payment_id race
   rolls back, and the rollback undoes its DB-side balance bump.
3. Debits clamp at the available balance (never negative).
4. A concurrent credit and debit serialize on the row lock to the correct
   final balance.
"""
import asyncio
import os
from decimal import Decimal

import pytest
import pytest_asyncio

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]


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

    # Mirror the app factory's expire_on_commit=False (backend/database/db.py) —
    # it's what keeps identity-mapped instances stale across commits.
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed_user(factory, balance: str) -> int:
    from backend.database.models import User

    async with factory() as db:
        user = User(
            email="wallet-test@example.com",
            hashed_password="x",
            full_name="Wallet Test",
            coin_balance=Decimal(balance),
        )
        db.add(user)
        await db.commit()
        return user.id


async def _fresh_balance(factory, user_id: int) -> Decimal:
    from sqlalchemy import select

    from backend.database.models import User

    async with factory() as db:
        result = await db.execute(
            select(User.coin_balance).where(User.id == user_id)
        )
        return result.scalar_one()


async def _load_stale_instance(db, user_id: int):
    """Simulate get_current_user: pull the User entity into the session's
    identity map, so later re-selects return this cached (stale) instance."""
    from sqlalchemy import select

    from backend.database.models import User

    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one()


async def _set_balance_out_of_band(factory, user_id: int, balance: str):
    """A concurrent transaction (webhook worker, reaper) committing a write."""
    from sqlalchemy import update

    from backend.database.models import User

    async with factory() as db:
        await db.execute(
            update(User).where(User.id == user_id).values(coin_balance=Decimal(balance))
        )
        await db.commit()


@pytest.mark.asyncio
async def test_credit_topup_uses_db_balance_not_stale_identity_map(factory):
    """Regression: auth loads balance 100, a webhook credit lands (150), then
    /verify credits 10 — the result must be 160, not 110 (the stale instance's
    100 + 10, which silently erased the webhook's 50)."""
    from sqlalchemy import select

    from backend.database.models import LedgerTransaction
    from backend.routers.payments import _credit_topup

    uid = await _seed_user(factory, "100.00")

    async with factory() as req_db:
        stale = await _load_stale_instance(req_db, uid)
        assert stale.coin_balance == Decimal("100.00")

        await _set_balance_out_of_band(factory, uid, "150.00")

        new_balance = await _credit_topup(
            req_db, user_id=uid, coins=10.0, payment_id="pay_stale", description="t",
        )

    assert new_balance == Decimal("160.00")
    assert await _fresh_balance(factory, uid) == Decimal("160.00")

    async with factory() as db:
        ledger = (await db.execute(
            select(LedgerTransaction).where(
                LedgerTransaction.razorpay_payment_id == "pay_stale"
            )
        )).scalar_one()
        assert ledger.amount == Decimal("10.00")
        assert ledger.balance_after == Decimal("160.00")


@pytest.mark.asyncio
async def test_credit_topup_duplicate_rolls_back_the_balance_bump(factory):
    """The duplicate payment_id loser must return None AND leave the balance
    untouched — its DB-side UPDATE has to be undone by the rollback."""
    from backend.routers.payments import _credit_topup

    uid = await _seed_user(factory, "100.00")

    async with factory() as db:
        first = await _credit_topup(
            db, user_id=uid, coins=10.0, payment_id="pay_dup", description="t",
        )
    assert first == Decimal("110.00")

    async with factory() as db:
        second = await _credit_topup(
            db, user_id=uid, coins=10.0, payment_id="pay_dup", description="t",
        )
    assert second is None
    assert await _fresh_balance(factory, uid) == Decimal("110.00")


@pytest.mark.asyncio
async def test_credit_topup_unknown_user_returns_none(factory):
    from backend.routers.payments import _credit_topup

    await _seed_user(factory, "100.00")
    async with factory() as db:
        assert await _credit_topup(
            db, user_id=999999, coins=10.0, payment_id="pay_ghost", description="t",
        ) is None


@pytest.mark.asyncio
async def test_debit_reads_fresh_balance_despite_stale_instance(factory):
    """Same lost-update scenario on the debit side: the stop path must debit
    from the committed balance (150), not the auth-time snapshot (100)."""
    from backend.services.wallet import debit_wallet_clamped

    uid = await _seed_user(factory, "100.00")

    async with factory() as req_db:
        stale = await _load_stale_instance(req_db, uid)
        assert stale.coin_balance == Decimal("100.00")

        await _set_balance_out_of_band(factory, uid, "150.00")

        actual_debit, new_balance = await debit_wallet_clamped(
            req_db, uid, Decimal("30.00")
        )
        await req_db.commit()

    assert actual_debit == Decimal("30.00")
    assert new_balance == Decimal("120.00")
    assert await _fresh_balance(factory, uid) == Decimal("120.00")


@pytest.mark.asyncio
async def test_debit_clamps_to_available_balance(factory):
    from backend.services.wallet import debit_wallet_clamped

    uid = await _seed_user(factory, "20.00")

    async with factory() as db:
        actual_debit, new_balance = await debit_wallet_clamped(
            db, uid, Decimal("50.00")
        )
        await db.commit()

    assert actual_debit == Decimal("20.00")
    assert new_balance == Decimal("0.00")
    assert await _fresh_balance(factory, uid) == Decimal("0.00")


@pytest.mark.asyncio
async def test_concurrent_credit_and_debit_serialize(factory):
    """A credit arriving while a debit holds the row lock must wait for it,
    not be lost: 100 - 30 + 50 = 120 regardless of interleaving."""
    from backend.services.wallet import credit_wallet, debit_wallet_clamped

    uid = await _seed_user(factory, "100.00")

    async def debit():
        async with factory() as db:
            result = await debit_wallet_clamped(db, uid, Decimal("30.00"))
            await asyncio.sleep(0.3)  # hold the row lock across the credit
            await db.commit()
            return result

    async def credit():
        await asyncio.sleep(0.1)  # start while the debit holds the lock
        async with factory() as db:
            result = await credit_wallet(db, uid, Decimal("50.00"))
            await db.commit()
            return result

    (actual_debit, _), _ = await asyncio.gather(debit(), credit())

    assert actual_debit == Decimal("30.00")
    assert await _fresh_balance(factory, uid) == Decimal("120.00")
