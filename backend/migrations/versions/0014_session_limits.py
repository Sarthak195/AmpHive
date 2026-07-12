"""session_limits: user-set stop conditions persisted on ChargingSession.

Adds two nullable columns to `charging_sessions`, snapshotted verbatim from
the SessionStartRequest at `POST /api/sessions/start`:

- `max_kwh` (DOUBLE PRECISION — an energy threshold, matching `energy_kwh`'s
  Float, not money)
- `max_duration_seconds` (INTEGER)

These are the same limits the backend already forwarded to the gateway in
the MQTT ON payload (where the firmware enforces them locally as relay
watchdogs) — but the firmware publishes NO alarm on those cutoffs, so the
backend session lingered ACTIVE until the staleness reaper. Persisting them
lets the backend MIRROR the enforcement on the telemetry path
(`MQTTManager._maybe_auto_stop_on_limits`, reasons "auto-stopped: energy
limit reached" / "auto-stopped: time limit reached", env toggle
`AUTO_STOP_ON_LIMITS`) plus a duration backstop in the session reaper.

NULL means a legacy session started before these columns existed — those are
never limit-auto-stopped (staleness reaping and balance-exhaustion auto-stop
still apply, exactly as before). New sessions always persist both values,
including the schema defaults (30 kWh / 14400 s).

NOTE on revision chaining: authored on a parallel branch cut from
`origin/main` when `0013_auth_holds` was the head; a sibling branch may also
create an 0014 chained on 0013 — the merge coordinator renumbers at merge
time (the established parallel-agent protocol). This revision only touches
these two columns, so it stays self-contained and reorderable.

Idempotent add (same rationale as 0002 onward): a create_all-built database
may already have these columns from the model, so guard with an
information_schema check.

Revision ID: 0014_session_limits
Revises: 0013_auth_holds
Create Date: 2026-07-12
"""
from alembic import op

revision = "0014_session_limits"
down_revision = "0013_auth_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # charging_sessions.max_kwh -- the energy stop condition the session was
    # started with. Nullable, no default (an explicit NULL is the correct
    # "legacy, no persisted limit" value -- new sessions always write one).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'max_kwh'
            ) THEN
                ALTER TABLE charging_sessions ADD COLUMN max_kwh DOUBLE PRECISION;
            END IF;
        END $$;
        """
    )
    # charging_sessions.max_duration_seconds -- the time stop condition.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'charging_sessions' AND column_name = 'max_duration_seconds'
            ) THEN
                ALTER TABLE charging_sessions ADD COLUMN max_duration_seconds INTEGER;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS max_kwh")
    op.execute("ALTER TABLE charging_sessions DROP COLUMN IF EXISTS max_duration_seconds")
