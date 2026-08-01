"""payout_settlement_marking: charging_sessions.settled_payout_id row-ownership.

Replaces the ended_at/watermark TIME-WINDOW that used to decide which
sessions were "unsettled" for a CPO's payout GROSS with row-ownership: each
payout claims its sessions by stamping settled_payout_id in the SAME
UPDATE...RETURNING statement that sums their coins_spent
(services/payouts.py mark_unsettled_sessions_and_sum_gross), so the read and
the claim can never disagree -- closing the watermark race documented in
that module's docstring (a session finalizing concurrently with a payout
snapshot could commit with an ended_at just behind the snapshot's `now` and
never be paid out under the old windowed scheme). NULL = unsettled,
regardless of ended_at; cpo_cancel_payout resets a cancelled payout's
claimed rows back to NULL, freeing them for the next request. Refund/top-up
windowing is UNCHANGED -- still watermark/ended_at/resolved_at-based -- and
out of scope for this migration.

Three changes:

1. `charging_sessions.settled_payout_id` INTEGER, FK -> payouts(id) ON
   DELETE SET NULL, nullable (NULL = unsettled). A payout row disappearing
   (there is no delete path for Payout today, but ON DELETE SET NULL is the
   safe default for an accounting FK either way) must never take a session
   row down with it.
2. Two indexes, mirroring the ORM's __table_args__ (database/models.py
   ChargingSession): a plain btree on settled_payout_id (FK lookups /
   cpo_cancel_payout's free-on-cancel UPDATE), and a partial composite
   (tenant_id, status) WHERE settled_payout_id IS NULL backing the new
   unsettled-sessions query (mark_unsettled_sessions_and_sum_gross /
   sum_unsettled_session_coins) -- same partial-index idiom as
   0011_disputes' ix_session_disputes_one_open_per_session.
3. BACKFILL (`_BACKFILL_SQL` -- the part that makes this deployable on real
   data): every payout that already exists was created under the OLD
   windowed scheme and therefore never claimed its sessions -- their
   settled_payout_id is NULL across the board. Without a backfill, the
   first post-deploy payout request would claim (and RE-PAY) every historic
   already-paid-out session in one shot. So this migration reproduces the
   old scheme's attribution once, as data: each FINISHED session
   (status completed/paid, matching services/payouts.py's
   FINISHED_SESSION_STATUSES) whose ended_at falls inside a non-CANCELLED
   payout's [period_start, period_end) window is stamped with that payout's
   id. Non-cancelled windows are disjoint and tile [EPOCH, watermark) by
   construction (each new payout's period_start = the previous
   MAX(period_end)), so the attribution is deterministic. Sessions that
   were LOST to the old watermark race (committed after their covering
   payout's snapshot ran) get stamped too -- they were never actually paid,
   but retroactively distinguishing them from paid ones is impossible (the
   old scheme kept no per-session record; that is exactly the defect), so
   the backfill preserves the old scheme's attribution as-was and the fix
   is forward-looking. The `cs.settled_payout_id IS NULL` guard makes the
   statement idempotent: re-running never re-stamps a claimed row.

Idempotent add (same rationale as 0025/0026): a create_all()-built database
already has this column/indexes from database/models.py (and, being brand
new, no legacy rows for the backfill to touch).

PROVISIONAL NUMBERING: down_revision chains to 0026_offline_topups, the
actual migration head on main at the time this was written (there was no
merged 0027 yet). If a 0027_xxx.py lands first, this file is renumbered to
0028 and its down_revision repointed to 0027 as part of the merge, per this
repo's convention for parallel in-flight migrations (see the PR
description).

Revision ID: 0028_payout_settlement_marking
Revises: 0026_offline_topups
Create Date: 2026-08-02
"""
from alembic import op

revision = "0028_payout_settlement_marking"
down_revision = "0026_offline_topups"
branch_labels = None
depends_on = None

# Module-level so backend/tests/test_payouts.py can execute the exact same
# statement against a seeded schema (the alembic-driven migration tests only
# run against an EMPTY database, which can't exercise a data backfill).
# See point 3 of the module docstring for the full rationale.
_BACKFILL_SQL = """
    UPDATE charging_sessions AS cs
    SET settled_payout_id = p.id
    FROM payouts AS p
    WHERE cs.settled_payout_id IS NULL
      AND cs.tenant_id = p.tenant_id
      AND p.status != 'cancelled'
      AND cs.status IN ('completed', 'paid')
      AND cs.ended_at >= p.period_start
      AND cs.ended_at < p.period_end
"""


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'settled_payout_id'
            ) THEN
                ALTER TABLE charging_sessions
                    ADD COLUMN settled_payout_id INTEGER
                        REFERENCES payouts(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_charging_sessions_settled_payout_id "
        "ON charging_sessions (settled_payout_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_charging_sessions_unsettled "
        "ON charging_sessions (tenant_id, status) WHERE settled_payout_id IS NULL"
    )
    # Claim legacy sessions for the pre-marking payouts that covered them --
    # see point 3 of the module docstring. Runs after the indexes exist so
    # the join is cheap even on a large history.
    op.execute(_BACKFILL_SQL)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_charging_sessions_unsettled")
    op.execute("DROP INDEX IF EXISTS idx_charging_sessions_settled_payout_id")
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS settled_payout_id")
