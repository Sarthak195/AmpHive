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
| `UserRole` (`user_role`) | `admin`, `cpo`, `driver`, `cpo_viewer` (F10, `0045_cpo_viewer_role`, 2026-11-17-w2, deployed 2026-11-19: `ALTER TYPE ... ADD VALUE`, cannot be dropped on downgrade) |
| `GatewayStatus` (`gateway_status`) | `online`, `offline` |
| `PlugStatus` (`plug_status`) | `available`, `occupied`, `offline`, `maintenance` |
| `SessionStatus` (`session_status`) | `active`, `completed`, `paid`, `cancelled` |
| `TransactionType` (`tx_type`) | `topup`, `session_debit`, `refund`, `cpo_topup` (2026-07-21, `0026_offline_topups`, first migration in this repo to `ALTER TYPE ... ADD VALUE` an existing enum rather than create one), `account_closure` (2026-08-18, `0038_account_closure`, same `ALTER TYPE` idiom, the forfeit line written when a closing account's remaining charging credit is zeroed, so the running balance stays reconcilable by summing `amount`) Wave 4 R2 (built, deployed 2026-11-20): `idle_fee` (`0052_idle_fee`) and `reservation_no_show_fee` (`0055_reservation_approval`), both `ALTER TYPE ... ADD VALUE IF NOT EXISTS`. |
| `ReservationStatus` (`reservation_status`) | `booked`, `cancelled`, `fulfilled`, `expired` (2026-07-12, `0016_reservations`), `pending`, `declined` (wave 4 R2, `0055_reservation_approval`, built, deployed 2026-11-20) |

## 2. Tables

### `tenants`
CPO/organization. `id` PK · `name` unique · `created_at`.
Owns users, gateways, sessions, charger_groups (cascade delete-orphan).

### `users`
`id` PK · `tenant_id` → tenants (SET NULL, nullable) · `email` unique ·
`hashed_password` · `full_name` (NOT NULL; may be the empty string for a
Google signup without a name claim, since wave 4) · `role` (default `driver`) ·
`coin_balance` **NUMERIC(12,2)** (Decimal, default 0.00) ·
`token_version` INTEGER (default 0; JWT-revocation epoch, embedded as the
`tv` claim and re-checked per request — revision `0003_token_version`,
2026-07-08) · `created_at`.
`deleted_at` TIMESTAMPTZ nullable (revision `0038_account_closure`,
2026-08-18) — when the holder closed the account; NULL while live.
`role` (enum `user_role`): `admin` | `cpo` | `driver` | `cpo_viewer`.
`cpo_viewer` was added in `0045_cpo_viewer_role` via `ALTER TYPE ... ADD
VALUE`; it cannot be dropped on downgrade. A `cpo_viewer` has `tenant_id` set
to the tenant whose console it can read (read-only, see SECURITY.md §8.11).
CHECK `ck_users_coin_balance_non_negative` (`coin_balance >= 0`, revision
`0002_wallet_non_negative`, 2026-07-07) — the DB-level backstop behind the
row-locked, clamped wallet debits.
`notification_prefs` JSONB NULL (`0048_notification_prefs`, C7, wave 3, not
deployed): per-type push toggles and quiet hours
(`services/notification_prefs.NotificationPrefs` dumped as JSON). NULL means
the defaults (all pushes on, no quiet hours). Included in the personal data
export as `account.notification_prefs`.

**Account closure ANONYMISES this row; it does not delete it.** Every
`user_id` foreign key in this document is `ON DELETE CASCADE`, so
`DELETE FROM users` would take the account's `charging_sessions`,
`ledger_transactions` and GST `invoices` with it — records the operator is
required to keep, and the inputs to the CPO earnings / payout-watermark maths.
So `DELETE /api/auth/me` (`services/account_closure.py`) purges only the rows
that are purely the person (push subscriptions, notifications, favourites,
watches, group memberships, capacity requests, outstanding reset/verification
tokens), cancels BOOKED reservations and WAITING queued charges, forfeits any
remaining credit as an `account_closure` ledger line, and then scrubs THIS row
into a tombstone: `email` -> `deleted-user-{id}@deleted.amphive.invalid`
(RFC 2606 reserved TLD — undeliverable, and can never collide with a real
signup while still satisfying the unique index), `full_name` -> `Deleted
user`, an unusable random `hashed_password`, `google_sub` NULL,
`auth_provider` `deleted`, `is_disabled` true, `email_verified` false,
`token_version` bumped (every outstanding JWT dies on its next request).
The financial rows survive, pointing at a row that identifies nobody.
Since wave 4 (deployed 2026-11-20) closure also deletes the user's `plug_reviews`
and `plug_photos` (stored image objects deleted in a background task after
the response, best-effort; rejected photos skipped, their object is deleted
at rejection; a failed deletion leaves an unreachable orphan object) and
clears `notification_prefs`; `promo_redemptions` stay against the tombstone.

### `gateways`
`id` **VARCHAR(50) PK** (caller-supplied MAC/UUID) · `tenant_id` → tenants
(CASCADE, **nullable** as of rev `0034_gateway_claim_code` — NULL = unclaimed
inventory, mirrors the `users.tenant_id` nullable-FK precedent) · `name` ·
`vpn_ip` unique · `status` (default `offline`) · `firmware_version`
VARCHAR(32) nullable (fw last reported in the `online` status; rev `0006`,
LWT never clobbers it) · `latitude`/`longitude` nullable ·
`claim_code` VARCHAR(16) nullable, **partial-unique** (`WHERE claim_code IS
NOT NULL`) · `claimed_at` TIMESTAMPTZ nullable · `last_seen_at` ·
`created_at`. Owns plugs.

**[Claim-code onboarding, 2026-08-02]** `claim_code`/`claimed_at` back the
preflashed-unit self-claim flow (`POST /api/admin/gateways/inventory` mints
an unclaimed row with `tenant_id NULL` + a fresh code; `POST
/api/cpo/gateways/claim` sets `tenant_id` + `claimed_at` on a successful
claim). `claim_code` is **not** cleared on claim — kept for admin
audit/reprint; "claimed" is `claimed_at IS NOT NULL`, not code presence.
Gateways created the old way (direct `POST /api/cpo/gateways`) have
`claim_code NULL` and were never inventory. See
[docs/API_REFERENCE.md](API_REFERENCE.md) and
[deploy/docs/preflashed_unit_runbook.md](../deploy/docs/preflashed_unit_runbook.md).

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

Plug hours (`0049_plug_hours`, C4, wave 3, deployed 2026-11-19; down_revision
`0048_notification_prefs`):
`opening_hours` JSONB NULL: a list of `{days, start, end}` windows in tenant
local time with TariffSlot semantics; NULL means open 24x7 (cleared values
are SQL NULL). `max_session_minutes` INTEGER NULL. `amenities` JSONB NULL:
tags from a fixed server-side allowlist. `access_note` VARCHAR(280) NULL:
plain text for drivers.

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
was started with, snapshotted verbatim from `SessionStartRequest`. **Opt-in
since 2026-08-02** — `SessionStartRequest` defaults both to `None`/absent, so
NULL is the *normal* value for a session with no driver-chosen limit (charge
until stopped), not just a legacy-row artifact (before that date an omitted
field defaulted to 30 kWh / 4 h here). Enforced backend-side by
`MQTTManager._maybe_auto_stop_on_limits` on the telemetry path (env
`AUTO_STOP_ON_LIMITS`) plus a reaper duration backstop when set; NULL is
never limit-auto-stopped. The firmware/AmpHive-Agent gateway still enforces
concrete watchdog values locally — the driver's own limit, or a large
UNLIMITED sentinel (`services/mqtt_manager.py`) when this column is NULL,
since the wire protocol has no way to encode "no limit" directly. `max_kwh`
is float, not NUMERIC — an energy threshold, not money.

`auto_stop_when_full` BOOLEAN NULL (`0044_session_auto_stop_full`, F1,
2026-11-17-w2, deployed 2026-11-19): driver opt-in to stop when the car looks full;
NULL/false = off.

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
  `not_before TIMESTAMPTZ NULL` (`0041_queued_not_before.py`,
  2026-11-15): NULL = outage queue; set = a scheduled start on a
  powered plug that the sweep never begins before this instant (its
  `expires_at` is `not_before + min(queue_ttl_min, SCHEDULE_START_GRACE_MIN)`).
  `auto_stop_when_full BOOLEAN NULL` (`0044_session_auto_stop_full`, F1):
  carried onto the auto-started session.
- **`password_reset_tokens`** (`0023_password_reset_tokens.py`) — a
  single-use, SHA-256-digest-only "forgot password" token
  (`PasswordResetToken`). Backs `POST /api/auth/forgot-password` +
  `POST /api/auth/reset-password`.
- **`email_verification_tokens`** (`0037_email_verification.py`) — a
  single-use, SHA-256-digest-only "verify your email" token
  (`EmailVerificationToken`), an exact mirror of `password_reset_tokens`.
  Paired with the new **`users.email_verified`** BOOLEAN NOT NULL DEFAULT
  false: `POST /api/auth/register` now creates an UNVERIFIED account and emails
  a link instead of auto-logging-in, `POST /api/auth/verify-email` consumes the
  token and flips the flag, `POST /api/auth/login` 403s until it's true, and
  Google sign-ins/links set it true (Google asserts the verified email). The
  `0037` migration **GRANDFATHERED every pre-existing row to `true`**
  (`UPDATE users SET email_verified = true` right after adding the column) so
  the live prod user base is never locked out — only signups made after the
  migration, through the app, start `false`. Closes the account-PRE-hijacking
  class at its root: while `false` the account's password is provisional
  (re-registering replaces it, a Google link drops it; residual in
  SECURITY.md §8.6). Backs `POST /api/auth/register` + `verify-email` +
  `resend-verification`.
- **`revoked_sessions`** (`0042_revoked_sessions.py`, 2026-11-15,
  after the off-peak `0041_queued_not_before`). Per-device logout's revocation list (`RevokedSession`):
  `sid` TEXT PRIMARY KEY (the JWT `sid` claim, a random sign-in session id),
  `user_id` FK `users` ON DELETE CASCADE (indexed,
  `ix_revoked_sessions_user_id`), `revoked_at` TIMESTAMPTZ default now,
  `expires_at` TIMESTAMPTZ (indexed, `ix_revoked_sessions_expires_at`) =
  `auth_time + JWT_MAX_SESSION_DAYS + JWT_EXPIRY_DAYS`, floored at the
  token's `exp`. Written by `POST /api/auth/logout`; read (one PK lookup) by
  every token-accepting path; rows past `expires_at` are pruned after each
  insert. Not exported by `GET /api/auth/me/export` (credential metadata,
  like the token digests) and not purged at closure (closure's
  `token_version` bump already kills every sid; rows age out). Its
  `downgrade()` bumps `token_version` for every user with a live row before
  the DROP, so a downgrade does not un-revoke anyone (SECURITY.md §2).
- **`offline_topups`** (`0026_offline_topups.py`) — a CPO's cash top-up of a
  driver's coin wallet, funded from the tenant's own unsettled net earnings
  (`OfflineTopup`; `actor_user_id`/`driver_user_id` nullable + SET NULL, same
  survives-account-deletion rationale as `audit_logs`). Read back by
  `services/payouts.py tenant_earnings_summary`'s `available_pool_coins` so
  neither a top-up nor a later bank payout can draw the same earnings twice.
  Backs `POST/GET /api/cpo/topups`.
  `promo_code_id INT NULL` FK promo_codes SET NULL (`0047_promo_codes`, F12):
  set when the top-up came from a promo redemption. Redemptions write an
  ordinary `offline_topups` row plus a `cpo_topup` ledger line, so
  `sum_offline_topups`, the top-up pool and payouts include them unchanged. No
  `tx_type` change.
- **`firmware_releases`** (`0036_firmware_releases.py`, feat/ota-version-
  picker) — an admin-registered firmware release catalog (`FirmwareRelease`:
  `version` UNIQUE, `url` the already-published `https://` image location —
  `gs://amphive-fw-2` or the backend's own image host, see
  [FIRMWARE.md](FIRMWARE.md) §7 — `notes`,
  `is_active` soft-deactivate flag). Not tenant-scoped: one firmware fleet.
  Replaces hand-pasting a firmware URL with a version dropdown on the CPO
  OTA flow; ordering everywhere is semver-aware
  (`services/versioning.version_sort_key`), never a raw string sort. Backs
  `POST/GET /api/admin/firmware-releases` + `POST
  /api/admin/firmware-releases/{id}/deactivate` (and its inverse
  `.../reactivate`, 2026-08-11 PR #115) + `GET
  /api/cpo/firmware-releases` (active only). The table itself still stores
  only URLs, never binary blobs — since feat/admin-dashboard (2026-08-04)
  `POST /api/admin/firmware-releases/upload` accepts the `.bin` itself and
  auto-registers a row; since 2026-08-11 (PR #115) that upload **publishes
  the bytes to `gs://amphive-fw-2`** and registers the bucket URL (the
  2026-08-04 backend-volume host `GET /api/firmware/images/{filename}` is
  kept only as legacy back-compat for URLs registered before the switch).
  Re-uploading/re-registering a **deactivated** version overwrites
  `url`/`notes` and reactivates the row; an active duplicate stays a 400.
- **`api_tokens`** (`0046_api_tokens.py`, F7, 2026-11-17-w2, deployed 2026-11-19) - a
  personal read-only API token. Columns: `id` SERIAL PK; `user_id` INT NOT
  NULL FK users ON DELETE CASCADE (`ix_api_tokens_user_id`); `name`
  VARCHAR(64) NOT NULL; `token_prefix` VARCHAR(16) NOT NULL; `token_hash`
  VARCHAR(64) NOT NULL UNIQUE (sha256 hex); `scope` VARCHAR(16) NOT NULL
  DEFAULT `'read'`; `created_at` TIMESTAMPTZ NOT NULL DEFAULT now;
  `last_used_at` TIMESTAMPTZ NULL; `revoked_at` TIMESTAMPTZ NULL (NULL =
  live). The runtime role needs SELECT/INSERT/UPDATE, covered by the owner
  role's default privileges. Backs `/api/me/tokens*` and `GET /api/me/meter`.
- **`promo_codes`** (`0047_promo_codes.py`, F12, 2026-11-17-w2, not
  deployed) - `id`, `tenant_id` FK tenants CASCADE, `code_hash` VARCHAR(64)
  UNIQUE (sha256 of the canonical code), `code_hint` VARCHAR(4) (last 4
  chars), `amount_coins` NUMERIC(12,2) CHECK > 0, `expires_at` TIMESTAMPTZ
  NULL (NULL = never), `max_redemptions` INT CHECK >= 1, `per_user_limit` INT
  DEFAULT 1 CHECK >= 1, `note` VARCHAR(200), `created_by` FK users SET NULL,
  `created_at`, `disabled_at`. Index `idx_promo_codes_tenant_created`
  (`tenant_id`, `created_at`).
- **`promo_redemptions`** (`0047_promo_codes.py`) - `id`, `code_id` FK
  promo_codes CASCADE, `user_id` FK users SET NULL, `offline_topup_id` FK
  offline_topups CASCADE UNIQUE, `created_at`. Index
  `idx_promo_redemptions_code_user` (`code_id`, `user_id`). Limits are
  COUNT(*) of these rows, taken under the `promo_codes` row lock.

- **`charge_routines`** (`0050_charge_routines.py`, C5, wave 3,
  deployed 2026-11-19; down_revision `0049_plug_hours`) - a driver's recurring weekly
  charge. Columns: `id` SERIAL PK; `user_id` FK users CASCADE (indexed
  `ix_charge_routines_user_id`); `plug_id` FK plugs CASCADE; `tenant_id` FK
  tenants CASCADE, denormalized from plug -> gateway -> tenant like
  `queued_charges`; `weekdays` SMALLINT, bitmask Mon=1..Sun=64, CHECK
  `ck_charge_routines_weekdays` 1..127; `start_time` TIME, local wall-clock in
  the tenant timezone; `max_kwh` DOUBLE PRECISION NULL; `max_duration_seconds`
  INTEGER NULL, with CHECK `ck_charge_routines_has_limit` requiring at least
  one of the two limits; `auto_stop_when_full` BOOLEAN default false;
  `enabled` BOOLEAN default true; `last_materialized_date` DATE, the local
  date of the last handled occurrence, stamped under a row lock in the same
  transaction as the insert; `consecutive_failures` INTEGER default 0;
  `gate_notified` BOOLEAN NOT NULL DEFAULT false (the driver was told the
  routine is paused because queued charging is off for the plug; sent once,
  re-armed by the next materialize pass that finds the queued-charging gate
  on); `last_queued_charge_id` FK queued_charges
  SET NULL, the occurrence's row until its outcome is settled; `created_at`.
  Account closure deletes the user's routines.

The ORM declares **40 tables** on `main` (`__tablename__`
count, rechecked 2026-11-20; 36 as of `0050_charge_routines`, the four new ones
are `group_invites`, `ocpp_charge_points`, `ocpp_id_tags` and
`ocpp_transactions`). Prod, at `0056_ocpp_charge_points` since 2026-11-20, has
40 (36 at `0050_charge_routines`, 35 at `0047_promo_codes`, 32 at
`0043_email_ci_unique`); the R2 tables were created by the 2026-11-20 deploy. All are applied via Alembic
per §4 below.

### Wave 4 R2 schema (`release/2026-11-19-r2`, migrations `0051` to `0056`, built, deployed 2026-11-20)

Every change is additive (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`,
`ALTER TYPE ... ADD VALUE IF NOT EXISTS`), with no backfill, no GRANT (the owner
role's default privileges cover new tables) and no index on a hot table.

**`0051_durable_notice_state`** (lane DUR). Five nullable `TIMESTAMPTZ` columns,
no default, no index:

- `charging_sessions.full_notified_at`, `nearly_done_notified_at`,
  `power_dropped_notified_at`: once-per-session notice claims
  (`UPDATE ... WHERE col IS NULL AND status = 'active'`), written only when a
  detector fires. `full_notified_at` is also the durable "judged fully charged"
  marker (the C1 idle fee reads it).
- `gateways.offline_notified_at`: an operator "Gateway offline" alert is
  outstanding (cleared by the back-online follow-up; it only counts while
  `last_seen_at <= offline_notified_at`). `gateways.last_operator_alert_at`: the
  time of the last offline alert (the cooldown start; a pending re-check is
  re-armed from it at startup). Both are written under the gateway row lock.

**`0052_idle_fee`** (lane C1). `tx_type` gains `idle_fee`.

- `tariffs.idle_grace_min` INTEGER NOT NULL DEFAULT 0, `idle_fee_per_min`
  NUMERIC(12,2) NOT NULL DEFAULT 0, `idle_fee_cap` NUMERIC(12,2) NOT NULL
  DEFAULT 0: the opt-in terms (0 = off; when on, the routes require a cap above
  0 and a grace of at least 15).
- `charging_sessions.idle_grace_min` INTEGER, `idle_fee_per_min` NUMERIC(12,2),
  `idle_fee_cap` NUMERIC(12,2): the terms copied at start (NULL = no fee).
  `idle_started_at` TIMESTAMPTZ (the open idle stretch), `idle_seconds` INTEGER
  NOT NULL DEFAULT 0 (banked idle seconds; **-1 = voided** by a renewed draw),
  `idle_fee_coins` NUMERIC(12,2) (collected at finalize, also included in
  `coins_spent`).
- Downgrade drops the columns and leaves the enum value.

**`0053_vehicle_profile`** (lane F8V). Four `users` columns: `vehicle_profile`
JSONB NULL (`services/insights.VehicleProfile`: `km_per_kwh`,
`petrol_price_per_l`, `petrol_km_per_l`; NULL = defaults),
`monthly_insights_email` BOOLEAN NOT NULL DEFAULT false (opt-in),
`insights_email_month` VARCHAR(7) NULL (the last `YYYY-MM` claimed, sent or
skipped as empty: the idempotency stamp, compared as a string),
`insights_email_failures` SMALLINT NOT NULL DEFAULT 0 (send failures for the
pending month; 3 gives the month up).

**`0054_member_price_invites`** (lane C9).

- `group_memberships.price_per_kwh_override` NUMERIC(12,2) NULL: the member's
  own flat price on the group's plugs. `charging_sessions.member_price_override`
  BOOLEAN NULL: true when the session rate is a member override frozen at start
  (never repriced).
- `group_invites`: `id`; `group_id` FK `charger_groups` ON DELETE CASCADE;
  `tenant_id` FK `tenants` ON DELETE CASCADE; `email` VARCHAR(255) (lowercased);
  `token_hash` VARCHAR(64) UNIQUE (the SHA-256 of the mailed token, the raw
  token is never stored); `invited_by_user_id` FK `users` SET NULL;
  `created_at`; `expires_at` (created + 7 days); `accepted_at`;
  `accepted_by_user_id` FK `users` SET NULL; `revoked_at`. A newer invite for
  the same group and email revokes the older one. Indexes:
  `ix_group_invites_group_id`, `ix_group_invites_inviter_created`
  (`invited_by_user_id`, `created_at`, the per-CPO daily cap) and
  `ix_group_invites_created_at` (the platform-wide cap). Account closure keeps
  the row but rewrites `email` to the tombstone address and revokes a pending
  invite; the table is not in the data export.

**`0055_reservation_approval`** (lane C8). `reservation_status` gains `pending`
and `declined`; `tx_type` gains `reservation_no_show_fee` (enum labels cannot be
removed on downgrade).

- `plugs.reservation_mode` VARCHAR(16) NOT NULL DEFAULT 'instant';
  `plugs.no_show_fee_coins` NUMERIC(12,2) NOT NULL DEFAULT 0.
- `reservations.no_show_fee_coins` NUMERIC(12,2) NOT NULL DEFAULT 0 (the fee at
  booking time); `reservations.no_show_checked_at` TIMESTAMPTZ NULL (set when
  the no-show sweep settles the row, charged or skipped, or when a pending
  request lapses). Partial index `idx_reservations_no_show_due` ON
  `reservations(start_at) WHERE no_show_checked_at IS NULL AND
  no_show_fee_coins > 0`.
- `ledger_transactions.reservation_id` INTEGER NULL UNIQUE REFERENCES
  `reservations(id)` ON DELETE SET NULL: the no-show fee's idempotency key.

**`0056_ocpp_charge_points`** (lane C2, no enum change; the tables stay empty
while `OCPP_CSMS_ENABLED` is off). Detail in [OCPP_CSMS.md](OCPP_CSMS.md).

- `ocpp_charge_points`: `charge_point_id` VARCHAR(40) UNIQUE (`[A-Za-z0-9_.-]`),
  `tenant_id` FK `tenants` CASCADE, `gateway_id` UNIQUE FK `gateways` (the
  backing gateway, never marked online), `password_hash` (bcrypt), `vendor`,
  `model`, `firmware_version`, `last_boot_at`, `created_at`.
- `ocpp_id_tags`: `id`, `user_id` FK `users` (indexed), `tenant_id` FK `tenants`
  ON DELETE CASCADE (the operator the tag is enabled for), `tag_hash`
  VARCHAR(64) (SHA-256 hex; a lookup key, not a password hash), `created_at`;
  UNIQUE `uq_ocpp_id_tags_tenant_tag` (`tenant_id`, `tag_hash`). Listed in the
  user data export (id, operator tenant, created_at) and hard-deleted on
  account closure.
- `ocpp_transactions`: `session_id` PK FK `charging_sessions` (the OCPP
  `transactionId`), `charge_point_id` FK (indexed), `connector_id`,
  `id_tag_hash`, `meter_start_wh`, `created_at`.

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
  **36 tables** once `0050_charge_routines` is applied (35 at `0047`, deployed 2026-11-19; 32 at `0043`), **40** once `0056_ocpp_charge_points` is applied (wave 4 R2, deployed 2026-11-20); see "Tables added since" in §2 above for the rest.)
- **Wave 2 chain (2026-11-17, `release/2026-11-17-w2`, deployed 2026-11-19).**
  `0043_email_ci_unique` -> `0044_session_auto_stop_full` (two nullable
  booleans, IF NOT EXISTS) -> `0045_cpo_viewer_role` (enum value, downgrade is
  a no-op) -> `0046_api_tokens` (CREATE TABLE IF NOT EXISTS, downgrade drops
  it) -> `0047_promo_codes` (two tables plus `offline_topups.promo_code_id`,
  downgrade drops them). One head. `env.py` runs all pending revisions in one
  transaction; `ALTER TYPE ... ADD VALUE` is allowed there on PG 12+ (prod is
  15) because no later revision uses the new value.
- **Wave 3 chain (2026-11-19, `release/2026-11-17-w3`, deployed 2026-11-19).**
  `0047_promo_codes` -> `0048_notification_prefs` (`users.notification_prefs`
  JSONB, ADD COLUMN IF NOT EXISTS, no backfill, no GRANT) ->
  `0049_plug_hours` (four nullable `plugs` columns, ADD COLUMN IF NOT EXISTS,
  metadata-only; downgrade drops them) -> `0050_charge_routines` (CREATE
  TABLE IF NOT EXISTS plus `ix_charge_routines_user_id`; downgrade drops the
  table). One head, no branch point (checked offline with `ScriptDirectory`).
  All three were written against `0047` on their branches and rechained at
  integration. The owner role's default privileges cover the new table.
- **Wave 4 R2 chain (2026-11-19, `release/2026-11-19-r2`, built, deployed 2026-11-20).**
  `0050_charge_routines` -> `0051_durable_notice_state` (five nullable
  timestamptz columns) -> `0052_idle_fee` (`tx_type` value, three `tariffs`
  and six `charging_sessions` columns) -> `0053_vehicle_profile` (four `users`
  columns) -> `0054_member_price_invites` (two columns, table `group_invites`,
  three indexes) -> `0055_reservation_approval` (three enum values, five
  columns, one partial index) -> `0056_ocpp_charge_points` (three tables).
  Every lane wrote its revision against `0050` and the integrator rechained
  them (`ccbd096`); one head, no branch point. Everything is `IF NOT EXISTS`
  and reversible, except that Postgres cannot drop the four new enum labels
  (`idle_fee`, `reservation_no_show_fee`, `pending`, `declined`): a downgrade
  leaves them, which is harmless, but an older Python enum cannot map a ledger
  row or reservation that already uses one. The enum additions share the one
  upgrade transaction, which PG 12+ allows because no later revision uses a
  new value. No GRANT is needed.
- **New schema change** = new revision: `alembic -c backend/alembic.ini
  revision --autogenerate -m "..."` (autogenerate needs a reachable database —
  use the CI postgres or the VM; dev boxes run no DB by policy).
- **Startup** applies `upgrade head` automatically; a database predating
  Alembic (built by the old `create_all` path) is detected (tables exist, no
  `alembic_version`) and stamped at the baseline first.
- **Writing-a-migration conventions (2026-08-11).** Because `init_db()` runs
  `alembic upgrade head` on **every boot of the single backend container**, a
  slow migration is a startup stall — i.e. **downtime** — and a broken revision
  graph wedges boot entirely. Hence:
  - **No inline data backfills on large tables.** A backfill
    (`UPDATE ... SET` across many rows) runs **out-of-band** — a one-off
    script/management command executed after deploy — never inside the
    migration itself. `telemetry_readings` is the hot table to worry about.
  - **Index creation on hot tables** uses `CREATE INDEX CONCURRENTLY`, which
    can't run in Alembic's usual transaction — wrap it in
    `op.get_context().autocommit_block()`. A plain `CREATE INDEX` on
    `telemetry_readings` takes a lock *and* blocks boot for the duration.
  - **Exactly one head.** Two parallel branches each adding a revision produce
    two heads, which makes `upgrade head` ambiguous and **wedges startup**
    until someone hand-merges. CI enforces single-head
    (`backend/tests/test_migration_heads.py`, PR #110, pure/no-DB) — rebase the
    revision chain into a single line rather than `alembic merge` where
    possible.
  - **Least-privilege role grants have an object-class limit.** The runtime
    role's grants (see `_provision_runtime_role` in `backend/database/db.py`,
    which documents this) cover tables/views/sequences created by the owner in
    schema `public` only — a migration that adds a **materialized view**, a
    **new schema**, or objects owned by a **different role** must `GRANT` to
    `APP_DB_USER` explicitly, or the app gets runtime "permission denied"
    that CI (running as the owner) never sees.
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
