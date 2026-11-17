"""cpo_viewer_role: read-only CPO demo login (feature F10).

Adds one value to the native `user_role` enum (database/models.py
UserRole.CPO_VIEWER). A cpo_viewer sees its tenant's CPO console but every
request other than GET/HEAD/OPTIONS is refused with 403 in the single auth
path (services/auth.py enforce_viewer_read_only), apart from signing out.
A new enum value fails closed: no existing role check names it, so any route
that was not deliberately widened keeps answering 403.

`ADD VALUE IF NOT EXISTS` is idempotent (PG 9.6+) and safe inside Alembic's
per-migration transaction on PG 12+, the same pattern as 0026_offline_topups
(tx_type 'cpo_topup'). This migration never reads or writes the new value.

Downgrade is a documented no-op: Postgres cannot drop a value from an enum
type. An image built before this migration does not know this revision and
cannot boot against an upgraded database, so tag a rollback image built from
this revision before deploying it.

Revision ID: 0045_cpo_viewer_role
Revises: 0043_email_ci_unique
Create Date: 2026-11-17
"""
from alembic import op

revision = "0045_cpo_viewer_role"
down_revision = "0043_email_ci_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'cpo_viewer'")


def downgrade() -> None:
    # No-op: Postgres has no ALTER TYPE ... DROP VALUE. A leftover
    # 'cpo_viewer' label is inert once the code no longer names it; delete or
    # demote any cpo_viewer users by hand if the role is retired.
    pass
