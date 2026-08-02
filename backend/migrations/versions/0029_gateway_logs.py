"""gateway_logs: firmware WARN/ERROR log-line diagnostics feed (TD#28).

Firmware >= 2.1.0-direct forwards WARN/ERROR log lines as plain text to
`amphive/gateways/{gw}/logs` (see firmware/main/main.c log_forward_task,
docs/MQTT_CONTRACT.md). The backend now subscribes and persists each line so
a deployed gateway can be diagnosed without a serial cable — see
services/mqtt/logs.py. Surfaced via GET /api/cpo/gateways/{id}/logs and
GET /api/admin/gateway-logs. Pruned by services/session_reaper.py
reap_gateway_logs_once() per GATEWAY_LOGS_RETENTION_DAYS (default 14 days —
much shorter than gateway_events/telemetry_readings since this is
high-volume, low-value-per-row diagnostic noise).

Idempotent create (same rationale as 0005/0026): a create_all()-built
database may already have this table from models.py.

NOTE: this repo's convention is that migration numbering gets renumbered at
merge time if other in-flight PRs also chain off 0026 (see AGENTS.md /
recent PR history) — 0029 here reserves headroom past 0027/0028 in case
sibling PRs land first; renumber down to the next free slot on rebase if
needed.

Revision ID: 0029_gateway_logs
Revises: 0026_offline_topups
Create Date: 2026-08-02
"""
from alembic import op

revision = "0029_gateway_logs"
down_revision = "0028_payout_settlement_marking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_logs (
            id            BIGSERIAL PRIMARY KEY,
            tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            gateway_id    VARCHAR(50) NOT NULL REFERENCES gateways(id) ON DELETE CASCADE,
            level         VARCHAR(16) NOT NULL,
            message       VARCHAR(220) NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_gateway_logs_tenant_created "
        "ON gateway_logs (tenant_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_gateway_logs_gateway_created "
        "ON gateway_logs (gateway_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gateway_logs")
