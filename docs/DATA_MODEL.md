# AmpHive — Data Model

*Verified against `backend/database/models.py`, `schema.sql`, `schema_v2.sql` on 2026-07-06.*

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
`coin_balance` **NUMERIC(12,2)** (Decimal, default 0.00) · `created_at`.

### `gateways`
`id` **VARCHAR(50) PK** (caller-supplied MAC/UUID) · `tenant_id` → tenants
(CASCADE, **not null**) · `name` · `vpn_ip` unique · `status` (default `offline`)
· `last_seen_at` · `created_at`. Owns plugs.

`last_seen_at` is the **liveness marker**: written only by the MQTT handlers
(status connect/LWT, plus a telemetry-driven refresh throttled to once per
gateway per minute) and read by the session-start liveness gate
(`gateway_is_live`: status ONLINE **and** seen within
`GATEWAY_LIVENESS_WINDOW_SEC`, default 120 s). The old `onupdate=now` hook was
removed 2026-07-06 — an unrelated row edit must not make a dead gateway look
freshly seen.

### `plugs`
`id` PK · `gateway_id` → gateways (CASCADE) · `name` · `local_ip` ·
`plug_model` (default `tapo_p110`) · `status` (default `offline`) ·
`current_power_w` float · `last_seen_at` · `created_at` ·
`group_id` → charger_groups (SET NULL, nullable).

### `charging_sessions`
`id` PK · `tenant_id` → tenants (CASCADE) · `user_id` → users (CASCADE) ·
`plug_id` → plugs (CASCADE) · `started_at` · `ended_at` (nullable) ·
`energy_kwh` float · `peak_power_w` float *(populated from inbound telemetry in
`mqtt_manager.py`)* · `coins_spent` **NUMERIC(12,2)** (Decimal) · `status`
(default `active`).

### `ledger_transactions`
Double-entry-style wallet audit. `id` PK · `user_id` → users (CASCADE) ·
`session_id` → charging_sessions (SET NULL, nullable) · `amount` **NUMERIC(12,2)**
(Decimal, signed: `+` topups, `-` debits) · `transaction_type` · `description`
(nullable) · `balance_after` **NUMERIC(12,2)** · `razorpay_payment_id`
(unique, nullable) · `created_at`.

> **Money columns are `NUMERIC(12,2)` (Decimal), not float** — moved off `float`
> to eliminate rounding drift; all wallet math routes through
> `services/money.to_money`. `energy_kwh` / `peak_power_w` / `current_power_w`
> stay `float` (physical measurements, not currency).

### `charger_groups`
`id` PK · `tenant_id` → tenants (CASCADE) · `name` · `is_public` bool (default
false) · `access_code` VARCHAR(20) unique (nullable) · `created_at`.

### `group_memberships`
Join table. `id` PK · `user_id` → users (CASCADE) · `group_id` → charger_groups
(CASCADE) · `joined_at`.

### `telemetry_readings`
Append-only time-series of raw plug samples (~1 row/plug/~15s). `id` **BIGINT PK**
· `tenant_id` → tenants (CASCADE, **denormalized** so CPO charts filter without a
plug→gateway→tenant join) · `plug_id` → plugs (CASCADE) · `session_id` →
charging_sessions (**SET NULL, nullable** — telemetry can arrive with no active
session, and deleting a session must not erase audit history) · `recorded_at`
(stamped in the MQTT handler) · `power_w` · `energy_kwh` *(session-relative kWh, as
reported by firmware — not the plug's lifetime meter; see
[MQTT_CONTRACT.md](MQTT_CONTRACT.md))* · `voltage_v` · `current_a` · `status`
VARCHAR(20) (raw firmware signal, nullable). Composite indexes on `(plug_id, recorded_at)`,
`(session_id, recorded_at)`, `(tenant_id, recorded_at)` — declared in `models.py`
(unlike other tables' indexes) so `create_all` actually creates them. Written by
the buffered batch-flush service `backend/services/telemetry_persistence.py`; read
by `GET /api/cpo/analytics/telemetry` via `date_trunc` aggregation.

## 3. Relationships

```
tenants ─┬─< users ─┬─< charging_sessions >─┬─ plugs >── gateways >── tenants
         │          ├─< ledger_transactions │
         │          └─< group_memberships >──┤
         ├─< gateways ─< plugs               │
         ├─< charging_sessions               │
         ├─< telemetry_readings >── plugs / charging_sessions (nullable)
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
- **`models.py`** = the union of all 9 tables and the authoritative runtime schema.
- **`telemetry_readings`** is the exception to the drift pattern: its three
  composite indexes are declared in `models.py` (`__table_args__`), so
  `create_all` creates them at runtime. `schema.sql` mirrors the same DDL for
  reference parity.

Constraints/indexes present in the SQL files but **missing from the ORM** (so a
DB created by the running app will not have them):

| Missing in ORM | Defined in |
|----------------|-----------|
| `UNIQUE (gateway_id, local_ip)` on `plugs` | schema.sql |
| `UNIQUE (user_id, group_id)` on `group_memberships` (dedup is enforced only in app logic) | schema_v2.sql |
| All performance `CREATE INDEX`es | schema.sql / schema_v2.sql |

## 5. Notes / gaps

- `charging_sessions.peak_power_w` is now populated from inbound telemetry
  (`backend/services/mqtt_manager.py` tracks the max observed wattage).
- Wallet credit/debit is **row-locked** (`SELECT ... FOR UPDATE` in the stop,
  verify, and webhook paths), so concurrent top-ups/debits no longer race.
- Time-series telemetry **is** now persisted to `telemetry_readings` via a
  buffered background batch-flush (`backend/services/telemetry_persistence.py`),
  decoupled from the live in-memory `TelemetryStore` (which still drives SSE).
  This uses **plain Postgres** + `date_trunc` aggregation; the spec's TimescaleDB
  (hypertables, native retention, continuous aggregates) is *not* used and is a
  possible future upgrade. Retention is an opt-in periodic prune gated by
  `TELEMETRY_RETENTION_DAYS` (default `0` = keep all). See tunables:
  `TELEMETRY_FLUSH_INTERVAL_SEC`, `TELEMETRY_BUFFER_MAX`,
  `TELEMETRY_PRUNE_EVERY_N_FLUSHES`.
- Self-registration always creates `role=driver`; a driver becomes a `cpo`
  through `POST /api/cpo/setup`. `role` **is** enforced for authorization on the
  `/api/cpo/*` routes via `require_role(...)` (`backend/services/rbac.py`).
- `backend/seed.py` populates sample tenants/users/gateways/plugs/sessions for
  development (default password `password123`) — see [DEPLOYMENT.md](DEPLOYMENT.md#database-seeding).
