"""plug_photos: driver-submitted charger photos, held for CPO approval.

Adds the PlugPhoto model + plug_photo_status enum (database/models.py) backing
POST /api/plugs/{plug_id}/photos (upload -> PENDING), the public read
(APPROVED only for anon), and the CPO moderation queue
(routers/cpo/_plug_photos.py: approve / reject).

Same verified-charger gate and denormalized tenant_id as plug_reviews (0039).
Bytes live in gs://amphive-plug-photos (services/photo_publish.py); this table
stores only metadata + the pending/approved/rejected lifecycle. object_name is
the GCS key kept so a reject can delete the stored bytes.

Idempotent create (same rationale as 0032_plug_reports / 0039_plug_reviews): a
create_all-built database already has the type/table from models.py, so every
statement is IF NOT EXISTS. Index names match the SQLAlchemy defaults so
test_migrations.py sees no drift.

Chained onto 0039_plug_reviews.

Revision ID: 0040_plug_photos
Revises: 0039_plug_reviews
Create Date: 2026-08-21
"""
from alembic import op

revision = "0040_plug_photos"
down_revision = "0039_plug_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'plug_photo_status') THEN
                CREATE TYPE plug_photo_status AS ENUM ('pending', 'approved', 'rejected');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plug_photos (
            id                   SERIAL PRIMARY KEY,
            plug_id              INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            uploaded_by_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            url                  VARCHAR(500) NOT NULL,
            object_name          VARCHAR(300) NOT NULL,
            status               plug_photo_status NOT NULL DEFAULT 'pending',
            created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at          TIMESTAMPTZ,
            reviewed_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_plug_photos_tenant_status "
        "ON plug_photos (tenant_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_plug_photos_plug_status "
        "ON plug_photos (plug_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plug_photos")
    op.execute("DROP TYPE IF EXISTS plug_photo_status")
