"""email_ci_unique: case-insensitive unique index on users.email.

users.email carried only a byte-exact UNIQUE, while every lookup compares
lower(email) (services.auth.normalize_email + func.lower(User.email)). A
case-variant duplicate (Driver@x.com next to driver@x.com) would therefore make
login raise MultipleResultsFound (HTTP 500). Every write path already stores
the lower-cased address (register, Google sign-in, seed, tombstone), so this
index only backstops a race or a hand-made row.

Prod was checked 2026-11-16: 0 case-variant duplicate groups, 0 mixed-case
emails. If a database does hold duplicates, CREATE UNIQUE INDEX fails loudly
and the migration aborts (nothing is merged automatically): resolve the
duplicate accounts by hand and re-run.

The old byte-exact unique is kept: it is harmless alongside the new one and
dropping it would only add rollback risk.

Idempotent (IF NOT EXISTS): a create_all-built database already has the index
from models.py. The name matches the model so test_migrations.py (alembic
compare_metadata) sees no drift.

Revision ID: 0043_email_ci_unique
Revises: 0042_revoked_sessions
Create Date: 2026-11-16
"""
from alembic import op

revision = "0043_email_ci_unique"
down_revision = "0042_revoked_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower "
        "ON users (lower(email))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_email_lower")
