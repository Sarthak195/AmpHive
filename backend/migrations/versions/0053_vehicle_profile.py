"""vehicle_profile: per-driver insights assumptions and the monthly email (F8V).

Four nullable-or-defaulted columns on users:

- vehicle_profile JSONB: the driver's own km per kWh, petrol price per litre
  and petrol km per litre (services/insights.VehicleProfile dumped as JSON; the
  API validates it before it is written). NULL means every default, so every
  existing row keeps today's figures.
- monthly_insights_email BOOLEAN NOT NULL DEFAULT false: the opt-in for the
  once-a-month insights email. Off for every existing account.
- insights_email_month VARCHAR(7): the last "YYYY-MM" the email was decided for
  (claimed, sent or skipped as an empty month). The idempotency stamp.
- insights_email_failures SMALLINT NOT NULL DEFAULT 0: send failures for the
  month still pending, so a down mail server is retried a few times and then
  given up on for that month.

No enum change, so this is fully reversible. No index (the table is tiny and
the sweep query is limited), no GRANT (new columns on an existing table, same
reasoning as 0048). IF NOT EXISTS so a create_all()-built database migrates
cleanly (the 0038 idempotency idiom).

Revision ID: 0053_vehicle_profile
Revises: 0050_charge_routines
Create Date: 2026-11-19
"""
from alembic import op

revision = "0053_vehicle_profile"
down_revision = "0050_charge_routines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS vehicle_profile JSONB")
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "monthly_insights_email BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS insights_email_month VARCHAR(7)")
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "insights_email_failures SMALLINT NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS insights_email_failures")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS insights_email_month")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS monthly_insights_email")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS vehicle_profile")
