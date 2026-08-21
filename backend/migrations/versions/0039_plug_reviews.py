"""plug_reviews: verified-charger star reviews of public plugs.

Adds the PlugReview model (database/models.py) backing POST
/api/plugs/{plug_id}/reviews and the discovery-map aggregate (avg_rating,
review_count on PublicPlugResponse/PlugResponse).

Unlike plug_reports (0032 — any driver who can SEE a plug may flag it), a review
requires a finished ChargingSession on that plug: the router enforces the
verified-charger gate, this table just stores the result. The UNIQUE index on
(driver_user_id, plug_id) makes one review per driver per plug — a re-submit is
an EDIT the router upserts on the IntegrityError (same idempotency as
0031_user_favorites' uq_user_favorites_user_plug), so the aggregate can't be
ballot-stuffed from one account.

Idempotent create (same rationale as 0032_plug_reports): a create_all-built
database already has this table/index from the current models.py, so every
statement is IF NOT EXISTS. Index/constraint names match the SQLAlchemy defaults
so test_migrations.py sees no drift.

Chained onto 0038_account_closure (the head at authoring time — verified by
reading versions/, per this repo's renumber-at-merge convention).

Revision ID: 0039_plug_reviews
Revises: 0038_account_closure
Create Date: 2026-08-21
"""
from alembic import op

revision = "0039_plug_reviews"
down_revision = "0038_account_closure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plug_reviews (
            id              SERIAL PRIMARY KEY,
            plug_id         INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            driver_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rating          SMALLINT NOT NULL,
            body            TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_plug_reviews_user_plug "
        "ON plug_reviews (driver_user_id, plug_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_plug_reviews_plug ON plug_reviews (plug_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plug_reviews")
