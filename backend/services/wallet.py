"""
Wallet write primitives — the only code that mutates ``User.coin_balance``
(initial values at registration/seed aside) — plus :func:`available_balance`,
a read-only helper for the session-sized authorization hold (see its own
docstring; it deliberately does NOT mutate ``coin_balance``).

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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import ChargingSession, SessionStatus, User
from backend.services.money import ZERO_MONEY, to_money


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


async def available_balance(db: AsyncSession, user_id: int) -> Decimal:
    """Coins the user could still put behind a NEW authorization hold right
    now: ``coin_balance`` minus the sum of ``hold_coins`` already reserved by
    this user's ACTIVE sessions (``ChargingSession.hold_coins`` — see
    ``routers/sessions.py`` ``start_charging_session`` and
    ``database/models.py`` ``ChargingSession.hold_coins``). Legacy ACTIVE
    sessions with a NULL hold contribute 0 (no reservation held).

    This is a **read-only derived view** — it never touches ``coin_balance``
    itself, and does not lock any row. A hold is a logical reservation, not a
    real debit: the DB non-negative CHECK
    (``ck_users_coin_balance_non_negative``) and every wallet-write path in
    this module (:func:`credit_wallet`, :func:`debit_wallet_clamped`) keep
    reading/writing ``coin_balance`` exactly as before this function existed.
    Money only actually moves at
    ``services/session_lifecycle.py`` ``finalize_charging_session`` (a real
    debit, capped by the hold) — never at hold creation or release.

    Single aggregate query, no row lock: a correlated scalar subquery sums
    ``hold_coins`` alongside the plain ``coin_balance`` column, so this is
    one round trip. Callers that need this figure to be race-safe against a
    concurrent hold (the session-start path) already hold the user row lock
    (``SELECT ... FOR UPDATE``) before calling this — which also serializes
    concurrent starts for the same user, so two sessions can never reserve
    the same coins. Callers that just want to display the number
    (``/api/auth/me``, the wallet ledger endpoint) read it unlocked, same as
    any other balance display.
    """
    held_subq = (
        select(func.coalesce(func.sum(ChargingSession.hold_coins), ZERO_MONEY))
        .where(
            ChargingSession.user_id == user_id,
            ChargingSession.status == SessionStatus.ACTIVE,
        )
        .scalar_subquery()
    )
    result = await db.execute(
        select(User.coin_balance, held_subq).where(User.id == user_id)
    )
    row = result.one_or_none()
    if row is None:
        return ZERO_MONEY
    balance, held = row
    return to_money(balance - held)
