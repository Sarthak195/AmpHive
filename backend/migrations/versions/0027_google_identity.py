"""google_identity: users.auth_provider + google_sub for "Sign in with Google".

1. users.auth_provider VARCHAR(20) NOT NULL DEFAULT 'password' — which
   identity provider created this account. Every pre-existing row backfills
   to 'password' (the only provider that existed before this revision).
   Purely informational: it does not gate the password-login route by
   itself — a Google-only account gets an unusable random bcrypt hash (the
   same "unusable hash" trick as services/auth._DUMMY_PASSWORD_HASH), so
   password login already refuses it correctly with zero route changes.

2. users.google_sub VARCHAR(255) — the verified Google ID token's "sub"
   claim, once this account has signed in with Google at least once. NULL
   for accounts that never linked Google. A password-created account can
   gain a google_sub later (linking) without losing its password or having
   auth_provider rewritten.

3. uq_users_google_sub — a partial UNIQUE index on google_sub WHERE NOT
   NULL, so a Google identity can back at most one AmpHive account while the
   many NULL rows (unlinked accounts) never collide with each other. Same
   pattern as ix_session_disputes_one_open_per_session (migration
   0011_disputes): a partial unique index is the DB-level backstop for a
   check-then-insert race backend/routers/auth.py's google_callback catches
   as IntegrityError.

Idempotent add (same rationale as 0003/0025): a create_all-built database
already has these columns/index from the current models.py, so guard the
ALTER TABLE / use IF NOT EXISTS throughout.

Revision ID: 0027_google_identity
Revises: 0026_offline_topups
Create Date: 2026-08-02
"""
from alembic import op

revision = "0027_google_identity"
down_revision = "0026_offline_topups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'auth_provider'
            ) THEN
                ALTER TABLE users
                    ADD COLUMN auth_provider VARCHAR(20) NOT NULL DEFAULT 'password';
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
                WHERE table_name = 'users' AND column_name = 'google_sub'
            ) THEN
                ALTER TABLE users ADD COLUMN google_sub VARCHAR(255);
            END IF;
        END $$;
        """
    )
    # Partial unique index: only rows with a linked Google identity are
    # constrained, so the many NULLs (unlinked accounts) coexist freely.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_google_sub "
        "ON users (google_sub) WHERE google_sub IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_google_sub")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS google_sub")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS auth_provider")
