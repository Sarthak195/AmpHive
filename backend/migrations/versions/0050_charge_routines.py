"""charge_routines: recurring weekly charge routines (feature C5).

One row per routine a driver saves (models.ChargeRoutine): the weekdays
bitmask (Monday=1 .. Sunday=64), a local start time in the plug's tenant
timezone, a mandatory energy and/or duration limit, the "stop when full"
opt-in, and the bookkeeping the session reaper needs to materialize each
occurrence exactly once (last_materialized_date, last_queued_charge_id,
consecutive_failures, gate_notified). Each occurrence becomes an ordinary
scheduled queued_charges row, so no other table changes.

No enum change, so this migration is fully reversible. The least-privilege
runtime role already covers public-schema tables and sequences created by the
owner (ALTER DEFAULT PRIVILEGES, docs/DATA_MODEL.md), so no GRANT is needed.

Idempotent (same rationale as 0047): a create_all-built database already has
the table from models.py, so the DDL is guarded with IF NOT EXISTS. Index and
constraint names match the model because test_migrations.py diffs the
migrated schema against it.

Revision ID: 0050_charge_routines
Revises: 0047_promo_codes
Create Date: 2026-11-19
"""
from alembic import op

revision = "0050_charge_routines"
down_revision = "0047_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS charge_routines (
            id                      SERIAL PRIMARY KEY,
            user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plug_id                 INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            tenant_id               INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            weekdays                SMALLINT NOT NULL,
            start_time              TIME WITHOUT TIME ZONE NOT NULL,
            max_kwh                 DOUBLE PRECISION,
            max_duration_seconds    INTEGER,
            auto_stop_when_full     BOOLEAN NOT NULL DEFAULT false,
            enabled                 BOOLEAN NOT NULL DEFAULT true,
            last_materialized_date  DATE,
            consecutive_failures    INTEGER NOT NULL DEFAULT 0,
            gate_notified           BOOLEAN NOT NULL DEFAULT false,
            last_queued_charge_id   INTEGER REFERENCES queued_charges(id) ON DELETE SET NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_charge_routines_has_limit
                CHECK (max_kwh IS NOT NULL OR max_duration_seconds IS NOT NULL),
            CONSTRAINT ck_charge_routines_weekdays CHECK (weekdays BETWEEN 1 AND 127)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_charge_routines_user_id "
        "ON charge_routines (user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS charge_routines")
