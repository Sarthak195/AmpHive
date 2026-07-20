"""session_ledger_indexes: composite btree indexes for hot session/ledger paths.

Adds the composite indexes backing the query patterns that were previously
falling back to sequential scans / single-column indexes:
  - charging_sessions(plug_id, status): "is this plug's session still active"
  - charging_sessions(user_id, status): a driver's active/past sessions
  - charging_sessions(tenant_id, started_at): CPO session history, newest-first
  - ledger_transactions(user_id, created_at): a driver's wallet/ledger feed

Revision ID: 0024_session_ledger_indexes
Revises: 0023_password_reset_tokens
Create Date: 2026-07-20
"""
from alembic import op

revision = "0024_session_ledger_indexes"
down_revision = "0023_password_reset_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_charging_sessions_plug_status", "charging_sessions", ["plug_id", "status"])
    op.create_index("ix_charging_sessions_user_status", "charging_sessions", ["user_id", "status"])
    op.create_index("ix_charging_sessions_tenant_started", "charging_sessions", ["tenant_id", "started_at"])
    op.create_index("ix_ledger_user_created", "ledger_transactions", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ledger_user_created", table_name="ledger_transactions")
    op.drop_index("ix_charging_sessions_tenant_started", table_name="charging_sessions")
    op.drop_index("ix_charging_sessions_user_status", table_name="charging_sessions")
    op.drop_index("ix_charging_sessions_plug_status", table_name="charging_sessions")
