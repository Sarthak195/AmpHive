# OCPP 1.6J central system (feature C2)

A development and portfolio module: AmpHive can act as an OCPP 1.6J central
system (CSMS), so a standard OCPP charger or simulator registers as a set of
ordinary plugs and is billed through the same ledger as the ESP32 gateways.

**It is OFF by default and OFF in production.** With `OCPP_CSMS_ENABLED`
unset, `backend/main.py` never imports `backend/routers/ocpp.py` or
`backend/services/ocpp_adapter.py`, mounts no route, and nothing in the
Caddyfile forwards `/ocpp`. `backend/tests/test_ocpp_csms.py` checks this in
a fresh interpreter. No new dependency: the OCPP-J framing is implemented
here on FastAPI's WebSocket (the `websockets` package already ships with
`uvicorn[standard]`), with pydantic models for the Core payloads.

## Scope

OCPP 1.6 Core profile, charge point to central system only:

| Action | What the central system does |
|---|---|
| BootNotification | Stores vendor, model, firmware and boot time; answers `Accepted` with a heartbeat interval (`OCPP_HEARTBEAT_INTERVAL_SEC`, 60). |
| Heartbeat | Answers `currentTime`. No database write. |
| StatusNotification | `Faulted` on a connector takes an AVAILABLE plug out of service (OFFLINE, the safe direction; the operator re-enables it). Every other status writes nothing. |
| Authorize | `Accepted` for a tag bound to a live account whose available balance clears `MIN_START_BALANCE_COINS`; `Blocked` for a disabled or closed account or a low balance; `Invalid` for an unknown tag. |
| StartTransaction | The same admission as a walk-up start (below); `Accepted` with `transactionId` = the charging session id, otherwise `transactionId` 0 with `Blocked` (a gate refused) or `Invalid` (unknown tag or connector). A retried start for the running transaction (same connector, tag and meterStart) gets the same id back. |
| MeterValues | Readings for one of this charge point's ACTIVE transactions feed the existing telemetry persist path; idle readings write nothing. |
| StopTransaction | Feeds `meterStop`, then bills through `finalize_charging_session`. Always answered, also for a transaction the backend already closed. |

Not implemented: RemoteStartTransaction, RemoteStopTransaction and every other
central-system-initiated CALL (the central system never sends a CALL), the
other profiles, OCPP 2.x, security profiles 2 and 3 (TLS client certificates).
Any other action is answered with CALLERROR `NotImplemented`.

## How it maps onto the platform

- A **charge point** is an `ocpp_charge_points` row backed by an ordinary
  `gateways` row with id `ocpp-<charge_point_id>`. Connector N is the ordinary
  `plugs` row on that gateway whose `unique_id` is `ocpp:<charge_point_id>:N`
  (`plug_model` `ocpp1.6j`). Tariffs, groups, caps, opening hours,
  reservations, holds, the ledger, receipts and the CPO views apply unchanged.
- The backing gateway is **never marked ONLINE**, so `gateway_is_live()` stays
  false for it. The app's own start paths (walk-up start, queued charge,
  charge routine) therefore refuse an OCPP connector, and the only way to
  start a session there is the charger's own StartTransaction. The public
  status page and the admin health view count it as an offline gateway.
- **StartTransaction** replays the walk-up route's caller-side gates
  (`routers/sessions.py start_charging_session`) in the same user then plug
  lock order: account live, concurrent-session cap, plug lock, private-group
  membership, plug AVAILABLE, lazy reservation expiry and another driver's
  covering reservation, another driver's scheduled start. It then calls the
  shared `begin_active_session` (circuit caps, opening hours and the
  `max_session_minutes` clamp, rate snapshot, wallet floor and authorization
  hold) with `actuate=False`, so no MQTT command is published. Skipped on
  purpose: the gateway liveness gate (the charger is connected right now)
  and the plug power gate (the charger meters itself). The OCPP session has
  no energy or time limit of its own; the hold is the available balance.
- **MeterValues** and the StopTransaction `meterStop` are converted to
  session-relative kWh (`register - meterStart`, Wh or kWh units, whole-meter
  values only, per-phase values ignored) and handed to
  `MQTTManager._persist_telemetry` as a live frame for that session: the same
  ownership check, monotonic energy, TOD reprice, wallet-exhaustion and limit
  auto-stops and `telemetry_readings` enqueue that ESP32 telemetry gets.
  Billing has one metering source. The finite / `MAX_PLAUSIBLE_KWH` guards are
  replayed in the adapter, plus two OCPP-specific bounds: a register reading
  below the last one fed is stale and ignored (an OCPP register only counts
  up), and the session total never exceeds `OCPP_MAX_CONNECTOR_KW` (default
  50) for the time since the server-side start, plus 0.1 kWh. A charger
  reporting more is billed at that bound and logged.
- **Keep-alive**: any handled CALL from a connected charge point (throttled to
  one UPDATE per 30 s per charge point) refreshes `last_telemetry_at` on its
  own ACTIVE sessions, so the stale-session reaper does not close a live
  transaction on a charger that sends MeterValues rarely or only in
  StopTransaction (`MeterValueSampleInterval=0` is valid OCPP 1.6). Once the
  connection drops, the reaper applies as usual.
- **StopTransaction** finalizes through `finalize_charging_session` with
  `actuate=False`: the same ledger debit (capped at the hold), receipt, bill
  email, plug release and watcher fan-out, without an MQTT OFF.

## Security

- WebSocket `/ocpp/{charge_point_id}`, subprotocol `ocpp1.6` required
  (otherwise HTTP 400 before the handshake completes).
- OCPP security profile 1: HTTP Basic, username = the charge point id in the
  path, password checked against a bcrypt hash (`services/auth.py`). An
  unknown id pays the same bcrypt cost against the dummy hash. Failure is
  HTTP 401 with `WWW-Authenticate`. The clear password is generated at
  registration, returned once and never stored or logged.
- Limits: `OCPP_HANDSHAKE_RATE_LIMIT` (10/60 per client IP, every handshake
  counts, HTTP 429), `OCPP_MAX_CONNECTIONS` (20, HTTP 503), one live
  connection per charge point (a new one replaces the old), text frames only
  (binary closes 1003), `OCPP_MAX_MESSAGE_BYTES` (8192, close 1009),
  `OCPP_MESSAGE_RATE_LIMIT` (60/60 per connection, close 1008), an idle
  timeout of three heartbeat intervals (`OCPP_IDLE_TIMEOUT_SEC`).
- Every payload is validated by a pydantic model with OCPP length limits and
  capped list sizes. A validation error is answered with a generic
  CALLERROR and never echoed (it would embed the idTag).
- Tenant scoping on every message: connectors resolve only to the plugs of
  the authenticated charge point's own backing gateway, transactions only to
  its own `ocpp_transactions` rows, idTags only to tags enabled for its own
  tenant. A charge point cannot meter or stop another charge point's
  session.
- idTags are per operator. A driver enables a tag for one operator at a time
  (the bind names one of that operator's charge points); a charge point of
  any other operator answers `Invalid` in Authorize and StartTransaction,
  exactly as for an unknown tag. A card's UID is broadcast to every reader,
  so without this any operator who once read the card could open a billed
  session against the driver's balance.
- idTags are stored only as a SHA-256 hex digest (upper-cased first, OCPP
  tags are case-insensitive). Logs carry the charge point id, plug and
  session ids, never a password, an Authorization header, a clear idTag or a
  raw frame.

## Provisioning (same flag)

- `POST /api/cpo/ocpp/charge-points` (cpo or admin with a tenant; a
  cpo_viewer is refused like every write): body `{charge_point_id, name,
  connectors (1-4)}`. Creates the backing gateway, one AVAILABLE plug per
  connector and the charge point row, audits `ocpp.charge_point.create`, and
  returns `{charge_point_id, gateway_id, websocket_path, subprotocol,
  password, connectors: [{connector_id, plug_id}]}`. 409 if the id is taken.
- `POST /api/ocpp/id-tags` (any signed-in user): body `{id_tag,
  charge_point_id}` binds a tag to the caller's own account and enables it
  for that charge point's operator only (201 `{id, tenant_id}`; 404 for an
  unknown charge point; 409 if the tag is already bound at that operator or
  the account already has 10 tags).
- `GET /api/ocpp/id-tags`: the caller's own tags, `[{id, tenant_id,
  created_at}]` (newest first, at most 10). There is no clear tag to show.
- `DELETE /api/ocpp/id-tags/{id}`: removes one of the caller's own tags (204,
  404 otherwise).

The only GET route returns the caller's own rows and no personal or infra
data, so a cpo_viewer sees nothing new. Bound tags are listed in the data
export (`ocpp_id_tags`: id, operator tenant id, created_at) and deleted on
account closure.

## Data model (migration `0056_ocpp_charge_points`)

- `ocpp_charge_points`: `charge_point_id` (unique, `[A-Za-z0-9_.-]{1,40}`),
  `tenant_id`, `gateway_id` (unique, FK gateways), `password_hash` (bcrypt),
  `vendor`, `model`, `firmware_version`, `last_boot_at`, `created_at`.
- `ocpp_id_tags`: `user_id` (indexed), `tenant_id` (FK tenants, the operator
  the tag is enabled for), `tag_hash` (SHA-256 hex), `created_at`; unique
  `uq_ocpp_id_tags_tenant_tag (tenant_id, tag_hash)`.
- `ocpp_transactions`: `session_id` (PK, FK charging_sessions; the OCPP
  transactionId), `charge_point_id` (FK, indexed), `connector_id`,
  `id_tag_hash`, `meter_start_wh`, `created_at`.

No enum change; fully reversible. With the flag off the tables stay empty.

## Known limits

- Simulator-only. Not validated against real EVSE quirks: transaction id
  handling, meter start and stop semantics, offline-queued StopTransaction
  replayed after the reaper already closed the session (logged as a warning
  with the unbilled kWh, never billed).
- No RemoteStop: a backend-side stop (wallet exhaustion, a limit auto-stop,
  the telemetry-lost reaper, a reservation force-stop, a driver pressing Stop
  in the app) bills and frees the session but cannot stop the charger, so
  energy delivered after it is not billed. Those shared stop paths still
  publish their usual MQTT OFF (and SET_INTERVAL) to the backing gateway's
  topic `amphive/gateways/ocpp-<id>/...`: nothing subscribes there and no
  broker credentials exist for `ocpp-*` ids, so it actuates nothing. Only
  StartTransaction and StopTransaction use `actuate=False`.
- The keep-alive means a charger that stays connected but never sends
  StopTransaction keeps its session (and the driver's whole-balance hold)
  open until the driver presses Stop in the app (which finalizes regardless
  of the inert publish), a time limit applies, or the connection drops.
- There is no default energy cap on an OCPP session's hold: without
  RemoteStop a cap would end billing while the charger keeps delivering. The
  per-operator tag scope and the `OCPP_MAX_CONNECTOR_KW` bound limit what a
  charger can bill instead.
- Each MeterValues frame for a running transaction writes the session row,
  like an ESP32 telemetry frame; the per-connection rate limit bounds it.
- `StartTransaction` admission duplicates the walk-up route's caller-side
  gates rather than sharing them; a new gate added to the walk-up route must
  be added to `services/ocpp_adapter.py _admit_and_begin` too
  (`test_ocpp_csms_db.py` pins the reservation and opening-hours gates).
- `POST /api/ocpp/id-tags` answers 409 for a tag someone else bound at the
  same operator, which reveals that to anyone who already holds the tag
  value.
- The message size check runs after uvicorn has buffered the frame (uvicorn's
  own `--ws-max-size` default is 16 MiB).

## Enabling it (never on the production e2-micro as it stands)

1. Set `OCPP_CSMS_ENABLED=1` for the backend.
2. Add a reverse-proxy route that forwards WebSocket upgrades for `/ocpp/*`
   to the backend (Caddy, then frontend nginx, or straight to the backend),
   over TLS: profile 1 sends the password in every handshake.
3. Pass `--ws-max-size` to uvicorn close to `OCPP_MAX_MESSAGE_BYTES`.
4. Measure the backend's RSS with the flag on and a simulator connected
   before any use on the 1 GB VM.
5. Register a charge point, bind an idTag, point the charger at
   `wss://<host>/ocpp/<charge_point_id>` with the returned password.
