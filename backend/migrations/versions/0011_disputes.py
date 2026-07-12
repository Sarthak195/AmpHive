"""session_disputes: driver dispute + CPO refund/reject flow (coins-only).

Adds the SessionDispute model: a driver files a dispute against a finished
ChargingSession (POST /api/sessions/{id}/dispute); the CPO owning the
session's plug approves or rejects it (GET /api/cpo/disputes, POST
/api/cpo/disputes/{id}/resolve). Coins-only remedy — there is no Razorpay
money-out path. An approved dispute credits the driver's coin wallet via
services/wallet.credit_wallet and writes a REFUND LedgerTransaction
referencing the session (see MARKET_GAP_ANALYSIS.md §3 "Refunds").

At most one OPEN dispute per session is enforced by a partial unique index
(not just app-level check-then-insert), so a double-submit race can't create
two open disputes on the same session. Resolving (approve) row-locks the
session so the cumulative APPROVED refund_coins for a session can never
exceed its coins_spent even under concurrent resolves of two different
disputes on the same session (backend/routers/cpo.py cpo_resolve_dispute).

Idempotent create (same rationale as 0005_gateway_events): a create_all-built
database (init_db() stamps a pre-Alembic DB at the baseline, then upgrades to
head) already has this type/table from the current models.py.

Chained onto 0007_audit_log (the actual head at the time this revision was
authored — this feature branch was originally planned off 0006 alongside
other in-flight, not-yet-merged migrations, but 0007 landed on main first;
re-pointing here keeps this branch's `alembic upgrade head` single-headed and
green standalone instead of leaving a dangling 0006 fork for the orchestrator
to untangle). Any further 0008/0010-style migrations still in flight get
re-chained past this one at merge time same as always.

Revision ID: 0011_disputes
Revises: 0010_tariffs
Create Date: 2026-07-12
"""
from alembic import op

revision = "0011_disputes"
down_revision = "0010_tariffs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'dispute_status') THEN
                CREATE TYPE dispute_status AS ENUM ('open', 'approved', 'rejected');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_disputes (
            id                   SERIAL PRIMARY KEY,
            session_id           INTEGER NOT NULL REFERENCES charging_sessions(id) ON DELETE CASCADE,
            tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            driver_user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason               TEXT NOT NULL,
            status               dispute_status NOT NULL,
            resolution_note      TEXT,
            refund_coins         NUMERIC(12, 2),
            created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at          TIMESTAMPTZ,
            resolved_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_disputes_session_id "
        "ON session_disputes (session_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_disputes_tenant_id "
        "ON session_disputes (tenant_id)"
    )
    # "At most one OPEN dispute per session" as a DB constraint, not just
    # app-level check-then-insert: a partial unique index over rows where
    # status = 'open'. Resolved (approved/rejected) disputes fall outside the
    # predicate, so a session can accumulate several resolved disputes over
    # time — only a second simultaneously-open one is rejected (the router
    # catches the IntegrityError and returns 409).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_session_disputes_one_open_per_session "
        "ON session_disputes (session_id) WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_disputes")
    op.execute("DROP TYPE IF EXISTS dispute_status")
