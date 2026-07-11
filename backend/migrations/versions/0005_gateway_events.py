"""gateway_events: operational events / alarms feed for the CPO portal.

Firmware raises safety alarms (THERMAL_CUTOFF, OVERCURRENT_CUTOFF,
UNAUTHORIZED_ON) and OTA lifecycle notices on the .../alarms topic; the backend
now ingests them (services/mqtt_manager._handle_gateway_alarm) and persists a
GatewayEvent so a CPO can see, e.g., a plug switched on out-of-band with no
authorized session. Surfaced via GET /api/cpo/events.

Idempotent create (same rationale as 0002/0003/0004): a create_all-built
database may already have the table from the model, so guard with IF NOT EXISTS.

Revision ID: 0005_gateway_events
Revises: 0004_plug_unique_id
Create Date: 2026-07-10
"""
from alembic import op

revision = "0005_gateway_events"
down_revision = "0004_plug_unique_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_events (
            id            BIGSERIAL PRIMARY KEY,
            tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            gateway_id    VARCHAR(50) NOT NULL REFERENCES gateways(id) ON DELETE CASCADE,
            plug_id       INTEGER REFERENCES plugs(id) ON DELETE SET NULL,
            event_type    VARCHAR(48) NOT NULL,
            severity      VARCHAR(16) NOT NULL DEFAULT 'warning',
            detail        VARCHAR(255),
            acknowledged  BOOLEAN NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_gateway_events_tenant_created "
        "ON gateway_events (tenant_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_gateway_events_gateway_created "
        "ON gateway_events (gateway_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gateway_events")
