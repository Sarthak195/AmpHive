"""tariffs: per-CPO/per-site pricing foundation.

Replaces the single global COINS_PER_KWH env var with a real pricing model:
- New `tariffs` table: a named coins-per-kWh plan owned by a tenant.
- Nullable `tariff_id` FKs on `plugs` and `charger_groups` (a plug/group can
  be assigned a specific tariff).
- Nullable `default_tariff_id` on `tenants` (the tenant-wide fallback).
- Nullable `rate_coins_per_kwh` on `charging_sessions` — the rate SNAPSHOTTED
  at session start (services/pricing.py resolve_rate_for_plug), so editing a
  tariff's price later never retroactively changes an in-flight or
  already-billed session.

Resolution chain (services/pricing.py resolve_rate_for_plug), first match
wins: plug.tariff -> plug's group.tariff -> tenant.default_tariff -> the
global COINS_PER_KWH env var (legacy fallback, unchanged default 5.0).

Idempotent create/add (same rationale as 0002/0003/0004/0005/0006): a
create_all-built database may already have these from the model, so guard
with IF NOT EXISTS / information_schema checks throughout.

NOTE for the merge orchestrator: this revision is deliberately self-contained
(only tariffs.* and the four new columns it owns) so it stays green whether
its down_revision chain to 0006 is preserved standalone or later re-chained
after sibling migrations (0007/0009/0010) developed in parallel worktrees.

Revision ID: 0010_tariffs
Revises: 0009_payouts
Create Date: 2026-07-12
"""
from alembic import op

revision = "0010_tariffs"
down_revision = "0009_payouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. The tariffs table itself. tenant_id is NOT NULL — every tariff is
    #    owned by exactly one tenant (CPO); CASCADE matches the
    #    ChargerGroup/Gateway tenant-ownership convention elsewhere.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tariffs (
            id             SERIAL PRIMARY KEY,
            tenant_id      INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name           VARCHAR(100) NOT NULL,
            price_per_kwh  NUMERIC(12,2) NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tariffs_tenant_id ON tariffs (tenant_id)"
    )

    # 2. plugs.tariff_id — per-plug pricing override. ON DELETE SET NULL: a
    #    deleted Tariff must not take the plug row down with it, just drop it
    #    back a link in the resolution chain.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'tariff_id'
            ) THEN
                ALTER TABLE plugs ADD COLUMN tariff_id INTEGER
                    REFERENCES tariffs(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )

    # 3. charger_groups.tariff_id — group-level pricing override.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charger_groups' AND column_name = 'tariff_id'
            ) THEN
                ALTER TABLE charger_groups ADD COLUMN tariff_id INTEGER
                    REFERENCES tariffs(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )

    # 4. tenants.default_tariff_id — the tenant-wide fallback tariff.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'default_tariff_id'
            ) THEN
                ALTER TABLE tenants ADD COLUMN default_tariff_id INTEGER
                    REFERENCES tariffs(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )

    # 5. charging_sessions.rate_coins_per_kwh — the per-session SNAPSHOT.
    #    No FK (a point-in-time copy of the rate, not a live reference), and
    #    nullable: legacy pre-existing sessions have none and fall back to
    #    the env default at every billing call site.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'rate_coins_per_kwh'
            ) THEN
                ALTER TABLE charging_sessions ADD COLUMN rate_coins_per_kwh NUMERIC(12,2);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS rate_coins_per_kwh")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS default_tariff_id")
    op.execute("ALTER TABLE charger_groups DROP COLUMN IF EXISTS tariff_id")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS tariff_id")
    op.execute("DROP INDEX IF EXISTS ix_tariffs_tenant_id")
    op.execute("DROP TABLE IF EXISTS tariffs")
