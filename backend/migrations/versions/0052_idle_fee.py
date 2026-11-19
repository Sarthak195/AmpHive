"""idle_fee: opt-in idle fee after the car looks fully charged (wave-4 C1).

tariffs (the level that carries rates), all NOT NULL DEFAULT 0 so every
existing tariff reads "no fee" and nothing changes until a CPO sets them:
  idle_grace_min    minutes after the "looks full" signal before the fee
  idle_fee_per_min  coins per whole idle minute past the grace
  idle_fee_cap      most coins one session can be charged

charging_sessions:
  idle_grace_min / idle_fee_per_min / idle_fee_cap  the terms snapshotted at
                    start (NULL = no fee), so a tariff edit never reprices a
                    running session
  idle_started_at   an open stretch of low draw after the "looks full" notice
  idle_seconds      closed parts of that stretch (NOT NULL DEFAULT 0; -1 =
                    voided, the car drew again after the signal)
  idle_fee_coins    what finalize collected (also part of coins_spent)
All written on transitions only, never per frame.

tx_type gains 'idle_fee' (the settled ledger line), the 0026/0038 idiom:
ALTER TYPE ... ADD VALUE IF NOT EXISTS. Nothing in this migration uses the new
value, so no separate revision is needed. IF NOT EXISTS everywhere so a
create_all()-built database migrates cleanly. No index (the columns are read
off rows already loaded by primary key) and no GRANT (columns on existing
tables).

Revision ID: 0052_idle_fee
Revises: 0051_durable_notice_state
Create Date: 2026-11-19
"""
from alembic import op

revision = "0052_idle_fee"
down_revision = "0051_durable_notice_state"
branch_labels = None
depends_on = None

_TARIFF_COLS = (
    ("idle_grace_min", "INTEGER NOT NULL DEFAULT 0"),
    ("idle_fee_per_min", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
    ("idle_fee_cap", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
)
_SESSION_COLS = (
    ("idle_grace_min", "INTEGER"),
    ("idle_fee_per_min", "NUMERIC(12, 2)"),
    ("idle_fee_cap", "NUMERIC(12, 2)"),
    ("idle_started_at", "TIMESTAMP WITH TIME ZONE"),
    ("idle_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("idle_fee_coins", "NUMERIC(12, 2)"),
)


def upgrade() -> None:
    op.execute("ALTER TYPE tx_type ADD VALUE IF NOT EXISTS 'idle_fee'")
    for col, ddl in _TARIFF_COLS:
        op.execute(f"ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS {col} {ddl}")
    for col, ddl in _SESSION_COLS:
        op.execute(f"ALTER TABLE charging_sessions ADD COLUMN IF NOT EXISTS {col} {ddl}")


def downgrade() -> None:
    # Postgres cannot drop an enum value; leaving 'idle_fee' is harmless and a
    # re-upgrade is a no-op thanks to IF NOT EXISTS (the 0038 note).
    for col, _ddl in reversed(_SESSION_COLS):
        op.execute(f"ALTER TABLE charging_sessions DROP COLUMN IF EXISTS {col}")
    for col, _ddl in reversed(_TARIFF_COLS):
        op.execute(f"ALTER TABLE tariffs DROP COLUMN IF EXISTS {col}")
