"""offline_topups: CPO cash top-up ledger + tx_type enum extension.

Backs the "CPO offline top-up" feature (docs/TODO.md "Wallet & payments
features" backlog): a CPO manually credits a driver's coin wallet for cash
collected offline, funded from the CPO's OWN unsettled net earnings pool (see
services/payouts.py tenant_earnings_summary's available_pool_coins) — never
coins created from nothing.

Two changes:

1. `ALTER TYPE tx_type ADD VALUE IF NOT EXISTS 'cpo_topup'` — the first
   migration in this repo to extend a native Postgres enum rather than
   create one from scratch (see database/models.py TransactionType.CPO_TOPUP).
   `ADD VALUE ... IF NOT EXISTS` is natively idempotent (PG9.6+), no DO-block
   guard needed like the CREATE TYPE precedent (0009_payouts). Safe to run
   inside Alembic's per-migration transaction on PG12+: the restriction is
   only that the *new value* can't be read/written in the SAME transaction
   that added it, which this migration never does.

2. `offline_topups` table: one row per top-up. tenant_id is NOT NULL (every
   top-up is tenant-scoped, same as Payout); actor_user_id/driver_user_id are
   nullable + SET NULL (mirrors AuditLog: the accounting record must survive
   either user's account being deleted later, since the earnings/payout
   watermark math keeps reading this table for the life of the tenant).

Idempotent create (same rationale as 0009/0025): a create_all()-built
database already has this table from models.py.

Revision ID: 0026_offline_topups
Revises: 0025_user_disable
Create Date: 2026-07-21
"""
from alembic import op

revision = "0026_offline_topups"
down_revision = "0025_user_disable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE tx_type ADD VALUE IF NOT EXISTS 'cpo_topup'")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_topups (
            id                SERIAL PRIMARY KEY,
            tenant_id         INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            actor_user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            driver_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            amount_coins      NUMERIC(12, 2) NOT NULL,
            note              VARCHAR(500),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_topups_tenant_created "
        "ON offline_topups (tenant_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS offline_topups")
    # Postgres cannot drop a single enum value (would require rebuilding the
    # type and every column/index that uses it) — 'cpo_topup' stays in
    # tx_type on downgrade. Harmless: no row can reference it once this
    # table (its only writer) is gone.
