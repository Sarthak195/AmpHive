"""gst_invoices: formal numbered GST tax invoices per session.

Adds:
- Four nullable-ish GST/invoice-numbering columns on `tenants`: `gstin`,
  `legal_name`, `invoice_prefix` (all nullable — GST config is an opt-in
  rollout step, not a schema requirement) and `next_invoice_seq` (NOT NULL,
  default 1 — the counter allocated under a tenant row lock at issue time).
- New `invoices` table: one row per invoiced ChargingSession (UNIQUE
  session_id), a globally-unique sequential `invoice_number`, the
  inclusive-GST tax split (amount_coins / taxable_value_inr / gst_rate_pct /
  gst_amount_inr / total_inr, all NUMERIC(12,2)), and immutable seller
  (tenant legal name + GSTIN) + line-item (energy_kwh, rate) snapshots so a
  later Tenant edit never rewrites an already-issued invoice.

India intra-state GST only (services/invoices.py) — CGST/SGST vs. IGST
splitting is explicitly out of scope; see the Invoice model docstring.

Idempotent create/add (same rationale as 0002-0010): a create_all-built
database may already have these from the model, so guard with
IF NOT EXISTS / information_schema checks throughout.

NOTE for the merge orchestrator: this revision is deliberately self-contained
(only tenants.* GST columns + the new invoices table) so it stays green
whether its down_revision chain to 0010_tariffs is preserved standalone or
later re-chained after a sibling migration developed in a parallel worktree.

Revision ID: 0012_gst_invoices
Revises: 0011_disputes
Create Date: 2026-07-12
"""
from alembic import op

revision = "0012_gst_invoices"
down_revision = "0011_disputes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. tenants: GST registration identity + sequential invoice-numbering state.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'gstin'
            ) THEN
                ALTER TABLE tenants ADD COLUMN gstin VARCHAR(15);
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'legal_name'
            ) THEN
                ALTER TABLE tenants ADD COLUMN legal_name VARCHAR(120);
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'invoice_prefix'
            ) THEN
                ALTER TABLE tenants ADD COLUMN invoice_prefix VARCHAR(12);
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'next_invoice_seq'
            ) THEN
                ALTER TABLE tenants ADD COLUMN next_invoice_seq INTEGER NOT NULL DEFAULT 1;
            END IF;
        END $$;
        """
    )

    # 2. invoices table. tenant_id/session_id/driver_user_id all CASCADE,
    #    mirroring charging_sessions' own FK choices (an Invoice is a child
    #    record of a session that already cascades with its tenant/user).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id                  SERIAL PRIMARY KEY,
            tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            session_id          INTEGER NOT NULL UNIQUE REFERENCES charging_sessions(id) ON DELETE CASCADE,
            driver_user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            invoice_number      VARCHAR(40) NOT NULL UNIQUE,
            issued_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            amount_coins        NUMERIC(12,2) NOT NULL,
            taxable_value_inr   NUMERIC(12,2) NOT NULL,
            gst_rate_pct        NUMERIC(12,2) NOT NULL,
            gst_amount_inr      NUMERIC(12,2) NOT NULL,
            total_inr           NUMERIC(12,2) NOT NULL,
            seller_legal_name   VARCHAR(120),
            seller_gstin        VARCHAR(15),
            energy_kwh          FLOAT NOT NULL,
            rate_coins_per_kwh  NUMERIC(12,2) NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_invoices_tenant_issued ON invoices (tenant_id, issued_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_invoices_tenant_issued")
    op.execute("DROP TABLE IF EXISTS invoices")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS next_invoice_seq")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS invoice_prefix")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS legal_name")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS gstin")
