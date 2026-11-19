"""ocpp_charge_points: OCPP 1.6J central-system adapter tables (feature C2).

Three tables back the flag-gated OCPP adapter (services/ocpp_adapter.py,
OCPP_CSMS_ENABLED, off by default and off in production):

- ocpp_charge_points: one row per registered charge point, its tenant, the
  ordinary gateways row that backs it (its connectors are ordinary plugs on
  that gateway) and a bcrypt hash of its HTTP Basic password.
- ocpp_id_tags: idTags a driver bound to their account, stored only as a
  SHA-256 hex digest (unique).
- ocpp_transactions: the OCPP side of a charging session (the OCPP
  transactionId is charging_sessions.id), with the charger's meterStart.

No enum change, so this migration is fully reversible. Idempotent (same
rationale as 0046 and 0050): a create_all-built database already has the
tables from models.py, so the DDL is guarded with IF NOT EXISTS. Index and
constraint names match the model because test_migrations.py diffs the
migrated schema against it. The least-privilege runtime role already covers
public-schema tables and sequences created by the owner (ALTER DEFAULT
PRIVILEGES, docs/DATA_MODEL.md), so no GRANT is needed. With the flag off the
tables stay empty and nothing reads them.

Revision ID: 0056_ocpp_charge_points
Revises: 0050_charge_routines
Create Date: 2026-11-19
"""
from alembic import op

revision = "0056_ocpp_charge_points"
down_revision = "0050_charge_routines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ocpp_charge_points (
            id                SERIAL PRIMARY KEY,
            charge_point_id   VARCHAR(40) NOT NULL UNIQUE,
            tenant_id         INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            gateway_id        VARCHAR(50) NOT NULL UNIQUE REFERENCES gateways(id) ON DELETE CASCADE,
            password_hash     VARCHAR(255) NOT NULL,
            vendor            VARCHAR(20),
            model             VARCHAR(20),
            firmware_version  VARCHAR(50),
            last_boot_at      TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ocpp_id_tags (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tag_hash    VARCHAR(64) NOT NULL UNIQUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ocpp_id_tags_user_id "
        "ON ocpp_id_tags (user_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ocpp_transactions (
            session_id       INTEGER PRIMARY KEY REFERENCES charging_sessions(id) ON DELETE CASCADE,
            charge_point_id  INTEGER NOT NULL REFERENCES ocpp_charge_points(id) ON DELETE CASCADE,
            connector_id     INTEGER NOT NULL,
            id_tag_hash      VARCHAR(64) NOT NULL,
            meter_start_wh   BIGINT NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ocpp_transactions_charge_point_id "
        "ON ocpp_transactions (charge_point_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ocpp_transactions")
    op.execute("DROP TABLE IF EXISTS ocpp_id_tags")
    op.execute("DROP TABLE IF EXISTS ocpp_charge_points")
