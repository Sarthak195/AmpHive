"""user_favorites: driver-armed "favorite this charger" star.

Backs the discovery bundle's Favorites feature: a driver stars a plug from
PlugCard / the map popup; `is_favorite` on PlugResponse reflects it back on
every list/detail read. Unlike PlugWatch (a one-shot, self-deleting "notify
me when free" arm), a favorite is a STANDING preference — nothing ever
deletes the row except the driver un-starring it or one of the two FKs
CASCADE-ing it away.

UNIQUE(user_id, plug_id) makes starring idempotent (the router treats the
IntegrityError from a double-tap race as already-favorited, same pattern as
PlugWatch's uq_plug_watches_user_plug). idx_user_favorites_plug serves a
future per-plug "N drivers favorited this" read (not used yet, but the same
shape as idx_plug_watches_plug — cheap to add now, expensive to backfill
later). Both FKs CASCADE: a favorite is meaningless without its user or its
plug, same as a watch.

Idempotent create (same rationale as 0014_plug_watches / 0026_offline_topups):
a create_all-built database may already have this table from the model.

Revision ID: 0031_user_favorites
Revises: 0030_plug_specs
Create Date: 2026-08-02
"""
from alembic import op

revision = "0031_user_favorites"
down_revision = "0030_plug_specs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_favorites (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plug_id     INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_favorites_user_plug "
        "ON user_favorites (user_id, plug_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_favorites_plug ON user_favorites (plug_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_favorites")
