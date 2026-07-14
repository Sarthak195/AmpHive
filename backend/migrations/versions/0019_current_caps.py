"""caps: per-plug + per-circuit current caps for admission control.

First slice of the caps + circuits feature (design in the frontend-redesign /
first-client memory). Adds two NULLABLE amperage columns:

- `plugs.max_current_a`         — the operator-set per-plug current cap. NULL =
  the default hardware cutoff (env DEFAULT_PLUG_CAP_A, 16 A on a Tapo P110).
- `charger_groups.max_current_a` — the circuit/line capacity the group's plugs
  share. NULL = no admission limit (unbounded, the pre-caps behaviour).

Admission control (services/caps.py, enforced at session start) is a strict
refinement: with both NULL nothing changes, so this is safe to deploy before
any cap is configured. A ChargerGroup doubles as the electrical circuit here
(the society/office primary case = one group per line); a dedicated Circuit
entity is the upgrade path for a group that spans multiple lines.

Idempotent (same rationale as 0018): a create_all-built DB may already have
these columns from models.py, so guard with information_schema checks.

Revision ID: 0019_current_caps
Revises: 0018_pricing_v2_slots
Create Date: 2026-07-14
"""
from alembic import op

revision = "0019_current_caps"
down_revision = "0018_pricing_v2_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'max_current_a'
            ) THEN
                ALTER TABLE plugs ADD COLUMN max_current_a DOUBLE PRECISION;
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
                WHERE table_name = 'charger_groups' AND column_name = 'max_current_a'
            ) THEN
                ALTER TABLE charger_groups ADD COLUMN max_current_a DOUBLE PRECISION;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE charger_groups DROP COLUMN IF EXISTS max_current_a")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS max_current_a")
