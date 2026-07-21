# AmpHive — Data Model

*Verified against `backend/database/models.py` on 2026-07-07; table list refreshed 2026-07-21.*

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
| `TransactionType` (`tx_type`) | `topup`, `session_debit`, `refund`, `cpo_topup` (2026-07-21, `0026_offline_topups` — first migration in this repo to `ALTER TYPE ... ADD VALUE` an existing enum rather than create one) |
| `ReservationStatus` (`reservation_status`) | `booked`, `cancelled`, `fulfilled`, `expired` (2026-07-12, `0016_reservations`) |

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
`plug_model` (default `tapo_p110`) · `unique_id` VARCHAR(128) nullable, indexed
(stable device identity from AmpHive-Agent discovery, e.g. `kasa:AA:BB:…`; NULL
for ESP-gateway / manually-provisioned plugs — the `(gateway_id, unique_id)`
discovery-upsert key) · `status` (default `offline`) · `current_power_w` float ·
`last_seen_at` · `created_at` · `group_id` → charger_groups (SET NULL, nullable).

The backend's **retained plug roster** (`amphive/gateways/{gw}/config`, see
[MQTT_CONTRACT.md](MQTT_CONTRACT.md)) is derived from these rows — one
`{plug_id, local_ip, max_current_a}` entry per plug on the gateway — and
republished whenever a plug is created/updated or the gateway reconnects.

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

`max_kwh` float (nullable) · `max_duration_seconds` int (nullable) — added
2026-07-12 (Alembic `0015_session_limits`): the stop conditions the session
was started with (user-chosen or the request defaults, 30 kWh / 4 h),
snapshotted from `SessionStartRequest`. Enforced backend-side by
`MQTTManager._maybe_auto_stop_on_limits` on the telemetry path (env
`AUTO_STOP_ON_LIMITS`) plus a reaper duration backstop; the firmware enforces
the same values locally as relay watchdogs. NULL = legacy pre-limit session
(never limit-auto-stopped). `max_kwh` is float, not NUMERIC — an energy
threshold, not money.

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

### `audit_logs`
CPO admin action audit trail (TD#26) — gateway/plug/group create-delete,
status changes, and access-code regeneration, previously unrecorded. Written
by `services/audit.py` (`record_audit` stages the row; `try_record_audit`
commits it non-fatally — a write failure is caught, logged, and rolled back
without breaking the admin action it documents), called from
`routers/cpo.py` after each action's own commit has landed. Read via
`GET /api/cpo/audit`. `id` **BIGINT PK** · `tenant_id` → tenants (CASCADE,
**not null**) · `actor_user_id` → users (**SET NULL**, nullable — the acting
user's account must stay deletable without erasing the audit trail) ·
`action` VARCHAR(64) (e.g. `gateway.create`, `plug.status_change`,
`access_code.regen`) · `target_type` VARCHAR(32) (e.g. `gateway`, `plug`,
`group`) · `target_id` VARCHAR(64) (nullable; stringified so one column fits
both string gateway ids and integer plug/group ids) · `detail` TEXT
(nullable, free-form) · `created_at` TIMESTAMPTZ. Index on
`(tenant_id, created_at)`. Added by Alembic revision `0007_audit_log`
(2026-07-12). Gateway/plug **delete** are pre-named in the action taxonomy
but have no CPO endpoint yet to hook — nothing to audit until they exist.

### `notifications`
Per-user driver notification feed (session stopped/auto-stopped/reaped/safety
cutoff, low balance, charger offline, top-up credited), written by
`services/notifications.py` and read by `GET /api/notifications`. `id`
**BIGINT PK** · `user_id` → users (CASCADE) · `type` VARCHAR(32) (plain
string, not a PG enum — the set evolves) · `severity` VARCHAR(16) (default
`info`) · `title` VARCHAR(120) · `body` VARCHAR(500) · `plug_id` → plugs
(SET NULL, nullable) · `session_id` → charging_sessions (SET NULL, nullable)
· `read` bool (default false) · `created_at` TIMESTAMPTZ. Composite index on
`(user_id, created_at)`. Added by Alembic revision `0008_notifications`
(2026-07-11, renumbered from 0007 at merge — `0007_audit_log` landed first).

### `push_subscriptions`
Web-Push subscriptions, one row per browser/device that enabled push. `id`
SERIAL PK · `user_id` → users (CASCADE, indexed) · `endpoint` VARCHAR(1024)
**UNIQUE** (the push-service URL) · `p256dh` VARCHAR(255) · `auth`
VARCHAR(64) (client encryption keys) · `created_at` TIMESTAMPTZ. Pruned when
the push service reports the subscription gone (404/410) or the user
disables push. Added by `0008_notifications` (2026-07-11).

### `reservations`
A driver's booked time window on a plug (2026-07-12, `0016_reservations`,
renumbered from 0014 at merge —
the private society/office use case; **free** in v1, no coin hold). `id`
SERIAL PK · `plug_id` → plugs (CASCADE) · `user_id` → users (CASCADE,
indexed) · `tenant_id` → tenants (CASCADE, indexed; **denormalized** from
plug → gateway → tenant, mirroring `charging_sessions`, so CPO-scoped
queries need no join) · `start_at`/`end_at` TIMESTAMPTZ (half-open
`[start_at, end_at)`, so back-to-back bookings can share an edge) ·
`status` `reservation_status` (default `booked`) · `session_id` →
charging_sessions (SET NULL, nullable — set when FULFILLED, i.e. the holder
started a session inside the window) · `created_at` TIMESTAMPTZ. Composite
index `idx_reservations_plug_start (plug_id, start_at)` — the
session-start gate / overlap check / per-plug schedule shape.

Lifecycle: `booked → cancelled | fulfilled | expired`. Expiry is **lazy**
(`services/reservations.py expire_lapsed_reservations`, run by every read
path + the session-start gate — no background sweep): a BOOKED row past
`start_at + RESERVATION_NO_SHOW_GRACE_MIN` (or past `end_at`) flips to
`expired`. Overlap exclusion among BOOKED rows is app-level, serialized by
`SELECT ... FOR UPDATE` on the plug row in the booking path (deliberately
no tstzrange EXCLUDE constraint — it would require btree_gist for a race
the plug lock already closes).

### `plug_watches`
One-shot "notify me when free" subscriptions: a driver looking at an
occupied/offline plug arms one via `POST /api/plugs/{id}/watch`; when the
plug next flips back to AVAILABLE (`finalize_charging_session`, or the CPO
maintenance-clear path) `services/plug_watch.py` sends each watcher a
`plug_available` notification (feed + Socket.io + Web Push) and **deletes**
the rows — transient state, not history. `id` SERIAL PK · `user_id` → users
(CASCADE) · `plug_id` → plugs (CASCADE) · `created_at` TIMESTAMPTZ.
**UNIQUE `(user_id, plug_id)`** (`uq_plug_watches_user_plug` — arming is
idempotent; its leading `user_id` also serves the per-user `watching` lookup
on the plug list/detail responses) + index on `plug_id`
(`idx_plug_watches_plug`, the per-plug fan-out read). Added by Alembic
revision `0014_plug_watches` (2026-07-12).

### Tables added since (doc-drift catch-up, flagged 2026-07-21)

The table-by-table sections above stopped at `0014_plug_watches`; the
following landed in later revisions and are documented here tersely rather
than re-flowing the whole chapter — see `backend/database/models.py` for the
full column list of each:

- **`payouts`** (`0009_payouts.py`) — a per-tenant settlement snapshot
  (`Payout`: gross/fee/net coins over `[period_start, period_end)`,
  `requested → paid | cancelled`). Backs `GET/POST /api/cpo/payouts`.
- **`tariffs`** (`0010_tariffs.py`) — a named coins-per-kWh pricing plan
  (`Tariff`) a tenant assigns to a plug/group/tenant-default, replacing the
  single global `COINS_PER_KWH` env rate. Backs `/api/cpo/tariffs*`.
- **`tariff_slots`** (`0018_pricing_v2_slots.py`) — time-of-day price
  refinements on a `Tariff` (`TariffSlot`: half-open minute-of-day window +
  weekday `days_mask`). Backs `/api/cpo/tariffs/{id}/slots*`.
- **`session_disputes`** (`0011_disputes.py`) — a driver-filed, CPO-resolved
  coins-only refund dispute on a finished session (`SessionDispute`;
  partial-unique "one OPEN dispute per session"). Backs
  `POST /api/sessions/{id}/dispute` + `GET /api/cpo/disputes` +
  `POST /api/cpo/disputes/{id}/resolve`.
- **`invoices`** (`0012_gst_invoices.py`) — an immutable, sequentially
  numbered GST tax invoice snapshot per session (`Invoice`; `UNIQUE
  session_id`, idempotent issuance). Backs `GET /api/sessions/{id}/invoice`
  and `GET /api/cpo/invoices*`.
- **`capacity_requests`** (`0020_capacity_requests.py`) — a one-shot "notify
  when the shared circuit has room" arm (`CapacityRequest`; `UNIQUE
  (user_id, plug_id)`, self-deletes on fan-out — mirrors `plug_watches`).
  Backs `POST /api/plugs/{id}/request-capacity`.
- **`queued_charges`** (`0022_queued_charge.py`) — a driver's auto-start
  request on a plug with no line power but a live gateway (`QueuedCharge`;
  `waiting → started | cancelled | expired | failed`, reaped by
  `services/session_reaper.py`). Backs `/api/sessions/queue*`.
- **`password_reset_tokens`** (`0023_password_reset_tokens.py`) — a
  single-use, SHA-256-digest-only "forgot password" token
  (`PasswordResetToken`). Backs `POST /api/auth/forgot-password` +
  `POST /api/auth/reset-password`.
- **`offline_topups`** (`0026_offline_topups.py`) — a CPO's cash top-up of a
  driver's coin wallet, funded from the tenant's own unsettled net earnings
  (`OfflineTopup`; `actor_user_id`/`driver_user_id` nullable + SET NULL, same
  survives-account-deletion rationale as `audit_logs`). Read back by
  `services/payouts.py tenant_earnings_summary`'s `available_pool_coins` so
  neither a top-up nor a later bank payout can draw the same earnings twice.
  Backs `POST/GET /api/cpo/topups`.

The live schema is now **24 tables** (up from the 15 documented in the
sections above), all applied via Alembic per §4 below.

## 3. Relationships

```
tenants ─┬─< users ─┬─< charging_sessions >─┬─ plugs >── gateways >── tenants
         │          ├─< ledger_transactions │
         │          ├─< group_memberships >──┤
         │          ├─< notifications >── plugs / charging_sessions (nullable)
         │          ├─< push_subscriptions   │
         │          ├─< plug_watches >───────┤
         │          └─< reservations >───────┤ (also → charging_sessions, nullable)
         ├─< gateways ─< plugs               │
         ├─< charging_sessions               │
         ├─< telemetry_readings >── plugs / charging_sessions (nullable)
         ├─< gateway_events >── gateways / plugs (nullable)
         ├─< audit_logs >── users (actor, nullable)
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
  live schema was **13 tables** as of `0008_notifications` — `gateway_events`
  arrived via `0005_gateway_events` (2026-07-10), `audit_logs` via
  `0007_audit_log` (2026-07-12), and `notifications` + `push_subscriptions`
  via `0008_notifications` (2026-07-11, renumbered from 0007 at merge); it's
  **24 tables** today — see "Tables added since" in §2 above for the rest.)
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
