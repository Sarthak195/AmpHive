"""energy_counter_reset: REC-01 follow-up — persist reset-detection state.

Backs the fix for the REC-01 known gap (services/mqtt/telemetry.py
_persist_telemetry): the plain `max(energy_kwh, kwh)` clamp used to freeze
billing at the pre-reset peak whenever the P110/ESP32's session-relative wire
counter reset mid-session (device reboot/reflash, or the ESP32 losing its NVS
baseline) — every kWh delivered until the raw counter climbed back past the
old peak went unbilled from the bill.

Two ADD COLUMNs on charging_sessions:

- `energy_counter_last_raw_kwh` (nullable) — last UNADJUSTED wire kwh seen on
  a LIVE frame, read only to detect the next regression.
- `energy_reset_offset_kwh` (NOT NULL DEFAULT 0) — cumulative energy banked
  from segments closed off by a detected reset; billed energy becomes
  max(energy_kwh, energy_reset_offset_kwh + raw_kwh).

Idempotent add (same rationale as 0021/0025): a create_all()-built database
already has both columns from models.py, so guard each ADD COLUMN.

Revision ID: 0027_energy_counter_reset
Revises: 0026_offline_topups
Create Date: 2026-08-01
"""
from alembic import op

revision = "0027_energy_counter_reset"
down_revision = "0026_offline_topups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'energy_counter_last_raw_kwh'
            ) THEN
                ALTER TABLE charging_sessions
                    ADD COLUMN energy_counter_last_raw_kwh DOUBLE PRECISION;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'energy_reset_offset_kwh'
            ) THEN
                ALTER TABLE charging_sessions
                    ADD COLUMN energy_reset_offset_kwh DOUBLE PRECISION NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS energy_reset_offset_kwh")
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS energy_counter_last_raw_kwh")
