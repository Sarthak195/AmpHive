"""session_auto_stop_full: per-session opt-in to stop when the car is full.

charging_sessions.auto_stop_when_full: NULL/false = off (every existing row).
True = when the telemetry path decides the car looks fully charged (see
services/mqtt/charge_full.py), finalize the session through the same
finalize_charging_session call a driver stop makes.

queued_charges.auto_stop_when_full carries the same choice from a queued or
scheduled start to the session the reaper later starts (the auto-start runs
long after the request, so the flag has to be stored on the queue row).

Both nullable booleans, no default, no index, no GRANT (no new object class,
same reasoning as 0041). IF NOT EXISTS so a create_all()-built database
migrates cleanly (the 0038 idempotency idiom).

Revision ID: 0044_session_auto_stop_full
Revises: 0043_email_ci_unique
Create Date: 2026-11-17
"""
from alembic import op

revision = "0044_session_auto_stop_full"
down_revision = "0043_email_ci_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE charging_sessions ADD COLUMN IF NOT EXISTS auto_stop_when_full BOOLEAN")
    op.execute("ALTER TABLE queued_charges ADD COLUMN IF NOT EXISTS auto_stop_when_full BOOLEAN")


def downgrade() -> None:
    op.execute("ALTER TABLE queued_charges DROP COLUMN IF EXISTS auto_stop_when_full")
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS auto_stop_when_full")
