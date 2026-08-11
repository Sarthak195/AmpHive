"""email_verification: users.email_verified + email_verification_tokens table.

Closes the account-PRE-hijacking class at its root — an unverified email can no
longer hold a usable credential:

1. email_verification_tokens — single-use, time-boxed "verify your email"
   tokens (EmailVerificationToken). Only the SHA-256 hex digest of the emailed
   token is stored (token_hash, unique); used_at NULL means still consumable,
   expires_at time-boxes the link. Column/index idioms match
   0023_password_reset_tokens exactly (its mirror).

2. users.email_verified BOOLEAN NOT NULL DEFAULT false — whether the account
   has proven ownership of its email. New password signups get false via the
   app (register emails a token and no longer auto-logs-in; login 403s until
   this flips true). Google signups/links get true (Google asserts the
   verified email).

   GRANDFATHER: right after adding the column every EXISTING row is set true
   (`UPDATE users SET email_verified = true`). This is CRITICAL — without it
   the live prod user base (all predating this feature) would be locked out of
   login by the new unverified-gate. Only signups made AFTER this migration,
   through the app, start false. `users` is a small table, so this inline
   backfill is fine per docs/DATA_MODEL.md §4 (the "no inline backfill" rule is
   about hot tables like telemetry_readings).

The least-privilege runtime role already covers public-schema tables/sequences
created by the owner (docs/DATA_MODEL.md §4 object-class limit); this ordinary
table/sequence qualifies, so no extra GRANT is needed.

Idempotent (same rationale as 0023/0025/0033): a create_all-built database
already has the table (and the column) from models.py — guard every DDL with
IF NOT EXISTS. Index names match the SQLAlchemy defaults
(ix_email_verification_tokens_user_id for the index=True user_id column; the
unique=True token_hash gets an inline UNIQUE constraint) because
test_migrations.py diffs the migrated schema against the models.

Revision ID: 0037_email_verification
Revises: 0036_firmware_releases
Create Date: 2026-08-11
"""
from alembic import op

revision = "0037_email_verification"
down_revision = "0036_firmware_releases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at    TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_verification_tokens_user_id "
        "ON email_verification_tokens (user_id)"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'email_verified'
            ) THEN
                ALTER TABLE users
                    ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT false;
                -- GRANDFATHER every pre-existing account as verified so the
                -- live prod user base is NOT locked out by the new login gate.
                -- New signups get false through the app, never here. `users`
                -- is small, so this inline backfill is safe (DATA_MODEL §4).
                UPDATE users SET email_verified = true;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email_verified")
    op.execute("DROP TABLE IF EXISTS email_verification_tokens")
