"""plug_hours: weekly opening hours, max session length, amenity tags and an
access note per plug (services/plug_hours.py).

1. plugs.opening_hours: nullable JSONB list of {days, start, end} windows in
   the tenant's local wall-clock (TariffSlot semantics). NULL = open 24x7, so
   every existing plug keeps its current behaviour.
2. plugs.max_session_minutes: nullable INTEGER; NULL = no per-plug cap.
3. plugs.amenities: nullable JSONB list of allowlisted tags.
4. plugs.access_note: nullable VARCHAR(280), plain text for drivers.

All four are nullable with no default, so the ALTERs are metadata-only and
need no backfill. No grant is needed (columns on an existing table).

Idempotent (same rationale as 0026/0037/0047): a create_all-built database
already has these columns from models.py, so every DDL is guarded with
IF NOT EXISTS.

Revision ID: 0049_plug_hours
Revises: 0047_promo_codes
Create Date: 2026-11-19
"""
from alembic import op

revision = "0049_plug_hours"
down_revision = "0047_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE plugs ADD COLUMN IF NOT EXISTS opening_hours JSONB")
    op.execute("ALTER TABLE plugs ADD COLUMN IF NOT EXISTS max_session_minutes INTEGER")
    op.execute("ALTER TABLE plugs ADD COLUMN IF NOT EXISTS amenities JSONB")
    op.execute("ALTER TABLE plugs ADD COLUMN IF NOT EXISTS access_note VARCHAR(280)")


def downgrade() -> None:
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS access_note")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS amenities")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS max_session_minutes")
    op.execute("ALTER TABLE plugs DROP COLUMN IF EXISTS opening_hours")
