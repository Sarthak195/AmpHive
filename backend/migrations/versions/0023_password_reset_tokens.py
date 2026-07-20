"""password_reset_tokens: single-use, time-boxed "forgot password" tokens.

Backs the self-service password-reset flow (routers/auth.py
POST /api/auth/forgot-password + /api/auth/reset-password). Only the SHA-256
hex digest of the emailed token is stored (token_hash, unique); used_at NULL
means still consumable, expires_at time-boxes the link, and consumption also
bumps users.token_version (existing 0003 column) to revoke all JWTs.

Idempotent (same rationale as 0011/0016/0022): a create_all-built database
already has this table from models.py — guard every DDL with IF NOT EXISTS.
Index names match the SQLAlchemy defaults (ix_<table>_<column> for index=True
columns; unique=True String columns get a UNIQUE constraint) because
test_migrations.py diffs the migrated schema against the models.

Revision ID: 0023_password_reset_tokens
Revises: 0022_queued_charge
Create Date: 2026-07-20
"""
from alembic import op

revision = "0023_password_reset_tokens"
down_revision = "0022_queued_charge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at    TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user_id "
        "ON password_reset_tokens (user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS password_reset_tokens")
