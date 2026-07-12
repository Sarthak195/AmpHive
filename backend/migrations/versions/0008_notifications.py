"""notifications + push_subscriptions: driver notification feed & Web Push.

Driver-facing notifications (session stopped/auto-stopped, low balance,
charger offline, safety cutoff, top-up credited) written by
services/notifications.py, listed by GET /api/notifications, and delivered
live via Socket.io user rooms + Web Push (push_subscriptions holds each
browser's subscription; pruned on 404/410 from the push service).

Idempotent create (same rationale as 0002..0005): a create_all-built database
may already have the tables from the models, so guard with IF NOT EXISTS.

Revision ID: 0008_notifications
Revises: 0007_audit_log
Create Date: 2026-07-11
"""
from alembic import op

revision = "0008_notifications"
down_revision = "0007_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id          BIGSERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type        VARCHAR(32) NOT NULL,
            severity    VARCHAR(16) NOT NULL DEFAULT 'info',
            title       VARCHAR(120) NOT NULL,
            body        VARCHAR(500) NOT NULL,
            plug_id     INTEGER REFERENCES plugs(id) ON DELETE SET NULL,
            session_id  INTEGER REFERENCES charging_sessions(id) ON DELETE SET NULL,
            read        BOOLEAN NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_created "
        "ON notifications (user_id, created_at)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            endpoint    VARCHAR(1024) NOT NULL UNIQUE,
            p256dh      VARCHAR(255) NOT NULL,
            auth        VARCHAR(64) NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_push_subscriptions_user_id "
        "ON push_subscriptions (user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS push_subscriptions")
    op.execute("DROP TABLE IF EXISTS notifications")
