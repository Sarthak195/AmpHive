"""plug_specs: driver-facing charger specs for discovery filters.

Backs the driver "discovery" bundle (search/filter/favorites/find-nearest):
the power and connector filters need two new NULLABLE columns on `plugs`.
Price needs no schema change — `price_per_kwh` is already resolved onto
every plug response by services/pricing.py.

- `rated_power_w` — the ADVERTISED spec (what the driver is told the socket
  supports), deliberately SEPARATE from `max_current_a` (the load-balancing
  policy cap an operator sets for circuit admission — see the Plug model's
  max_current_a comment). A 3.3kW-rated socket can still be capped lower by
  policy; this column never feeds caps.py, only the discovery filter and the
  charger's spec display.
- `connector_type` — VARCHAR, not a native enum, same rationale as
  `plug_model`: connector naming evolves (new adapters, regional variants)
  without a migration. NULL = unset/unknown; the driver-side filter option
  list is built from whatever distinct non-null values are actually in use.

Both NULL until a CPO fills them in on create/edit — existing plugs are
unaffected. An unset plug matches only the driver filter's "Any" option
(never mis-bucketed into a specific power/connector bucket).

Idempotent (same rationale as 0021_plug_power): a create_all-built DB may
already have these columns from models.py, so guard with information_schema
checks.

Revision ID: 0030_plug_specs
Revises: 0026_offline_topups
Create Date: 2026-08-02

NOTE on revision chaining: authored on a branch cut from origin/main while
`0026_offline_topups` was the head. Several PRs may be chaining to 0026 in
parallel — the merge coordinator renumbers at merge time (the same protocol
noted in 0014_plug_watches). 0031_user_favorites chains onto this revision.
"""
from alembic import op

revision = "0030_plug_specs"
down_revision = "0029_gateway_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'rated_power_w'
            ) THEN
                ALTER TABLE plugs ADD COLUMN rated_power_w INTEGER;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'plugs' AND column_name = 'connector_type'
            ) THEN
                ALTER TABLE plugs ADD COLUMN connector_type VARCHAR(32);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS connector_type")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS rated_power_w")
