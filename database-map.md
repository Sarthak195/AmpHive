# AmpHive — Database Map

> Verified against `backend/database/models.py` on 2026-06-20.
> Source of truth is the ORM (`models.py`), NOT the SQL files.

---

## 1. Engine Configuration

| Setting | Value |
|---------|-------|
| Driver | `asyncpg` (async PostgreSQL) |
| Pool size | 5 (base) + 10 (overflow) |
| Pre-ping | `True` (detects stale connections) |
| Session | `expire_on_commit=False` |
| Schema creation | `Base.metadata.create_all` on startup |

---

## 2. Enum Types

| Python Enum | DB Type Name | Values |
|-------------|-------------|--------|
| `UserRole` | `user_role` | `admin`, `cpo`, `driver` |
| `GatewayStatus` | `gateway_status` | `online`, `offline` |
| `PlugStatus` | `plug_status` | `available`, `occupied`, `offline`, `maintenance` |
| `SessionStatus` | `session_status` | `active`, `completed`, `paid`, `cancelled` |
| `TransactionType` | `tx_type` | `topup`, `session_debit`, `refund` |

---

## 3. Table Definitions

### `tenants`

**Purpose:** CPO (Charge Point Operator) organization. Top-level multi-tenancy entity.

| Column | Type | Constraints | Default | Notes |
|--------|------|------------|---------|-------|
| `id` | `INTEGER` | PK, auto-increment | — | |
| `name` | `VARCHAR(100)` | UNIQUE, NOT NULL | — | Organization name |
| `created_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |

**Relationships:** → users, gateways, charging_sessions, charger_groups (all cascade delete-orphan)

---

### `users`

**Purpose:** Driver, CPO, or admin accounts. Holds prepaid wallet balance.

| Column | Type | Constraints | Default | Notes |
|--------|------|------------|---------|-------|
| `id` | `INTEGER` | PK, auto-increment | — | |
| `tenant_id` | `INTEGER` | FK → tenants.id, NULLABLE | `NULL` | SET NULL on delete |
| `email` | `VARCHAR(255)` | UNIQUE, NOT NULL | — | Login identifier |
| `hashed_password` | `VARCHAR(255)` | NOT NULL | — | bcrypt hash |
| `full_name` | `VARCHAR(150)` | NOT NULL | — | Display name |
| `role` | `ENUM(user_role)` | NOT NULL | `'driver'` | `admin`/`cpo`/`driver` — never enforced |
| `coin_balance` | `FLOAT` | NOT NULL | `0.0` | Prepaid wallet (⚠ not atomic) |
| `created_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |

**Relationships:** → tenant, sessions, transactions, group_memberships

---

### `gateways`

**Purpose:** ESP32-S3 edge devices deployed at charging sites.

| Column | Type | Constraints | Default | Notes |
|--------|------|------------|---------|-------|
| `id` | `VARCHAR(50)` | **PK** (caller-supplied) | — | MAC address or hardware UUID |
| `tenant_id` | `INTEGER` | FK → tenants.id, NOT NULL | — | CASCADE on delete |
| `name` | `VARCHAR(100)` | NOT NULL | — | Human-readable name |
| `vpn_ip` | `VARCHAR(45)` | UNIQUE, NOT NULL | — | Tailscale/WireGuard overlay IP |
| `status` | `ENUM(gateway_status)` | NOT NULL | `'offline'` | |
| `last_seen_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | `onupdate=datetime.now` |
| `created_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |

**Relationships:** → tenant, plugs (cascade delete-orphan)

> ⚠ Gateway `id` is a string PK (not auto-increment) — the caller supplies it during registration.

---

### `plugs`

**Purpose:** Individual smart plugs registered on gateways.

| Column | Type | Constraints | Default | Notes |
|--------|------|------------|---------|-------|
| `id` | `INTEGER` | PK, auto-increment | — | Printed on physical outlet as "Plug ID" |
| `gateway_id` | `VARCHAR(50)` | FK → gateways.id, NOT NULL | — | CASCADE on delete |
| `name` | `VARCHAR(100)` | NOT NULL | — | Human-readable name |
| `local_ip` | `VARCHAR(45)` | NOT NULL | — | VLAN 20 IP address |
| `plug_model` | `VARCHAR(50)` | NOT NULL | `'tapo_p110'` | Hardware model |
| `status` | `ENUM(plug_status)` | NOT NULL | `'offline'` | Runtime state |
| `current_power_w` | `FLOAT` | NOT NULL | `0.0` | Last known power draw |
| `last_seen_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |
| `created_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |
| `group_id` | `INTEGER` | FK → charger_groups.id, NULLABLE | `NULL` | SET NULL on delete. NULL = ungrouped/public |

**Relationships:** → gateway, sessions (cascade delete-orphan), charger_group

> ⚠ Missing: `UNIQUE(gateway_id, local_ip)` constraint defined in schema.sql but absent from ORM.

---

### `charging_sessions`

**Purpose:** Records of charging events linking a user to a plug with energy/cost tracking.

| Column | Type | Constraints | Default | Notes |
|--------|------|------------|---------|-------|
| `id` | `INTEGER` | PK, auto-increment | — | |
| `tenant_id` | `INTEGER` | FK → tenants.id, NOT NULL | — | CASCADE on delete |
| `user_id` | `INTEGER` | FK → users.id, NOT NULL | — | CASCADE on delete |
| `plug_id` | `INTEGER` | FK → plugs.id, NOT NULL | — | CASCADE on delete |
| `started_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |
| `ended_at` | `TIMESTAMP WITH TZ` | NULLABLE | `NULL` | Set on session stop |
| `energy_kwh` | `FLOAT` | NOT NULL | `0.0` | Total energy consumed |
| `peak_power_w` | `FLOAT` | NOT NULL | `0.0` | ⚠ NEVER POPULATED |
| `coins_spent` | `FLOAT` | NOT NULL | `0.0` | Total cost deducted |
| `status` | `ENUM(session_status)` | NOT NULL | `'active'` | |

**Relationships:** → tenant, user, plug, ledger_transactions

---

### `ledger_transactions`

**Purpose:** Double-entry wallet audit trail. Every wallet change gets a ledger entry.

| Column | Type | Constraints | Default | Notes |
|--------|------|------------|---------|-------|
| `id` | `INTEGER` | PK, auto-increment | — | |
| `user_id` | `INTEGER` | FK → users.id, NOT NULL | — | CASCADE on delete |
| `session_id` | `INTEGER` | FK → charging_sessions.id, NULLABLE | `NULL` | SET NULL on delete. NULL for top-ups |
| `amount` | `FLOAT` | NOT NULL | — | Signed: `+` = topup, `-` = debit |
| `transaction_type` | `ENUM(tx_type)` | NOT NULL | — | `topup`, `session_debit`, `refund` |
| `description` | `VARCHAR(255)` | NULLABLE | `NULL` | Human-readable description |
| `balance_after` | `FLOAT` | NOT NULL | — | Snapshot of balance after transaction |
| `created_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |

**Relationships:** → user, session (optional)

---

### `charger_groups`

**Purpose:** Named groups of plugs with optional access control (public vs. private with access code).

| Column | Type | Constraints | Default | Notes |
|--------|------|------------|---------|-------|
| `id` | `INTEGER` | PK, auto-increment | — | |
| `tenant_id` | `INTEGER` | FK → tenants.id, NOT NULL | — | CASCADE on delete |
| `name` | `VARCHAR(100)` | NOT NULL | — | Group display name |
| `is_public` | `BOOLEAN` | NOT NULL | `False` | True = open to all users |
| `access_code` | `VARCHAR(20)` | UNIQUE, NULLABLE | `NULL` | e.g., "SUNRISE2024". NULL for public groups |
| `created_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |

**Relationships:** → tenant, plugs, memberships (cascade delete-orphan)

---

### `group_memberships`

**Purpose:** Join table tracking which users have joined which private groups.

| Column | Type | Constraints | Default | Notes |
|--------|------|------------|---------|-------|
| `id` | `INTEGER` | PK, auto-increment | — | |
| `user_id` | `INTEGER` | FK → users.id, NOT NULL | — | CASCADE on delete |
| `group_id` | `INTEGER` | FK → charger_groups.id, NOT NULL | — | CASCADE on delete |
| `joined_at` | `TIMESTAMP WITH TZ` | NOT NULL | `CURRENT_TIMESTAMP` | |

**Relationships:** → user, charger_group

> ⚠ Missing: `UNIQUE(user_id, group_id)` constraint defined in schema_v2.sql but absent from ORM.
> Deduplication is enforced only in application logic (`join_group` endpoint checks existing membership).

---

## 4. Entity Relationship Diagram

```
tenants ─┬──< users ─┬──< charging_sessions >──┬── plugs >── gateways >── tenants
         │           ├──< ledger_transactions    │
         │           └──< group_memberships >────┤
         │                                       │
         ├──< gateways ──< plugs                 │
         │                    │                  │
         ├──< charging_sessions                  │
         │                                       │
         └──< charger_groups ─┬──< plugs         │
                              └──< group_memberships
```

### Access Control Logic

A user can see/use a plug if:
1. `plug.group_id IS NULL` (ungrouped / legacy — visible to everyone)
2. `plug.group_id` → `charger_group.is_public = TRUE`
3. `plug.group_id` → exists in `group_memberships` with matching `user_id`

---

## 5. Schema Files (Reference Only)

| File | Purpose | Executed by App? |
|------|---------|:----------------:|
| `database/schema.sql` | Original full schema (6 tables + 5 enums) | **No** |
| `database/schema_v2.sql` | Migration delta: charger_groups + memberships + plug.group_id | **No** |
| `database/models.py` | SQLAlchemy ORM models (8 tables + 5 enums) | **Yes** (create_all) |

### Missing from ORM (defined in SQL files)

| Constraint/Index | Defined In | Impact |
|-----------------|-----------|--------|
| `UNIQUE(gateway_id, local_ip)` on plugs | schema.sql | Duplicate plug IPs possible on same gateway |
| `UNIQUE(user_id, group_id)` on group_memberships | schema_v2.sql | Duplicate memberships possible (app-level check only) |
| `CREATE INDEX idx_*` (all indexes) | schema.sql, schema_v2.sql | No performance indexes on queries |

---

## 6. Data Integrity Notes

| Issue | Severity | Location |
|-------|:--------:|----------|
| `coin_balance` not row-locked on update | 🔴 High | `main.py:stop_charging_session`, `main.py:verify_payment` |
| `peak_power_w` never populated | 🟡 Low | `models.py:ChargingSession` |
| No time-series telemetry table | 🟡 Medium | Live data is in-memory only (`TelemetryStore`) |
| Registration always creates `role=driver` | 🟡 Medium | No API to create admin/CPO users |
| Group membership dedup is app-only | 🟡 Low | No DB unique constraint |
