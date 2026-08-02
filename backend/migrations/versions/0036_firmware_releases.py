"""firmware_releases: admin-registered OTA version catalog (feat/ota-version-picker).

New table backing the "dropdown of versions instead of hand-pasted URL" CPO
OTA flow:

- `version` VARCHAR(32) UNIQUE NOT NULL — e.g. "2.3.0-direct"
  (`firmware/CMakeLists.txt` PROJECT_VER format).
- `url` VARCHAR(512) NOT NULL — the already-published image location (today:
  `gs://amphive-fw`, see docs/FIRMWARE.md §7). This table is a catalog, not
  storage — no binary upload here (noted as follow-up).
- `notes` VARCHAR(500), nullable — free-text changelog line for the picker.
- `is_active` BOOLEAN NOT NULL DEFAULT TRUE — soft-deactivate (kept for
  admin audit / past-OTA detail strings) instead of a hard delete.
- `created_at` TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP.

Idempotent create (same rationale as 0029_gateway_logs): a create_all()-built
database may already have this table from models.py. Index names match the
SQLAlchemy defaults (unique=True String columns get an inline UNIQUE
constraint, not a separately-named index — same convention as
0023_password_reset_tokens) because test_migrations.py diffs the migrated
schema against the models.

Revision ID: 0036_firmware_releases
Revises: 0035_offline_consumption
Create Date: 2026-08-02

(Authored chained off 0034 while 0035 lived on a sibling branch; re-chained
onto 0035_offline_consumption at merge time per docs/MAINTAINER_GUIDE.md §7
"Parallel-branch migration renumbering".)
"""
from alembic import op

revision = "0036_firmware_releases"
down_revision = "0035_offline_consumption"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS firmware_releases (
            id            SERIAL PRIMARY KEY,
            version       VARCHAR(32) NOT NULL UNIQUE,
            url           VARCHAR(512) NOT NULL,
            notes         VARCHAR(500),
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_firmware_releases_active "
        "ON firmware_releases (is_active)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS firmware_releases")
