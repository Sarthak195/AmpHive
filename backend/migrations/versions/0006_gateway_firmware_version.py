"""gateways.firmware_version: track the fw each gateway last reported.

The gateway's `online` MQTT status payload carries `{"fw": "1.5.0-direct"}`;
the backend now persists it so a CPO can see which gateways are behind and
need an OTA (complements the OTA trigger endpoint). NULL until first connect.

Idempotent add (same rationale as 0002/0003/0004): a create_all-built database
already has the column from the model, so guard on information_schema.

Revision ID: 0006_gateway_firmware_version
Revises: 0005_gateway_events
Create Date: 2026-07-10
"""
from alembic import op

revision = "0006_gateway_firmware_version"
down_revision = "0005_gateway_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'gateways' AND column_name = 'firmware_version'
            ) THEN
                ALTER TABLE gateways ADD COLUMN firmware_version VARCHAR(32);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE gateways DROP COLUMN IF EXISTS firmware_version")
