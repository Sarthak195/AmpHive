"""promo_codes: CPO-funded promo codes, their redemptions, and the link from
offline_topups.

1. promo_codes: one row per code a CPO issues (models.PromoCode). Only the
   SHA-256 hex digest of the code is stored (code_hash, unique), plus its last
   4 characters (code_hint) so the CPO can tell codes apart in a list.
2. promo_redemptions: one row per redemption (models.PromoRedemption), pointing
   at the offline top-up it created (offline_topup_id, unique).
3. offline_topups.promo_code_id: nullable, set when the top-up came from a
   redemption rather than a cash top-up.

No enum change: a redemption reuses the existing tx_type value 'cpo_topup', so
this migration is fully reversible (unlike 0026/0038, which had to ALTER TYPE).

The least-privilege runtime role already covers public-schema tables and
sequences created by the owner (ALTER DEFAULT PRIVILEGES, docs/DATA_MODEL.md),
so no extra GRANT is needed.

Idempotent (same rationale as 0026/0037): a create_all-built database already
has these tables and the column from models.py, so every DDL is guarded with
IF NOT EXISTS. Index names match the models because test_migrations.py diffs
the migrated schema against them.

Revision ID: 0047_promo_codes
Revises: 0046_api_tokens
Create Date: 2026-11-17
"""
from alembic import op

revision = "0047_promo_codes"
down_revision = "0046_api_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS promo_codes (
            id               SERIAL PRIMARY KEY,
            tenant_id        INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            code_hash        VARCHAR(64) NOT NULL UNIQUE,
            code_hint        VARCHAR(4) NOT NULL,
            amount_coins     NUMERIC(12, 2) NOT NULL,
            expires_at       TIMESTAMPTZ,
            max_redemptions  INTEGER NOT NULL,
            per_user_limit   INTEGER NOT NULL DEFAULT 1,
            note             VARCHAR(200),
            created_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            disabled_at      TIMESTAMPTZ,
            CONSTRAINT ck_promo_codes_amount_positive CHECK (amount_coins > 0),
            CONSTRAINT ck_promo_codes_max_redemptions CHECK (max_redemptions >= 1),
            CONSTRAINT ck_promo_codes_per_user_limit CHECK (per_user_limit >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_promo_codes_tenant_created "
        "ON promo_codes (tenant_id, created_at)"
    )
    op.execute(
        "ALTER TABLE offline_topups ADD COLUMN IF NOT EXISTS promo_code_id "
        "INTEGER REFERENCES promo_codes(id) ON DELETE SET NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS promo_redemptions (
            id                SERIAL PRIMARY KEY,
            code_id           INTEGER NOT NULL REFERENCES promo_codes(id) ON DELETE CASCADE,
            user_id           INTEGER REFERENCES users(id) ON DELETE SET NULL,
            offline_topup_id  INTEGER NOT NULL UNIQUE REFERENCES offline_topups(id) ON DELETE CASCADE,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_promo_redemptions_code_user "
        "ON promo_redemptions (code_id, user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS promo_redemptions")
    op.execute("ALTER TABLE offline_topups DROP COLUMN IF EXISTS promo_code_id")
    op.execute("DROP TABLE IF EXISTS promo_codes")
