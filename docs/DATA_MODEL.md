# AmpHive — Data Model

*Verified against `backend/database/models.py`, `schema.sql`, `schema_v2.sql` on 2026-06-20.*

> **Source of truth at runtime is `models.py`.** `init_db()` (`backend/database/db.py`)
> calls SQLAlchemy `Base.metadata.create_all` on startup — the `.sql` files are
> **not executed by the app**; they are reference/manual-migration artifacts.
> This matters because the SQL files carry constraints and indexes the ORM omits
> (see [§4](#4-schema-vs-orm-drift)).

ORM: SQLAlchemy 2.0 `DeclarativeBase` with `mapped_column`. Enums use
`values_callable`, so the DB stores the lowercase string values.

---

## 1. Enums

| Enum (DB type) | Values |
|----------------|--------|
| `UserRole` (`user_role`) | `admin`, `cpo`, `driver` |
| `GatewayStatus` (`gateway_status`) | `online`, `offline` |
| `PlugStatus` (`plug_status`) | `available`, `occupied`, `offline`, `maintenance` |
| `SessionStatus` (`session_status`) | `active`, `completed`, `paid`, `cancelled` |
| `TransactionType` (`tx_type`) | `topup`, `session_debit`, `refund` |

## 2. Tables

### `tenants`
CPO/organization. `id` PK · `name` unique · `created_at`.
Owns users, gateways, sessions, charger_groups (cascade delete-orphan).

### `users`
`id` PK · `tenant_id` → tenants (SET NULL, nullable) · `email` unique ·
`hashed_password` · `full_name` · `role` (default `driver`) ·
`coin_balance` float (default 0.0) · `created_at`.

### `gateways`
`id` **VARCHAR(50) PK** (caller-supplied MAC/UUID) · `tenant_id` → tenants
(CASCADE, **not null**) · `name` · `vpn_ip` unique · `status` (default `offline`)
· `last_seen_at` (`onupdate=now`) · `created_at`. Owns plugs.

### `plugs`
`id` PK · `gateway_id` → gateways (CASCADE) · `name` · `local_ip` ·
`plug_model` (default `tapo_p110`) · `status` (default `offline`) ·
`current_power_w` float · `last_seen_at` · `created_at` ·
`group_id` → charger_groups (SET NULL, nullable).

### `charging_sessions`
`id` PK · `tenant_id` → tenants (CASCADE) · `user_id` → users (CASCADE) ·
`plug_id` → plugs (CASCADE) · `started_at` · `ended_at` (nullable) ·
`energy_kwh` float · `peak_power_w` float *(never populated — see status doc)* ·
`coins_spent` float · `status` (default `active`).

### `ledger_transactions`
Double-entry-style wallet audit. `id` PK · `user_id` → users (CASCADE) ·
`session_id` → charging_sessions (SET NULL, nullable) · `amount` float (signed:
`+` topups, `-` debits) · `transaction_type` · `description` (nullable) ·
`balance_after` float · `created_at`.

### `charger_groups`
`id` PK · `tenant_id` → tenants (CASCADE) · `name` · `is_public` bool (default
false) · `access_code` VARCHAR(20) unique (nullable) · `created_at`.

### `group_memberships`
Join table. `id` PK · `user_id` → users (CASCADE) · `group_id` → charger_groups
(CASCADE) · `joined_at`.

## 3. Relationships

```
tenants ─┬─< users ─┬─< charging_sessions >─┬─ plugs >── gateways >── tenants
         │          ├─< ledger_transactions │
         │          └─< group_memberships >──┤
         ├─< gateways ─< plugs               │
         ├─< charging_sessions               │
         └─< charger_groups ─┬─< plugs       │
                             └─< group_memberships
```

A plug is reachable by a user if it is **ungrouped** (`group_id IS NULL`, public
to everyone), in a **public** group, or in a **private** group the user has
joined via `access_code`.

## 4. Schema-vs-ORM drift

- **`schema.sql`** = original full schema: tenants, users, gateways, plugs,
  charging_sessions, ledger_transactions + the 5 enum types. It does **not**
  include `charger_groups`, `group_memberships`, or `plugs.group_id`.
- **`schema_v2.sql`** = a *migration delta* only: `CREATE TABLE IF NOT EXISTS
  charger_groups / group_memberships` and `ALTER TABLE plugs ADD COLUMN group_id`.
  Apply **after** `schema.sql`.
- **`models.py`** = the union of all 8 tables and the authoritative runtime schema.

Constraints/indexes present in the SQL files but **missing from the ORM** (so a
DB created by the running app will not have them):

| Missing in ORM | Defined in |
|----------------|-----------|
| `UNIQUE (gateway_id, local_ip)` on `plugs` | schema.sql |
| `UNIQUE (user_id, group_id)` on `group_memberships` (dedup is enforced only in app logic) | schema_v2.sql |
| All performance `CREATE INDEX`es | schema.sql / schema_v2.sql |

## 5. Notes / gaps

- `charging_sessions.peak_power_w` exists but is never written by any endpoint.
- Wallet credit/debit (`coin_balance += …`) is **not row-locked or atomic**,
  despite a "thread-safe" comment — concurrent top-ups/debits can race.
- There is **no time-series/telemetry table.** Live telemetry is in-memory only
  (`TelemetryStore`); the product spec's TimescaleDB is not implemented.
- Registration always creates `role=driver`; there is no API path to create
  `admin`/`cpo` users, and `role` is never checked for authorization.
</content>
