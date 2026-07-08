"""Token revocation: users.token_version epoch column.

Embedded in every JWT as the `tv` claim and re-checked per request; bumping
it (logout / password change / admin revoke) invalidates all previously
issued tokens for that user without a blacklist table.

Idempotent add (same rationale as 0002): a create_all-built database already
has the column from the model, so guard on information_schema before adding.

Revision ID: 0003_token_version
Revises: 0002_wallet_non_negative
Create Date: 2026-07-08
"""
from alembic import op

revision = "0003_token_version"
down_revision = "0002_wallet_non_negative"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'token_version'
            ) THEN
                ALTER TABLE users
                    ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS token_version")
