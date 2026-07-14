"""plug power: per-plug telemetry-liveness columns.

Foundation for the per-plug power gate (session-start refuses a plug that has
lost power) and the queued-charge proposal. Adds two NULLABLE timestamp
columns:

- `plugs.last_telemetry_at` — stamped on every inbound telemetry frame by
  MQTTManager._persist_telemetry; the freshness signal plug_is_powered() reads
  (services/session_lifecycle.py). Distinct from the never-written last_seen_at.
- `plugs.powered_since`      — re-baselined to "now" whenever telemetry resumes
  after a PLUG_POWER_STALE_SEC gap (a mains/relay power-cycle).

Both NULL until the first frame arrives, so this is safe to deploy before any
plug reports: plug_is_powered() reads NULL as "not powered", and the start gate
only tightens (a strict refinement, like the gateway-liveness gate).

Idempotent (same rationale as 0019): a create_all-built DB may already have
these columns from models.py, so guard with information_schema checks.

Revision ID: 0021_plug_power
Revises: 0020_capacity_requests
Create Date: 2026-07-14
"""
from alembic import op

revision = "0021_plug_power"
down_revision = "0020_capacity_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'last_telemetry_at'
            ) THEN
                ALTER TABLE plugs ADD COLUMN last_telemetry_at TIMESTAMPTZ;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'powered_since'
            ) THEN
                ALTER TABLE plugs ADD COLUMN powered_since TIMESTAMPTZ;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS powered_since")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS last_telemetry_at")
