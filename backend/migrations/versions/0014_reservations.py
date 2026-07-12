"""reservations: bookable time slots on a plug (feat/reservations).

Adds the Reservation model: a driver books a future [start_at, end_at)
window on a plug (POST /api/reservations); during that window only the
holder may start a session (enforced under the plug row lock in
routers/sessions.py start_charging_session), and everyone with plug access
can see the upcoming schedule (GET /api/plugs/{id}/reservations).
Reservations are FREE in v1 — no coin hold, no money movement.

Lifecycle: BOOKED -> CANCELLED | FULFILLED (session_id links the session
that consumed the window) | EXPIRED (lazy no-show expiry — see
services/reservations.py expire_lapsed_reservations; there is no background
sweep). tenant_id is denormalized from plug -> gateway -> tenant, mirroring
ChargingSession/SessionDispute, so CPO-scoped queries need no join.

Overlap exclusion among BOOKED rows is app-level (serialized by
SELECT ... FOR UPDATE on the plug row in the booking path) — deliberately
no tstzrange EXCLUDE constraint here, which would drag in btree_gist for a
race the plug lock already closes.

NOTE on revision chaining: authored against the 0013_auth_holds head.
Sibling feature branches in this parallel batch are also minting "0014"
revisions chained on 0013 — expected; the merge coordinator renumbers/
re-chains at merge time (the 0011_disputes/0013_auth_holds precedent). This
revision only creates the self-contained `reservations` table + enum, so it
is trivially reorderable.

Idempotent create (same rationale as 0005/0011): a create_all-built database
(init_db() stamps a pre-Alembic DB at the baseline, then upgrades to head)
already has this type/table from the current models.py.

Revision ID: 0014_reservations
Revises: 0013_auth_holds
Create Date: 2026-07-12
"""
from alembic import op

revision = "0014_reservations"
down_revision = "0013_auth_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'reservation_status') THEN
                CREATE TYPE reservation_status AS ENUM ('booked', 'cancelled', 'fulfilled', 'expired');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reservations (
            id          SERIAL PRIMARY KEY,
            plug_id     INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            start_at    TIMESTAMPTZ NOT NULL,
            end_at      TIMESTAMPTZ NOT NULL,
            status      reservation_status NOT NULL,
            session_id  INTEGER REFERENCES charging_sessions(id) ON DELETE SET NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Names must match what models.py declares (index=True columns get the
    # SQLAlchemy default ix_<table>_<column> name; the composite is named in
    # __table_args__) — test_migrations.py diffs the migrated schema against
    # the models, indexes included.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_reservations_user_id "
        "ON reservations (user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_reservations_tenant_id "
        "ON reservations (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reservations_plug_start "
        "ON reservations (plug_id, start_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reservations")
    op.execute("DROP TYPE IF EXISTS reservation_status")
