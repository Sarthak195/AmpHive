"""user_disable: users.is_disabled kill switch + platform-level audit rows.

1. users.is_disabled BOOLEAN NOT NULL DEFAULT false — the platform-admin
   account kill switch (PATCH /api/admin/users/{id}). Enforced at login
   (403 "account_disabled") AND in services/auth.get_current_user, so a
   disabled user's existing tokens die immediately, not just future logins.

2. audit_logs.tenant_id DROP NOT NULL — the admin console's user actions
   (disable, role change, balance adjustment) must be audited, but neither
   the actor (platform admins have tenant_id NULL by design) nor a plain
   driver target has a tenant to scope the row to. NULL = a platform-level
   action; tenant-scoped rows are written exactly as before, and the
   tenant-scoped GET /api/cpo/audit equality filter never matches NULL.

Idempotent add (same rationale as 0003): a create_all-built database already
has the column (and the relaxed nullability) from the models, so guard the
ADD COLUMN; DROP NOT NULL is naturally idempotent.

Revision ID: 0025_user_disable
Revises: 0024_session_ledger_indexes
Create Date: 2026-07-21
"""
from alembic import op

revision = "0025_user_disable"
down_revision = "0024_session_ledger_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'is_disabled'
            ) THEN
                ALTER TABLE users
                    ADD COLUMN is_disabled BOOLEAN NOT NULL DEFAULT false;
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE audit_logs ALTER COLUMN tenant_id DROP NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_disabled")
    # Platform-level rows (tenant_id NULL) block re-tightening; drop them
    # first — downgrade is destructive for those audit rows by necessity.
    op.execute("DELETE FROM audit_logs WHERE tenant_id IS NULL")
    op.execute("ALTER TABLE audit_logs ALTER COLUMN tenant_id SET NOT NULL")
