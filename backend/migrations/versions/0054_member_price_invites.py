"""member_price_invites: per-member price override + email invites (C9).

- group_memberships.price_per_kwh_override: the member's own flat
  coins-per-kWh price on that group's plugs (NULL = the normal tariff chain).
- charging_sessions.member_price_override: marks a session whose rate is that
  override, frozen at start, so tariff repricing skips it.
- group_invites: digest-only, single-use, expiring email invites to a private
  group (models.GroupInvite).

No enum change, so this migration is fully reversible. The least-privilege
runtime role already covers public-schema tables and sequences created by the
owner (ALTER DEFAULT PRIVILEGES, docs/DATA_MODEL.md), so no GRANT is needed.

Idempotent (same rationale as 0047): a create_all-built database already has
these from models.py, so the DDL is guarded with IF NOT EXISTS. Index names
match the model because test_migrations.py diffs the migrated schema against
it.

Revision ID: 0054_member_price_invites
Revises: 0050_charge_routines
Create Date: 2026-11-19
"""
from alembic import op

revision = "0054_member_price_invites"
down_revision = "0050_charge_routines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE group_memberships "
        "ADD COLUMN IF NOT EXISTS price_per_kwh_override NUMERIC(12, 2)"
    )
    op.execute(
        "ALTER TABLE charging_sessions "
        "ADD COLUMN IF NOT EXISTS member_price_override BOOLEAN"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS group_invites (
            id                  SERIAL PRIMARY KEY,
            group_id            INTEGER NOT NULL REFERENCES charger_groups(id) ON DELETE CASCADE,
            tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email               VARCHAR(255) NOT NULL,
            token_hash          VARCHAR(64) NOT NULL UNIQUE,
            invited_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at          TIMESTAMPTZ NOT NULL,
            accepted_at         TIMESTAMPTZ,
            accepted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            revoked_at          TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_group_invites_group_id "
        "ON group_invites (group_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_group_invites_inviter_created "
        "ON group_invites (invited_by_user_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS group_invites")
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS member_price_override")
    op.execute("ALTER TABLE group_memberships DROP COLUMN IF EXISTS price_per_kwh_override")
