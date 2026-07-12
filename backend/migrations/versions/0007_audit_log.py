"""audit_log: CPO admin action audit trail (TD#26).

Gateway/plug/group create-delete, status changes, and access-code
regeneration previously left no record of who did what in this multi-tenant
billing system. `services/audit.py` now writes an AuditLog row
(record_audit / try_record_audit) after each such action's own commit has
already landed in `routers/cpo.py`. Read via `GET /api/cpo/audit`.

Idempotent create (same rationale as 0002/0003/0004/0005): a create_all-built
database may already have the table from the model, so guard with IF NOT
EXISTS.

Revision ID: 0007_audit_log
Revises: 0006_gateway_firmware_version
Create Date: 2026-07-12
"""
from alembic import op

revision = "0007_audit_log"
down_revision = "0006_gateway_firmware_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id              BIGSERIAL PRIMARY KEY,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            actor_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            action          VARCHAR(64) NOT NULL,
            target_type     VARCHAR(32) NOT NULL,
            target_id       VARCHAR(64),
            detail          TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_created "
        "ON audit_logs (tenant_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_logs")
