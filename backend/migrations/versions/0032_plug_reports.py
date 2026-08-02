"""plug_reports: driver "report a problem with this charger" flow (no session,
no money — deliberately NOT bolted onto SessionDispute).

Adds the PlugReport model: any authenticated driver who can see a plug
(ensure_plug_group_access — same 403 rule as watch_plug) can flag it as
damaged/wrong_info/unsafe/other (POST /api/plugs/{plug_id}/report), with no
requirement that they ever charged there and no refund/money path — this is
purely a "something's wrong with this hardware" signal for the CPO, distinct
from SessionDispute (session-FK'd NOT NULL, coins-only refund remedy — see
0011_disputes.py). The CPO owning the plug's tenant reviews it (GET
/api/cpo/plug-reports, POST /api/cpo/plug-reports/{id}/resolve).

Filing also writes a GatewayEvent (event_type="DRIVER_PROBLEM_REPORT",
severity="warning") so the existing CPO alert strip / Health badge pick it up
for free — no new alert pipeline needed here.

Unlike session_disputes' partial unique "one open per session" index, this
table has NO one-open-per-plug constraint: several drivers may independently
flag the same charger, and each report is tracked (and resolved) on its own —
a second report on an already-flagged plug is not a conflict.

Idempotent create (same rationale as 0005_gateway_events / 0011_disputes): a
create_all-built database (init_db() stamps a pre-Alembic DB at the baseline,
then upgrades to head) already has this type/table from the current
models.py.

Chained onto 0026_offline_topups (the actual head at authoring time — verified
by reading the versions/ directory rather than assuming; per this repo's
renumbering-at-merge convention, any further migrations still in flight get
re-chained past this one when they land).

Revision ID: 0032_plug_reports
Revises: 0026_offline_topups
Create Date: 2026-08-01
"""
from alembic import op

revision = "0032_plug_reports"
down_revision = "0031_user_favorites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'plug_report_status') THEN
                CREATE TYPE plug_report_status AS ENUM ('open', 'acknowledged', 'resolved');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plug_reports (
            id                   SERIAL PRIMARY KEY,
            plug_id              INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            driver_user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category             VARCHAR(32) NOT NULL,
            description          TEXT NOT NULL,
            status               plug_report_status NOT NULL DEFAULT 'open',
            resolution_note      TEXT,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at          TIMESTAMPTZ,
            resolved_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_plug_reports_tenant_created "
        "ON plug_reports (tenant_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_plug_reports_plug "
        "ON plug_reports (plug_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plug_reports")
    op.execute("DROP TYPE IF EXISTS plug_report_status")
