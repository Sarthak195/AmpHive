"""reservations: started_notified_at for the reservation-start janitor.

Adds a nullable started_notified_at timestamp to reservations. The
reservation-start sweep (services/session_reaper.py
reap_reservation_starts_once, piggybacked on SessionReaperService — the
follow-up the reservations module header foretold) stamps it the first time
it processes a booking's window opening: it nudges the holder ("your
reservation has started") and force-stops any non-holder session still
running on the plug — a walk-up that started LEGALLY before the window (the
session-start gate only blocks NEW non-holder starts once the window covers
now; it can't touch a session already in progress). The column makes that
sweep idempotent across its 60 s ticks and across a restart, so neither the
nudge nor the force-stop ever fires twice for one reservation.

Idempotent add (ADD COLUMN IF NOT EXISTS): a create_all-built database
(init_db() stamps a pre-Alembic DB at the baseline, then upgrades to head)
already has this column from the current models.py.

NOTE the revision id is deliberately short: alembic_version.version_num is
VARCHAR(32), so a longer id (the original "0017_reservation_started_notified"
was 33 chars) fails to record with StringDataRightTruncationError and the
whole migration rolls back. Keep every revision id <= 32 characters.

Revision ID: 0017_reservation_started
Revises: 0016_reservations
Create Date: 2026-07-13
"""
from alembic import op

revision = "0017_reservation_started"
down_revision = "0016_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE reservations "
        "ADD COLUMN IF NOT EXISTS started_notified_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE reservations DROP COLUMN IF EXISTS started_notified_at"
    )
