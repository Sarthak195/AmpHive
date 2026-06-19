-- AmpHive Database Migration v2: Charger Groups & Access Code Model
-- =================================================================
-- Run this migration on the Cloud SQL instance AFTER the original schema.sql.
-- This adds support for public vs private (access-code-gated) charger groups.
--
-- Usage (via SSH to GCP VM):
--   gcloud compute ssh amphive-vm-in --zone=asia-south1-a
--   docker exec -i amphive-postgres psql -U postgres -d amphive < schema_v2.sql
--
-- Or via Cloud SQL proxy:
--   psql -h 127.0.0.1 -U postgres -d amphive -f schema_v2.sql

-- 1. Charger Groups Table
-- A CPO creates groups to organize plugs. Public groups are visible to all
-- registered users. Private groups require an access code to join.
CREATE TABLE IF NOT EXISTS charger_groups (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    -- is_public: true = any registered user can see and use plugs in this group.
    -- false = user must join via access_code first.
    is_public BOOLEAN DEFAULT false NOT NULL,
    -- access_code: null for public groups. For private groups, this is the
    -- shareable code that CPOs distribute to authorized users (e.g. residents).
    access_code VARCHAR(20) UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- 2. Group Memberships Table (many-to-many: users <-> private groups)
-- When a user submits a valid access code, a membership row is created.
-- Public group access does not require a membership row.
CREATE TABLE IF NOT EXISTS group_memberships (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES charger_groups(id) ON DELETE CASCADE,
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    -- Prevent duplicate memberships
    CONSTRAINT unique_user_group UNIQUE (user_id, group_id)
);

-- 3. Add group_id column to plugs table
-- Links each plug to a charger group. Plugs without a group (NULL) are
-- treated as ungrouped/legacy and are visible to all users.
ALTER TABLE plugs
    ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES charger_groups(id) ON DELETE SET NULL;

-- 4. Performance indexes
CREATE INDEX IF NOT EXISTS idx_charger_groups_tenant ON charger_groups(tenant_id);
CREATE INDEX IF NOT EXISTS idx_charger_groups_access_code ON charger_groups(access_code);
CREATE INDEX IF NOT EXISTS idx_group_memberships_user ON group_memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_group_memberships_group ON group_memberships(group_id);
CREATE INDEX IF NOT EXISTS idx_plugs_group ON plugs(group_id);
