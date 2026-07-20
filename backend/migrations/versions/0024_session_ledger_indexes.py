"""session_ledger_indexes: composite btree indexes for hot session/ledger paths.

Adds the composite indexes backing the query patterns that were previously
falling back to sequential scans / single-column indexes:
  - charging_sessions(plug_id, status): "is this plug's session still active"
  - charging_sessions(user_id, status): a driver's active/past sessions
  - charging_sessions(tenant_id, started_at): CPO session history, newest-first
  - ledger_transactions(user_id, created_at): a driver's wallet/ledger feed

The indexes are also declared on the ORM models (__table_args__). Because
init_db() stamps a legacy create_all database at baseline and then upgrades to
head, this revision can run against a schema that already carries these indexes
(create_all built them from the model metadata). CREATE INDEX IF NOT EXISTS
keeps that path a no-op while still building the indexes on a from-empty
`alembic upgrade head`.

Revision ID: 0024_session_ledger_indexes
Revises: 0023_password_reset_tokens
Create Date: 2026-07-20
"""
from alembic import op

revision = "0024_session_ledger_indexes"
down_revision = "0023_password_reset_tokens"
branch_labels = None
depends_on = None

_INDEXES = [
    ("ix_charging_sessions_plug_status", "charging_sessions", "plug_id, status"),
    ("ix_charging_sessions_user_status", "charging_sessions", "user_id, status"),
    ("ix_charging_sessions_tenant_started", "charging_sessions", "tenant_id, started_at"),
    ("ix_ledger_user_created", "ledger_transactions", "user_id, created_at"),
]


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")


def downgrade() -> None:
    for name, _table, _cols in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
