"""capacity_requests: one-shot "request capacity" queue for a full circuit.

Part of the caps + circuits feature. When a driver's start is refused because
the plug's circuit is at capacity (services/caps.py check_circuit_admission),
they can arm a request via POST /api/plugs/{id}/request-capacity. When the
circuit next has room — a session on it ends (finalize_charging_session) or the
operator raises the cap (PUT /api/cpo/groups) — services/capacity.py notifies
every requester whose plug would now be admitted and DELETES their rows
(one-shot, mirroring plug_watches).

UNIQUE(user_id, plug_id) makes arming idempotent (double-tap → IntegrityError
treated as already-requested). idx_capacity_requests_group serves the per-
circuit fan-out. Both FKs CASCADE — transient state, not history. group_id is
denormalized (captured at request time) like charging_sessions.tenant_id.

Idempotent create (same rationale as 0002 onward): guard with IF NOT EXISTS.

Revision ID: 0020_capacity_requests
Revises: 0019_current_caps
Create Date: 2026-07-14
"""
from alembic import op

revision = "0020_capacity_requests"
down_revision = "0019_current_caps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS capacity_requests (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plug_id     INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            group_id    INTEGER NOT NULL REFERENCES charger_groups(id) ON DELETE CASCADE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_capacity_requests_user_plug UNIQUE (user_id, plug_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_capacity_requests_group "
        "ON capacity_requests (group_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS capacity_requests")
