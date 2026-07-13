"""pricing_v2: time-of-day tariff slots + session segment-accrual state.

Phase 1 of Pricing v2 (docs/PRICING_V2_SPEC.md) — SCHEMA ONLY, plus the
resolution/billing helpers in services/pricing.py + services/billing.py. No
slots are created and no live billing path reads them yet, so current billing
behaviour is unchanged.

Adds:
- New `tariff_slots` table: a time-of-day price window owned by a tariff —
  half-open minute-of-day [start_min, end_min) on the weekdays in days_mask
  (Mon=bit0..Sun=bit6, 127 = all days), interpreted in the tenant's local
  wall-clock. tariff_id NOT NULL REFERENCES tariffs(id) ON DELETE CASCADE +
  its ix_tariff_slots_tariff_id index (matches models.py's index=True).
- Three NULLABLE columns on `charging_sessions` carrying the segment-accrual
  state (services/billing.py): settled_cost_coins (NUMERIC(12,2)),
  rate_segment_start_kwh (DOUBLE PRECISION), rate_valid_until (TIMESTAMPTZ).
  NULL = legacy single-rate session (the only kind Phase 1 produces).
- `tenants.timezone` (VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata'): the zone
  a CPO's minute-of-day slots are read in. The server_default backfills every
  existing tenant row to India's zone.

NOTE on revision chaining: chained onto 0017_reservation_started (the current
single head at authoring time — see that file's `revision`). This revision
only touches its own table + the columns it owns, so it stays self-contained
and reorderable if the merge orchestrator re-chains it (the established
parallel-agent protocol; the 0015/0016 precedent).

Idempotent create/add (same rationale as 0002 onward): a create_all-built
database (init_db() stamps a pre-Alembic DB at the baseline, then upgrades to
head) may already have this table/these columns from the current models.py, so
guard with IF NOT EXISTS / information_schema checks throughout.

Revision ID: 0018_pricing_v2_slots
Revises: 0017_reservation_started
Create Date: 2026-07-13
"""
from alembic import op

revision = "0018_pricing_v2_slots"
down_revision = "0017_reservation_started"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. The tariff_slots table. tariff_id NOT NULL — a slot is always owned by
    #    exactly one tariff; CASCADE matches the tenant-ownership convention.
    #    days_mask DEFAULT 127 so a slot inserted without one applies every day.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tariff_slots (
            id             SERIAL PRIMARY KEY,
            tariff_id      INTEGER NOT NULL REFERENCES tariffs(id) ON DELETE CASCADE,
            start_min      SMALLINT NOT NULL,
            end_min        SMALLINT NOT NULL,
            price_per_kwh  NUMERIC(12,2) NOT NULL,
            days_mask      SMALLINT NOT NULL DEFAULT 127
        )
        """
    )
    # Name must match what models.py's index=True generates
    # (ix_<table>_<column>) — test_migrations.py diffs the migrated schema
    # against the models, indexes included.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tariff_slots_tariff_id "
        "ON tariff_slots (tariff_id)"
    )

    # 2. charging_sessions segment-accrual columns — all nullable, no default
    #    (an explicit NULL is the correct "legacy single-rate session" value).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'settled_cost_coins'
            ) THEN
                ALTER TABLE charging_sessions ADD COLUMN settled_cost_coins NUMERIC(12,2);
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'rate_segment_start_kwh'
            ) THEN
                ALTER TABLE charging_sessions ADD COLUMN rate_segment_start_kwh DOUBLE PRECISION;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'rate_valid_until'
            ) THEN
                ALTER TABLE charging_sessions ADD COLUMN rate_valid_until TIMESTAMPTZ;
            END IF;
        END $$;
        """
    )

    # 3. tenants.timezone — NOT NULL with a server default so every existing
    #    row backfills to India's zone (ADD COLUMN ... NOT NULL DEFAULT applies
    #    the default to pre-existing rows in one statement).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'timezone'
            ) THEN
                ALTER TABLE tenants ADD COLUMN timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS timezone")
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS rate_valid_until")
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS rate_segment_start_kwh")
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS settled_cost_coins")
    op.execute("DROP INDEX IF EXISTS ix_tariff_slots_tariff_id")
    op.execute("DROP TABLE IF EXISTS tariff_slots")
