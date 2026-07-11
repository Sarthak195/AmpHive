# AmpHive — Data Model

*Verified against `backend/database/models.py` on 2026-07-07.*

> **Source of truth is `models.py`, applied via Alembic** (adopted 2026-07-07).
> `init_db()` (`backend/database/db.py`) runs `alembic upgrade head` at startup
> (stamping pre-Alembic databases at the frozen baseline
> `backend/migrations/versions/0001_baseline.py` first). The old
> `create_all` + `_INPLACE_UPGRADES` path and the drifted reference
> `schema.sql`/`schema_v2.sql` files are **gone** — schema changes ship as
> Alembic revisions, and CI (`backend/tests/test_migrations.py`) fails when
> migrations and models disagree.

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
`coin_balance` **NUMERIC(12,2)** (Decimal, default 0.00) ·
`token_version` INTEGER (default 0; JWT-revocation epoch, embedded as the
`tv` claim and re-checked per request — revision `0003_token_version`,
2026-07-08) · `created_at`.
CHECK `ck_users_coin_balance_non_negative` (`coin_balance >= 0`, revision
`0002_wallet_non_negative`, 2026-07-07) — the DB-level backstop behind the
row-locked, clamped wallet debits.

### `gateways`
`id` **VARCHAR(50) PK** (caller-supplied MAC/UUID) · `tenant_id` → tenants
(CASCADE, **not null**) · `name` · `vpn_ip` unique · `status` (default `offline`)
· `firmware_version` VARCHAR(32) nullable (fw last reported in the `online`
status; rev `0006`, LWT never clobbers it) · `latitude`/`longitude` nullable
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
`mqtt_manager.py`)* · `last_telemetry_at` (nullable) · `coins_spent`
**NUMERIC(12,2)** (Decimal) · `status` (default `active`).

`last_telemetry_at` (added 2026-07-06) is the session reaper's staleness
signal — stamped by `MQTTManager._persist_telemetry` on every reading
attributed to the session; the reaper judges sessions by
`COALESCE(last_telemetry_at, started_at)`.

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

### `gateway_events`
Operational events/alarms feed for the CPO portal (firmware safety alarms
`THERMAL_CUTOFF` / `OVERCURRENT_CUTOFF` / `UNAUTHORIZED_ON` + OTA lifecycle
notices), fed by `services/mqtt_manager._handle_gateway_alarm` and read by
`GET /api/cpo/events` (ack'd via `POST /api/cpo/events/{id}/ack`). `id`
**BIGINT PK** · `tenant_id` → tenants (CASCADE) · `gateway_id` → gateways
VARCHAR(50) (CASCADE) · `plug_id` → plugs (SET NULL, nullable) · `event_type`
VARCHAR(48) · `severity` VARCHAR(16) (default `warning`;
`critical`|`warning`|`info`) · `detail` VARCHAR(255) (nullable) ·
`acknowledged` bool (default false) · `created_at` TIMESTAMPTZ. Composite
indexes on `(tenant_id, created_at)` and `(gateway_id, created_at)`. Added by
Alembic revision `0005_gateway_events` (2026-07-10).

### `notifications`
Per-user driver notification feed (session stopped/auto-stopped/reaped/safety
cutoff, low balance, charger offline, top-up credited), written by
`services/notifications.py` and read by `GET /api/notifications`. `id`
**BIGINT PK** · `user_id` → users (CASCADE) · `type` VARCHAR(32) (plain
string, not a PG enum — the set evolves) · `severity` VARCHAR(16) (default
`info`) · `title` VARCHAR(120) · `body` VARCHAR(500) · `plug_id` → plugs
(SET NULL, nullable) · `session_id` → charging_sessions (SET NULL, nullable)
· `read` bool (default false) · `created_at` TIMESTAMPTZ. Composite index on
`(user_id, created_at)`. Added by Alembic revision `0007_notifications`
(2026-07-11).

### `push_subscriptions`
Web-Push subscriptions, one row per browser/device that enabled push. `id`
SERIAL PK · `user_id` → users (CASCADE, indexed) · `endpoint` VARCHAR(1024)
**UNIQUE** (the push-service URL) · `p256dh` VARCHAR(255) · `auth`
VARCHAR(64) (client encryption keys) · `created_at` TIMESTAMPTZ. Pruned when
the push service reports the subscription gone (404/410) or the user
disables push. Added by `0007_notifications` (2026-07-11).

## 3. Relationships

```
tenants ─┬─< users ─┬─< charging_sessions >─┬─ plugs >── gateways >── tenants
         │          ├─< ledger_transactions │
         │          ├─< group_memberships >──┤
         │          ├─< notifications >── plugs / charging_sessions (nullable)
         │          └─< push_subscriptions   │
         ├─< gateways ─< plugs               │
         ├─< charging_sessions               │
         ├─< telemetry_readings >── plugs / charging_sessions (nullable)
         ├─< gateway_events >── gateways / plugs (nullable)
         └─< charger_groups ─┬─< plugs       │
                             └─< group_memberships
```

A plug is reachable by a user if it is **ungrouped** (`group_id IS NULL`, public
to everyone), in a **public** group, or in a **private** group the user has
joined via `access_code`.

## 4. Migrations (Alembic, since 2026-07-07)

- **`backend/migrations/versions/0001_baseline.py`** — frozen PostgreSQL DDL
  snapshot of the full 9-table schema at adoption (includes everything the
  retired `_INPLACE_UPGRADES` produced). Never edit or regenerate it. (The
  live schema is now **12 tables** — `gateway_events` arrived via
  `0005_gateway_events` 2026-07-10, and `notifications` +
  `push_subscriptions` via `0007_notifications` 2026-07-11.)
- **New schema change** = new revision: `alembic -c backend/alembic.ini
  revision --autogenerate -m "..."` (autogenerate needs a reachable database —
  use the CI postgres or the VM; dev boxes run no DB by policy).
- **Startup** applies `upgrade head` automatically; a database predating
  Alembic (built by the old `create_all` path) is detected (tables exist, no
  `alembic_version`) and stamped at the baseline first.
- The old `schema.sql`/`schema_v2.sql` reference files are deleted. Two
  constraints they described were never in the ORM and therefore do **not**
  exist in any real database (still true today — add as revisions if wanted):
  `UNIQUE (gateway_id, local_ip)` on `plugs`, and `UNIQUE (user_id, group_id)`
  on `group_memberships` (dedup enforced only in app logic).

## 5. Notes / gaps

- `charging_sessions.peak_power_w` is now populated from inbound telemetry
  (`backend/services/mqtt_manager.py` tracks the max observed wattage).
- Wallet credit/debit is **row-locked** (`SELECT ... FOR UPDATE` in the stop,
  verify, and webhook paths), so concurrent top-ups/debits no longer race.
- Time-series telemetry **is** now persisted to `telemetry_readings` via a
  buffered background batch-flush (`backend/services/telemetry_persistence.py`),
  decoupled from the live in-memory `TelemetryStore` (which drives the live
  Socket.io stream).
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
