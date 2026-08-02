"""gateway_claim_code: preflashed-unit claim-code onboarding.

1. gateways.tenant_id DROP NOT NULL — a gateway can now exist as UNCLAIMED
   inventory (admin-minted via POST /api/admin/gateways/inventory, tenant_id
   NULL) before a CPO binds it to their tenant via POST
   /api/cpo/gateways/claim. Mirrors the users.tenant_id nullable-FK
   precedent (platform admins have tenant_id NULL by design); DROP NOT NULL
   is naturally idempotent (same rationale as 0025_user_disable).

2. gateways.claim_code VARCHAR(16) — the short, unambiguous-alphabet code
   printed on a preflashed unit's label. NULL for gateways created the old
   way (direct CPO registration via POST /api/cpo/gateways). Not cleared on
   claim — kept for admin audit/reprint.

3. gateways.claimed_at TIMESTAMPTZ — set once, when a claim succeeds. NULL
   = never claimed (unclaimed inventory, or a legacy pre-claim-code row).

4. uq_gateways_claim_code — a partial UNIQUE index on claim_code WHERE NOT
   NULL, so the many NULL rows (gateways with no claim code at all) never
   collide with each other. Same pattern as uq_users_google_sub (migration
   0033_google_identity) / ix_session_disputes_one_open_per_session
   (migration 0011_disputes).

Idempotent add (same rationale as 0025/0033): a create_all-built database
already has these columns/index/relaxed-nullability from the current
models.py, so guard the ALTER TABLE / use IF NOT EXISTS throughout.

Revision ID: 0034_gateway_claim_code
Revises: 0033_google_identity
Create Date: 2026-08-02
"""
from alembic import op

revision = "0034_gateway_claim_code"
down_revision = "0033_google_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE gateways ALTER COLUMN tenant_id DROP NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'gateways' AND column_name = 'claim_code'
            ) THEN
                ALTER TABLE gateways ADD COLUMN claim_code VARCHAR(16);
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
                WHERE table_name = 'gateways' AND column_name = 'claimed_at'
            ) THEN
                ALTER TABLE gateways ADD COLUMN claimed_at TIMESTAMPTZ;
            END IF;
        END $$;
        """
    )
    # Partial unique index: only inventory-minted rows are constrained, so
    # the many NULLs (hand-registered gateways) coexist freely.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_gateways_claim_code "
        "ON gateways (claim_code) WHERE claim_code IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_gateways_claim_code")
    op.execute("ALTER TABLE gateways DROP COLUMN IF EXISTS claimed_at")
    op.execute("ALTER TABLE gateways DROP COLUMN IF EXISTS claim_code")
    # Re-tightening NOT NULL would fail on any unclaimed (tenant_id NULL)
    # inventory row; downgrade is destructive for those by necessity — drop
    # them first (their broker credentials/claim code are gone anyway once
    # the columns above are dropped).
    op.execute("DELETE FROM gateways WHERE tenant_id IS NULL")
    op.execute("ALTER TABLE gateways ALTER COLUMN tenant_id SET NOT NULL")
