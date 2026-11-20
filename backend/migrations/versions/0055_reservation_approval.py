"""reservation_approval: per-plug approval mode for reservations (C8).

1. `reservation_status` gains 'pending' and 'declined'. A booking on a plug in
   approval mode starts PENDING until the tenant's CPO approves (BOOKED) or
   declines (DECLINED) it.
2. `plugs.reservation_mode VARCHAR(16) NOT NULL DEFAULT 'instant'` ('instant'
   or 'approval'). VARCHAR rather than a native enum, like plug_model and
   connector_type; the API schema validates the value.
3. The optional no-show fee: `tx_type` gains 'reservation_no_show_fee';
   `plugs.no_show_fee_coins` (default 0 = no fee); on `reservations`, the fee
   snapshotted at booking (`no_show_fee_coins`) and the sweep's settle stamp
   (`no_show_checked_at`) with a partial index over the unsettled fee rows;
   `ledger_transactions.reservation_id`, UNIQUE, the fee's idempotency key.

`ALTER TYPE ... ADD VALUE` is the idiom of 0026_offline_topups and
0038_account_closure. env.py runs the whole upgrade in one transaction, and a
value added there cannot be used before that transaction commits, so nothing
in this file mentions the new labels in any other DDL (no default, no partial
index, no backfill). IF NOT EXISTS everywhere so a create_all-built database
migrates cleanly.

The least-privilege runtime role already covers public-schema tables owned by
the owner role (docs/DATA_MODEL.md), so no GRANT is needed.

Revision ID: 0055_reservation_approval
Revises: 0054_member_price_invites
Create Date: 2026-11-19
"""
from alembic import op

revision = "0055_reservation_approval"
down_revision = "0054_member_price_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'pending'")
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'declined'")
    op.execute(
        "ALTER TABLE plugs ADD COLUMN IF NOT EXISTS reservation_mode "
        "VARCHAR(16) NOT NULL DEFAULT 'instant'"
    )

    # The optional no-show fee.
    op.execute("ALTER TYPE tx_type ADD VALUE IF NOT EXISTS 'reservation_no_show_fee'")
    op.execute(
        "ALTER TABLE plugs ADD COLUMN IF NOT EXISTS no_show_fee_coins "
        "NUMERIC(12, 2) NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS no_show_fee_coins "
        "NUMERIC(12, 2) NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS no_show_checked_at TIMESTAMPTZ"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reservations_no_show_due ON reservations "
        "(start_at) WHERE no_show_checked_at IS NULL AND no_show_fee_coins > 0"
    )
    op.execute(
        "ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS reservation_id "
        "INTEGER UNIQUE REFERENCES reservations(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type. Leaving 'pending',
    # 'declined' and 'reservation_no_show_fee' in place is harmless once no
    # code writes them, and re-upgrading is a no-op thanks to IF NOT EXISTS.
    op.execute("ALTER TABLE ledger_transactions DROP COLUMN IF EXISTS reservation_id")
    op.execute("DROP INDEX IF EXISTS idx_reservations_no_show_due")
    op.execute("ALTER TABLE reservations DROP COLUMN IF EXISTS no_show_checked_at")
    op.execute("ALTER TABLE reservations DROP COLUMN IF EXISTS no_show_fee_coins")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS no_show_fee_coins")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS reservation_mode")
