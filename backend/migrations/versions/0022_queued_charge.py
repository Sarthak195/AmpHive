"""queued_charges: queue-a-charge-during-outage + its CPO config columns.

Phase 2/3 of docs/proposals/queued-charge-offline-plug.md, built on the
per-plug power heartbeat shipped in 0021. Adds:

- queued_charges table + queued_charge_status enum: a driver's request to
  auto-start a charge on a plug whose gateway is online but line power is out
  (services/session_reaper.py reap_queued_starts_once energizes it, via
  services/session_start.py begin_active_session, once the plug is powered
  continuously for the debounce). A separate table on purpose — QUEUED is
  deliberately NOT a SessionStatus value (mirrors Reservation/PlugWatch). The
  partial unique index (plug_id, user_id) WHERE status = 'waiting' caps a
  driver to one live queue per plug.
- CPO config (Tenant default + Plug override, mirroring the max_current_a
  precedent): tenants.queued_charging_enabled / auto_start_delay_min /
  queue_ttl_min (NOT NULL with server_defaults so every existing row
  backfills), and the two NULLABLE plug overrides
  plugs.queued_charging_enabled / auto_start_delay_min (NULL = inherit tenant).

Idempotent (same rationale as 0011/0016/0020): a create_all-built database
(init_db() stamps a pre-Alembic DB at the baseline, then upgrades to head)
already has these from the current models.py — guard every DDL with IF NOT
EXISTS / information_schema. test_migrations.py diffs the migrated schema
against models.py (indexes included), so index names match the SQLAlchemy
defaults (ix_<table>_<column> for index=True columns; the partial unique is
named in __table_args__).

Revision ID: 0022_queued_charge
Revises: 0021_plug_power
Create Date: 2026-07-14
"""
from alembic import op

revision = "0022_queued_charge"
down_revision = "0021_plug_power"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- CPO config: Tenant defaults (NOT NULL + server_default backfill) ---
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'queued_charging_enabled'
            ) THEN
                ALTER TABLE tenants ADD COLUMN queued_charging_enabled BOOLEAN NOT NULL DEFAULT false;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'auto_start_delay_min'
            ) THEN
                ALTER TABLE tenants ADD COLUMN auto_start_delay_min INTEGER NOT NULL DEFAULT 2;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'queue_ttl_min'
            ) THEN
                ALTER TABLE tenants ADD COLUMN queue_ttl_min INTEGER NOT NULL DEFAULT 720;
            END IF;
        END $$;
        """
    )

    # --- CPO config: Plug overrides (NULLABLE = inherit tenant) ---
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'queued_charging_enabled'
            ) THEN
                ALTER TABLE plugs ADD COLUMN queued_charging_enabled BOOLEAN;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'auto_start_delay_min'
            ) THEN
                ALTER TABLE plugs ADD COLUMN auto_start_delay_min INTEGER;
            END IF;
        END $$;
        """
    )

    # --- queued_charges table + enum ---
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'queued_charge_status') THEN
                CREATE TYPE queued_charge_status AS ENUM ('waiting', 'started', 'cancelled', 'expired', 'failed');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS queued_charges (
            id                   SERIAL PRIMARY KEY,
            tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plug_id              INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            max_kwh              DOUBLE PRECISION,
            max_duration_seconds INTEGER,
            status               queued_charge_status NOT NULL,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at           TIMESTAMPTZ NOT NULL,
            started_session_id   INTEGER REFERENCES charging_sessions(id) ON DELETE SET NULL
        )
        """
    )
    # Names must match models.py (index=True columns get the SQLAlchemy default
    # ix_<table>_<column>; the partial unique is named in __table_args__) —
    # test_migrations.py diffs the migrated schema against the models.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_queued_charges_tenant_id "
        "ON queued_charges (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_queued_charges_user_id "
        "ON queued_charges (user_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_queued_charges_one_waiting_per_user_plug "
        "ON queued_charges (plug_id, user_id) WHERE status = 'waiting'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS queued_charges")
    op.execute("DROP TYPE IF EXISTS queued_charge_status")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS auto_start_delay_min")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS queued_charging_enabled")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS queue_ttl_min")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS auto_start_delay_min")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS queued_charging_enabled")
