"""
Wallet write primitives — the only code that mutates ``User.coin_balance``
(initial values at registration/seed aside).

Why these are not plain ORM read-modify-write: the request-scoped session
loads the authenticated User during auth (``get_current_user``), so that
instance is already in the session's identity map when a wallet write runs.
A later ``select(User).with_for_update()`` on the same session takes the row
lock but returns the CACHED instance without refreshing its loaded
attributes (SQLAlchemy identity-map semantics, and the factory sets
``expire_on_commit=False``). Arithmetic on ``instance.coin_balance`` then
uses the balance as of auth time — a credit/debit committed by another
transaction in between is silently overwritten (lost update).

- :func:`credit_wallet` pushes the arithmetic into the database
  (``UPDATE … SET coin_balance = coin_balance + :x RETURNING``), so the
  value added to is the one the UPDATE itself locks.
- :func:`debit_wallet_clamped` needs the previous balance in Python (the
  debit is clamped to what the wallet holds and the ledger must record the
  true delta), so it locks and reads the balance as a *column* — a scalar
  select bypasses the identity map and always yields the committed DB
  value — then writes the derived balance while the row lock is held.

Both run inside the caller's transaction: nothing here commits, and a
caller rollback undoes the balance change.
"""
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import User
from backend.services.money import ZERO_MONEY


async def credit_wallet(
    db: AsyncSession, user_id: int, amount: Decimal
) -> Optional[Decimal]:
    """Atomically add ``amount`` coins; returns the new balance, or None if
    no such user exists."""
    result = await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(coin_balance=User.coin_balance + amount)
        .returning(User.coin_balance)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none()


async def debit_wallet_clamped(
    db: AsyncSession, user_id: int, amount: Decimal
) -> Tuple[Decimal, Decimal]:
    """Debit up to ``amount`` coins, clamped so the balance never goes
    negative. Returns ``(actual_debit, new_balance)``; the row stays locked
    until the caller commits or rolls back."""
    prev_balance = (
        await db.execute(
            select(User.coin_balance).where(User.id == user_id).with_for_update()
        )
    ).scalar_one()
    if prev_balance < ZERO_MONEY:  # legacy rows predating the CHECK constraint
        prev_balance = ZERO_MONEY

    actual_debit = min(amount, prev_balance)
    new_balance = prev_balance - actual_debit
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(coin_balance=new_balance)
        .execution_options(synchronize_session=False)
    )
    return actual_debit, new_balance
