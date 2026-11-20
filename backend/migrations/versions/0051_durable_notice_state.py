"""durable_notice_state: notice dedupe state that survives a backend restart.

charging_sessions (wave-4 lane DUR, docs/TODO.md F1 + C7 known limits):
  full_notified_at           the "looks fully charged" notice was sent (F1)
  nearly_done_notified_at    the nearly_done notice was sent (C7)
  power_dropped_notified_at  the power_dropped notice was sent (C7)
Each is claimed once per session with UPDATE ... WHERE col IS NULL, right
after the in-memory detector fires (a state transition, never per frame).

gateways (F3 operator alerts):
  offline_notified_at     an operator "Gateway offline" alert is outstanding
                          (cleared by the "back online" follow-up)
  last_operator_alert_at  when the last offline alert went out (the per-gateway
                          cooldown, and the start of a pending "still offline"
                          re-check, re-armed at backend startup)

All nullable timestamptz with no default, so every existing row reads "never
sent". No index (looked up by primary key only; the startup re-check scan
reads the small gateways table) and no GRANT (new columns on existing tables).
IF NOT EXISTS so a create_all()-built database migrates cleanly (the 0044
idiom). No enum change, fully reversible.

Revision ID: 0051_durable_notice_state
Revises: 0050_charge_routines
Create Date: 2026-11-19
"""
from alembic import op

revision = "0051_durable_notice_state"
down_revision = "0050_charge_routines"
branch_labels = None
depends_on = None

_SESSION_COLS = ("full_notified_at", "nearly_done_notified_at", "power_dropped_notified_at")
_GATEWAY_COLS = ("offline_notified_at", "last_operator_alert_at")


def upgrade() -> None:
    for col in _SESSION_COLS:
        op.execute(f"ALTER TABLE charging_sessions ADD COLUMN IF NOT EXISTS {col} TIMESTAMP WITH TIME ZONE")
    for col in _GATEWAY_COLS:
        op.execute(f"ALTER TABLE gateways ADD COLUMN IF NOT EXISTS {col} TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    for col in _GATEWAY_COLS:
        op.execute(f"ALTER TABLE gateways DROP COLUMN IF EXISTS {col}")
    for col in _SESSION_COLS:
        op.execute(f"ALTER TABLE charging_sessions DROP COLUMN IF EXISTS {col}")
