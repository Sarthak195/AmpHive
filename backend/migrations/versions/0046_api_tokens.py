"""api_tokens: personal read-only API tokens (feature F7).

Backs the ApiToken model (database/models.py): a user mints a named token in
Account (POST /api/me/tokens), and Home Assistant or evcc presents it as
"Authorization: Bearer amph_<secret>" to GET /api/me/meter, the only endpoint
that accepts one. Only the SHA-256 hex digest of the token is stored
(token_hash, unique); token_prefix is a display stub; revoked_at NULL = live.

Idempotent create (same rationale as 0023_password_reset_tokens and
0042_revoked_sessions): a create_all-built database already has the table from
models.py, so every statement is IF NOT EXISTS. Index names match the
SQLAlchemy defaults so test_migrations.py sees no drift. The least-privilege
runtime role needs SELECT, INSERT and UPDATE here, all covered by the owner
role's ALTER DEFAULT PRIVILEGES (docs/DATA_MODEL.md), so no extra GRANT.

Downgrade drops the table: pre-feature code has no token path, so every
token simply stops working, which is the safe direction.

Revision ID: 0046_api_tokens
Revises: 0045_cpo_viewer_role
Create Date: 2026-11-18
"""
from alembic import op

revision = "0046_api_tokens"
down_revision = "0045_cpo_viewer_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id           SERIAL PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name         VARCHAR(64) NOT NULL,
            token_prefix VARCHAR(16) NOT NULL,
            token_hash   VARCHAR(64) NOT NULL UNIQUE,
            scope        VARCHAR(16) NOT NULL DEFAULT 'read',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMPTZ,
            revoked_at   TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_api_tokens_user_id "
        "ON api_tokens (user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS api_tokens")
