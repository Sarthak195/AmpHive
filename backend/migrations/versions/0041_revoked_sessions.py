"""revoked_sessions: per-device logout revocation list (JWT `sid` claim).

Adds the RevokedSession model (database/models.py) backing per-device logout:
POST /api/auth/logout inserts the caller's sign-in session id here, and every
token-accepting path (get_current_user, get_current_user_optional, socket.io
connect and its re-auth) rejects a token whose sid is listed. Log out
everywhere keeps using the users.token_version bump and writes nothing here.

Rows are pruned opportunistically after each insert once expires_at (the last
moment any token of that session could still be valid) has passed.

Idempotent create (same rationale as 0039_plug_reviews / 0040_plug_photos): a
create_all-built database already has the table from models.py, so every
statement is IF NOT EXISTS. Index names match the SQLAlchemy defaults so
test_migrations.py sees no drift. The least-privilege runtime role needs
SELECT, INSERT and DELETE here, all covered by the owner role's ALTER DEFAULT
PRIVILEGES (docs/DATA_MODEL.md), so no extra GRANT.

Downgrade converts every still-live per-device revocation into a global one
(token_version bump for the affected users) BEFORE dropping the table, so a
downgrade never silently un-revokes a logged-out device. The same statement
is the rollback runbook step for an image rollback that keeps the table
(docs/SECURITY.md, docs/DEPLOYMENT.md).

Revision ID: 0041_revoked_sessions
Revises: 0040_plug_photos
Create Date: 2026-11-15
"""
# PROVISIONAL NUMBERING: chained onto 0040_plug_photos, the head on main when
# this was written. The off-peak feature also claims 0041; at integration this
# file becomes 0042_revoked_sessions with down_revision repointed to the
# off-peak 0041 (same parallel-migration convention as
# 0028_payout_settlement_marking). test_migration_heads.py enforces one head.
from alembic import op

revision = "0041_revoked_sessions"
down_revision = "0040_plug_photos"
branch_labels = None
depends_on = None

# Shared with docs/SECURITY.md (rollback runbook): turn every live per-device
# revocation into a token_version bump so pre-feature code, which ignores the
# sid claim, still rejects those tokens.
PROMOTE_LIVE_REVOCATIONS_SQL = (
    "UPDATE users SET token_version = token_version + 1 "
    "WHERE id IN (SELECT DISTINCT user_id FROM revoked_sessions "
    "WHERE expires_at > now())"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS revoked_sessions (
            sid         TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            revoked_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at  TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_revoked_sessions_user_id "
        "ON revoked_sessions (user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_revoked_sessions_expires_at "
        "ON revoked_sessions (expires_at)"
    )


def downgrade() -> None:
    op.execute(PROMOTE_LIVE_REVOCATIONS_SQL)
    op.execute("DROP TABLE IF EXISTS revoked_sessions")
