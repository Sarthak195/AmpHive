"""auth_holds: session-sized authorization hold on ChargingSession.

Adds a nullable `hold_coins` column to `charging_sessions`: the amount of the
driver's wallet RESERVED (logically, not debited) when the session started
(`routers/sessions.py` `start_charging_session`), sized to
`min(available_balance, max_kwh * rate)` — see `services/wallet.py`
`available_balance()`. NULL means a pre-hold legacy session (started before
this column existed); those keep the old forgiven-overage finalize behavior
(`services/session_lifecycle.py` `finalize_charging_session`).

This does NOT touch `users.coin_balance` or its non-negative CHECK — a hold
is a logical reservation computed at read time from the SUM of a user's
ACTIVE sessions' `hold_coins`, not a real debit; no existing wallet-write
path (`services/wallet.py` `credit_wallet` / `debit_wallet_clamped`) changes
shape or behavior.

NOTE on revision chaining: MARKET_GAP_ANALYSIS.md §3 / this feature's task
brief was authored when `0010_tariffs` was the head and a sibling migration
("0011") was still in flight on a parallel branch. By the time this branch
was cut from `origin/main`, that sibling had already merged as
`0011_disputes` (chained onto `0010_tariffs`) — i.e. the orchestrator's
re-chain already happened once. Chaining this revision onto `0011_disputes`
(the actual current head) rather than the now-stale `0010_tariffs` keeps
`alembic upgrade head` single-headed and green standalone, matching the
precedent `0009_payouts`/`0011_disputes` themselves set for exactly this
situation. This revision only touches `charging_sessions.hold_coins`, so it
stays self-contained and reorderable if the orchestrator re-chains again.

Idempotent add (same rationale as 0002 onward): a create_all-built database
may already have this column from the model, so guard with an
information_schema check.

Revision ID: 0012_auth_holds
Revises: 0011_disputes
Create Date: 2026-07-12
"""
from alembic import op

revision = "0012_auth_holds"
down_revision = "0011_disputes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # charging_sessions.hold_coins -- the authorization hold reserved at
    # session start. No FK (a point-in-time money amount, not a reference),
    # nullable (NULL = legacy session predating this column), no default (an
    # explicit NULL is the correct "no hold" value -- 0 would read as "a hold
    # of zero coins", which is a different, meaningful state).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'hold_coins'
            ) THEN
                ALTER TABLE charging_sessions ADD COLUMN hold_coins NUMERIC(12,2);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS hold_coins")
