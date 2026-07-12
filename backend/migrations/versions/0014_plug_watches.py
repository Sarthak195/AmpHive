"""plug_watches: one-shot "notify me when free" plug watches.

A driver looking at an occupied/offline plug (typically a private
society/office group charger) arms a watch via `POST /api/plugs/{id}/watch`;
when the plug next flips back to AVAILABLE
(`services/session_lifecycle.py` `finalize_charging_session`, or the CPO
maintenance-clear path in `routers/cpo.py`) `services/plug_watch.py`
notifies each watcher through the existing notification pipeline
(persistent feed + Socket.io + Web Push) and deletes the rows — one-shot,
no standing subscription.

UNIQUE(user_id, plug_id) makes arming idempotent (the router treats the
IntegrityError from a double-tap race as already-watching); its leading
user_id also serves the per-user `watching` lookup the plug list/detail
responses make. `idx_plug_watches_plug` serves the per-plug fan-out read.
Both FKs CASCADE: a watch is transient state, not history.

NOTE on revision chaining: authored on a branch cut from origin/main while
`0013_auth_holds` was the head. Sibling "0014" revisions from parallel
branches are expected — the merge coordinator renumbers at merge time (the
same protocol that renumbered 0008_notifications and 0013_auth_holds). This
revision only creates the self-contained `plug_watches` table, so it stays
reorderable.

Idempotent create (same rationale as 0002 onward): a create_all-built
database may already have the table from the model, so guard with
IF NOT EXISTS.

Revision ID: 0014_plug_watches
Revises: 0013_auth_holds
Create Date: 2026-07-12
"""
from alembic import op

revision = "0014_plug_watches"
down_revision = "0013_auth_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plug_watches (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plug_id     INTEGER NOT NULL REFERENCES plugs(id) ON DELETE CASCADE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_plug_watches_user_plug UNIQUE (user_id, plug_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_plug_watches_plug ON plug_watches (plug_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plug_watches")
