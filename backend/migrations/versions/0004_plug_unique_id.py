"""plugs.unique_id: stable device identity for agent-discovered plugs.

The AmpHive Agent (docs/AMPHIVE_AGENT.md) discovers plugs of many brands and
reports a stable, brand-scoped ``unique_id`` (e.g. "kasa:AA:BB:.."). The backend
upserts a plug keyed by this id (letting the DB assign the authoritative
``plugs.id``), then hands that id back to the agent. NULL for ESP-gateway or
manually-provisioned plugs.

Idempotent add (same rationale as 0002/0003): a create_all-built database
already has the column from the model, so guard on information_schema.

Revision ID: 0004_plug_unique_id
Revises: 0003_token_version
Create Date: 2026-07-09
"""
from alembic import op

revision = "0004_plug_unique_id"
down_revision = "0003_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'unique_id'
            ) THEN
                ALTER TABLE plugs ADD COLUMN unique_id VARCHAR(128);
            END IF;
        END $$;
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_plugs_unique_id ON plugs (unique_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_plugs_unique_id")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS unique_id")
