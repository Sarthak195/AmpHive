"""payouts: CPO payout / settlement ledger (record-keeping only).

Adds the `payouts` table + `payout_status` enum backing the CPO settlement
flow: a tenant snapshots its unsettled coin earnings into a REQUESTED payout
(routers/cpo.py POST /api/cpo/payouts), and the platform operator marks it
PAID once the transfer has happened out-of-band (bank/UPI — there is NO
payment-gateway integration in this table, unlike the driver-side Razorpay
top-up flow). See services/payouts.py for the earnings/watermark math.

NOTE on revision chaining: this was authored in parallel with several other
agents each branching their own migration off 0006_gateway_firmware_version
(0007/0008/0009 are not present on this branch). The orchestrator re-chains
everything linearly at merge time; this revision is deliberately numbered
0010 so it lands last in that re-chain. It is self-contained and green on
its own down_revision (0006) standalone.

Idempotent create (same rationale as 0002/0003/0004/0005): a database whose
tables were already built by create_all() against a models.py that includes
Payout would hit "already exists" on a bare CREATE — guard both the enum
(via pg_type) and the table/index (via IF NOT EXISTS).

Revision ID: 0009_payouts
Revises: 0008_notifications
Create Date: 2026-07-12
"""
from alembic import op

revision = "0009_payouts"
down_revision = "0008_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payout_status') THEN
                CREATE TYPE payout_status AS ENUM ('requested', 'paid', 'cancelled');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS payouts (
            id                      SERIAL PRIMARY KEY,
            tenant_id               INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            period_start            TIMESTAMPTZ NOT NULL,
            period_end              TIMESTAMPTZ NOT NULL,
            gross_coins             NUMERIC(12, 2) NOT NULL,
            platform_fee_coins      NUMERIC(12, 2) NOT NULL,
            net_coins               NUMERIC(12, 2) NOT NULL,
            status                  payout_status NOT NULL,
            requested_by_user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            requested_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            paid_at                 TIMESTAMPTZ,
            note                    TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_payouts_tenant_status "
        "ON payouts (tenant_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS payouts")
    op.execute("DROP TYPE IF EXISTS payout_status")
