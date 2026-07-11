"""Wallet integrity: non-negative coin_balance CHECK on users.

Application code already row-locks wallets and debits only
min(final_cost, balance) (see services/session_lifecycle.py), so no live
write path can drive a balance negative — but nothing at the DB level
enforced that. This adds the backstop constraint, first clamping any
legacy negative rows (written before the 2026-07-06 ledger fix, IMPL
§3.30) to 0 so the constraint can attach on existing databases.

The constraint is also declared on the model (models.py User.__table_args__)
so fresh schemas match; autogenerate/compare tooling ignores CHECK
constraints, so drift-detection in test_migrations.py is unaffected.

Revision ID: 0002_wallet_non_negative
Revises: 0001_baseline
Create Date: 2026-07-07
"""
from alembic import op

revision = "0002_wallet_non_negative"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_users_coin_balance_non_negative"


def upgrade() -> None:
    # Legacy rows may hold negative balances from the pre-fix debit path
    # (which could record a debit larger than the wallet held). Forgive them —
    # the same policy finalize_charging_session applies at stop time.
    op.execute("UPDATE users SET coin_balance = 0 WHERE coin_balance < 0")
    # Idempotent add: a database bootstrapped via create_all (init_db stamps
    # such a pre-Alembic DB at the baseline, then upgrades to head) already
    # carries this constraint from the model's __table_args__, so a plain
    # ADD CONSTRAINT would raise DuplicateObject. Guard on pg_constraint so
    # the migration is a no-op when it's already present and adds it otherwise
    # (the genuine pre-constraint prod case).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{CONSTRAINT}'
                  AND conrelid = 'users'::regclass
            ) THEN
                ALTER TABLE users
                    ADD CONSTRAINT {CONSTRAINT} CHECK (coin_balance >= 0);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE users DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
