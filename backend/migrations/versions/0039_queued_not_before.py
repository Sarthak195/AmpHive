"""queued_not_before: queued_charges.not_before for scheduled (off-peak) starts.

NULL = today's outage queue (start when power returns). Set = do not start
before this instant (a scheduled start on a powered plug). No index: WAITING
rows are a handful and the sweep already scans them all. No GRANT needed (no
new object class, same reasoning as 0038). IF NOT EXISTS so a
create_all()-built database migrates cleanly (the 0038 idempotency idiom).

Revision ID: 0039_queued_not_before
Revises: 0038_account_closure
Create Date: 2026-11-15
"""
# PROVISIONAL NUMBERING: PR #127 takes 0039 and 0040; renumber at merge.
from alembic import op

revision = "0039_queued_not_before"
down_revision = "0038_account_closure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE queued_charges ADD COLUMN IF NOT EXISTS not_before TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE queued_charges DROP COLUMN IF EXISTS not_before")
