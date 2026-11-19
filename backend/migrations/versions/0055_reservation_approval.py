"""reservation_approval: per-plug approval mode for reservations (C8).

1. `reservation_status` gains 'pending' and 'declined'. A booking on a plug in
   approval mode starts PENDING until the tenant's CPO approves (BOOKED) or
   declines (DECLINED) it.
2. `plugs.reservation_mode VARCHAR(16) NOT NULL DEFAULT 'instant'` ('instant'
   or 'approval'). VARCHAR rather than a native enum, like plug_model and
   connector_type; the API schema validates the value.

`ALTER TYPE ... ADD VALUE` is the idiom of 0026_offline_topups and
0038_account_closure. env.py runs the whole upgrade in one transaction, and a
value added there cannot be used before that transaction commits, so nothing
in this file mentions the new labels in any other DDL (no default, no partial
index, no backfill). IF NOT EXISTS everywhere so a create_all-built database
migrates cleanly.

The least-privilege runtime role already covers public-schema tables owned by
the owner role (docs/DATA_MODEL.md), so no GRANT is needed.

Revision ID: 0055_reservation_approval
Revises: 0050_charge_routines
Create Date: 2026-11-19
"""
from alembic import op

revision = "0055_reservation_approval"
down_revision = "0050_charge_routines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'pending'")
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'declined'")
    op.execute(
        "ALTER TABLE plugs ADD COLUMN IF NOT EXISTS reservation_mode "
        "VARCHAR(16) NOT NULL DEFAULT 'instant'"
    )


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type. Leaving 'pending' and
    # 'declined' in place is harmless once no code writes them, and
    # re-upgrading is a no-op thanks to IF NOT EXISTS.
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS reservation_mode")
