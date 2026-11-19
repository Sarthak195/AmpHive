"""notification_prefs: per-user push preferences and quiet hours (C7).

users.notification_prefs: nullable JSONB. NULL means the defaults (every
suppressible push on, no quiet hours), so every existing row keeps today's
behaviour. The stored shape is services/notification_prefs.NotificationPrefs
dumped as JSON; the API validates it before it is written.

The prefs gate Web Push only. The in-app feed row is always written, and the
non-suppressible types (safety, faults, billing receipts) always push.

No default, no index, no GRANT (a new column on an existing table, same
reasoning as 0044). IF NOT EXISTS so a create_all()-built database migrates
cleanly (the 0038 idempotency idiom).

Revision ID: 0048_notification_prefs
Revises: 0047_promo_codes
Create Date: 2026-11-19
"""
from alembic import op

revision = "0048_notification_prefs"
down_revision = "0047_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_prefs JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS notification_prefs")
