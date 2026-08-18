"""account_closure: users.deleted_at + the account_closure ledger tx type.

Backs self-service account closure (DELETE /api/auth/me, see
backend/services/account_closure.py). Two small, additive changes:

1. `users.deleted_at TIMESTAMPTZ NULL` — when the holder closed the account.
   NULL for a live account. Closure ANONYMISES the row rather than deleting it:
   every `user_id` FK is ON DELETE CASCADE, so a real DELETE would take the
   account's charging_sessions, ledger_transactions and GST `invoices` with it
   — records the operator is required to keep and which feed the CPO earnings /
   payout watermark maths. The row is kept as a scrubbed tombstone instead so
   those financial rows still resolve while identifying nobody.

2. `tx_type` gains `'account_closure'` — the ledger line written when a closing
   account's remaining charging credit is forfeited. Credit is a closed-loop,
   non-withdrawable instrument (the /terms page says so), so closure zeroes the
   balance; writing it as an explicit ledger row rather than a silent UPDATE
   keeps the running balance reconcilable by summing `amount`, exactly like
   every other wallet movement.

`ALTER TYPE ... ADD VALUE` is the same idiom 0026_offline_topups introduced.
IF NOT EXISTS on both so a create_all()-built database (which already has the
column and the enum member from models.py) migrates cleanly — the idempotency
rationale of 0023/0025/0026/0033/0037.

The least-privilege runtime role already covers public-schema tables owned by
the owner role (docs/DATA_MODEL.md §4), and this adds no new object class, so
no extra GRANT is needed.

Revision ID: 0038_account_closure
Revises: 0037_email_verification
Create Date: 2026-08-18
"""
from alembic import op

revision = "0038_account_closure"
down_revision = "0037_email_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE tx_type ADD VALUE IF NOT EXISTS 'account_closure'")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type; leaving 'account_closure'
    # in place is harmless (no row references it after the column below goes,
    # and re-upgrading is a no-op thanks to IF NOT EXISTS).
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS deleted_at")
