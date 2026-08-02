"""offline_consumption: unmetered/offline plug-consumption reconciliation.

Backs the fix for the owner-reported incident: a P110 was manually toggled
on/off while its gateway was fully offline, so the firmware's own
session-relative energy integrator never saw the energy and it went unbilled.
The plug's own today_energy/month_energy counters (get_energy_usage) are
maintained ON THE PLUG and keep advancing regardless of gateway connectivity,
so the backend now cross-checks them (services/mqtt/telemetry.py
_persist_telemetry) against billed sessions and raises a GatewayEvent
(UNMETERED_CONSUMPTION) when they advance with no ACTIVE session covering the
gap. See also firmware/main/tapo_protocol.c's
tapo_plug_reconcile_idle_baseline() -- the firmware's own one-shot offline
report over the same alarms topic (docs/MQTT_CONTRACT.md).

One ADD COLUMN pair on plugs:

- `last_today_energy_kwh` (nullable) -- the plug's today_energy the last time
  we saw it, read only to detect the next jump/reset.
- `last_month_energy_kwh` (nullable) -- same, for month_energy (the more
  reliable cross-check across a multi-hour/day outage, since it resets far
  less often than today_energy).

Both NULL until the first telemetry frame carrying these fields arrives, so
older firmware / a plug model that doesn't report them simply never triggers
this reconciliation for that plug -- no behavior change for anyone not on the
new firmware.

Idempotent add (same rationale as 0021/0025/0027/0034): a create_all()-built
database already has both columns from models.py, so guard each ADD COLUMN.

Revision ID: 0035_offline_consumption
Revises: 0034_gateway_claim_code
Create Date: 2026-08-02
"""
from alembic import op

revision = "0035_offline_consumption"
down_revision = "0034_gateway_claim_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'last_today_energy_kwh'
            ) THEN
                ALTER TABLE plugs ADD COLUMN last_today_energy_kwh DOUBLE PRECISION;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'last_month_energy_kwh'
            ) THEN
                ALTER TABLE plugs ADD COLUMN last_month_energy_kwh DOUBLE PRECISION;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS last_month_energy_kwh")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS last_today_energy_kwh")
