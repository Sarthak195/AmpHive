# AmpHive — Testing Guide & Roadmap

*Verified against source on 2026-07-26. Current state of automated testing
across the stack, plus a prioritized roadmap to close the remaining coverage
gaps.*

---

## 1. What exists today

### Backend (`backend/tests/`, pytest + pytest-asyncio + pytest-cov)

49 test files, **678 tests total: 518 pass locally / 160 skip locally**
(DB-gated — see below). Grouped by area, not exhaustive per-file:

**Money & session lifecycle** (the path that must never break)
- `test_wallet.py` — wallet write-path integrity: stale-identity-map lost
  updates, idempotent `_credit_topup`, debit clamping, concurrent
  credit/debit serialization (DB-gated)
- `test_money.py` — Decimal-safe coin/rupee arithmetic helpers (DB-free)
- `test_auth_holds.py` — authorization holds cap the real debit at
  `min(final_cost, hold_coins)` (DB-gated)
- `test_session_limits.py` — user-set `max_kwh`/`max_duration_seconds`
  persist + auto-stop (mixed DB-gated/DB-free)
- `test_max_active_sessions.py` — per-driver concurrent-session cap
- `test_active_session_speedup.py` — **regression** for the login/`/me`
  `MultipleResultsFound` crash
- `test_session_reaper.py` — stale-session sweep: reap reason, no
  double-counting user-stop races, one failure doesn't abort the sweep
- `test_session_start_plug_status.py`, `test_reconnect_off_republish.py` —
  plug status transitions on session start / gateway reconnect
- `test_registration_races.py` — **regression** for duplicate-insert races
  on `/api/auth/register` + `/api/cpo/setup`
- `test_payments.py` — `fetch_captured_payment()`: amount comes from
  Razorpay not the client; order-mismatch rejected (mocked SDK)

**Pricing / tariffs**
- `test_pricing.py`, `test_pricing_v2.py` — tariff resolution chain
  (plug → group → tenant default → env fallback) and Pricing v2 segmented
  (time-of-day) billing across slot boundaries (DB-gated)
- `test_pricing_batch.py` — N+1-free batch tariff lookups for list endpoints

**Auth / access control**
- `test_login.py`, `test_auth_hashing.py`, `test_auth_email_normalization.py`,
  `test_registration_validation.py` — credential + registration edge cases
- `test_token_revocation.py` — JWT `tv` epoch: stale/legacy tokens rejected
  after logout/password-change bumps `token_version`
- `test_password_reset.py` — reset-token flow + `services/email.py` fallback
- `test_rate_limiting.py` — sliding-window limiter on auth endpoints
- `test_access_codes.py` — private-group join via access code
- `test_direct_rbac.py` — `require_role("admin")` gate on the
  plug-actuating Direct-Mode endpoints
- `test_admin_router.py` — platform-admin API surface + `is_disabled`
  enforcement in login/`get_current_user`

**Gateways / MQTT / telemetry**
- `test_mqtt_manager.py` — paho-thread → event-loop marshaling into
  `TelemetryStore` (DB-free)
- `test_gateway_liveness.py` — `gateway_is_live` matrix (offline/stale/fresh/
  naive-legacy) + telemetry-driven `last_seen_at` refresh throttle
- `test_gateway_create.py`, `test_gateway_ota.py`, `test_plug_roster.py` —
  gateway/plug provisioning + retained roster publish
- `test_telemetry_persistence.py`, `test_telemetry_store.py` — buffered
  flush into `TelemetryReading`, bounded buffer + drop counting, live
  cost-calc segment state
- `test_plug_maintenance.py`, `test_plug_watch.py`, `test_public_plugs.py` —
  operator maintenance mode, "notify me when free" watches, public listing

**Caps, reservations, queued charge**
- `test_caps.py` — circuit admission control (Σ active caps ≤ group limit)
- `test_reservations.py`, `test_reservation_reaper.py` — book-ahead
  reservation gate + expiry sweep (DB-gated)
- `test_queued_charge.py` — queue-during-outage auto-start/expiry

**CPO operator & platform-admin surfaces**
- `test_cpo_gap_endpoints.py`, `test_driver_gap_endpoints.py` — redesign
  v3 contract endpoints (member roster, analytics, events, invoices CSV, …)
- `test_disputes.py`, `test_invoices.py` — session dispute triage, GST
  invoice issuance (DB-gated)
- `test_payouts.py`, `test_offline_topups.py` — settlement watermark math,
  CPO-funded cash top-ups capped at unsettled earnings (DB-gated)
- `test_audit_log.py`, `test_notifications.py` — admin audit trail,
  driver notification feed (DB row + Socket.io + Web Push)

**Infra**
- `test_migrations.py` — Alembic `upgrade head` matches the models
  (drift check via `compare_metadata`); pre-Alembic DB gets stamped, not
  re-migrated (DB-gated)
- `test_logging.py` — structured JSON logging + correlation ids
- `test_socketio.py` — connect auth (token in auth vs query), unauthorized
  `subscribe_session`, and a **regression test** for the `get_participants` bug

**DB-gated tests** (`test_auth_holds.py`, `test_disputes.py`,
`test_invoices.py`, `test_migrations.py`, `test_offline_topups.py`,
`test_payouts.py`, `test_pricing.py`, `test_pricing_v2.py`,
`test_reservation_reaper.py`, `test_reservations.py`,
`test_session_limits.py`, `test_wallet.py`) need a real Postgres
(`TEST_DATABASE_URL`) and **skip locally by policy** — this repo's dev boxes
run no database (see [AGENTS.md](../AGENTS.md) hard rule #1). They run for
real in CI, which provisions a throwaway `postgres:15` service container.

Run locally (repo-root `.venv` — the system Python has no pytest):
```bash
.venv/Scripts/python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
.venv/Scripts/python -m pytest backend/tests -q
# -> 518 passed, 160 skipped (the DB-gated tests above)
```
> Per repo policy, DB-gated tests only ever run against a throwaway
> container (locally or in CI) — **never** against the VM or a real database.

### Frontend (`frontend/src/**/*.test.{jsx,js}`, Vitest + React Testing Library)

**~570 tests across 62 test files** — every page, most components, all
contexts and utils carry a co-located `*.test.jsx`/`*.test.js`. Highlights:
- `contexts/AuthContext.test.jsx`, `SessionContext.test.jsx`,
  `TenantContext.test.jsx` — restore/login/logout, multi-session
  subscribe/switch, polled badge counts
- `components/ProtectedRoutes.test.jsx`, `HostRouting.test.jsx` — role gates
  and the driver/CPO/admin host-partitioned routing split
- `pages/cpo/*.test.jsx`, `pages/admin/*.test.jsx` — one suite per operator
  and platform-admin console page
- `utils/money.test.js`, `plugAvailability.test.js`, `safePath.test.js`,
  `statusCopy.test.js` — pure-function edge cases

Setup: `vite.config.js` `test` block (jsdom environment;
`src/test-setup.js` registers jest-dom matchers + RTL cleanup).

Run:
```bash
cd frontend
npm ci
npm test        # vitest run — all suites, single pass, CI-friendly
npm run lint    # eslint . (react-hooks / react-refresh rules)
```

### Firmware
No host-side unit tests. Validation is on-device via serial monitor
(`tools/read_serial.py`) and the throwaway `tools/klap_probe.py` KLAP probe.

### The CI gate (`.github/workflows/ci.yml`, runs on every push/PR to `main`)

Three jobs, all required:
1. **`backend-tests`** — a `postgres:15` service container is provisioned
   (`TEST_DATABASE_URL=postgresql+asyncpg://postgres:ci@localhost:5432/amphive_test`),
   so the DB-gated tests from §1 actually run here, unlike a local checkout.
   Steps, in order: `pip install -r backend/requirements.txt -r backend/requirements-dev.txt`
   → `ruff check backend/` (lint; config in `pyproject.toml`, E/F/W + isort) →
   `mypy` (lenient starter config scoped to the `backend/services` +
   `backend/routers` files that pass today) →
   `pytest backend/tests -q --cov=backend --cov-report=term-missing --cov-fail-under=66`.
   Runs on Python 3.11 (matches `backend/Dockerfile`'s `python:3.11-slim`).
2. **`frontend-lint`** — `npm ci` then `npm run lint` and `npm test`.
3. **`frontend-tests`** — a second, independent `npm ci` + `npm test` (kept
   as its own job so frontend test failures show as a distinctly-named
   required check, separate from lint).

A PR that fails ruff, mypy, the 66% coverage floor, eslint, or any Vitest/
pytest test cannot merge.

---

### Seeded test fixture (prod DB, since 2026-07-07)

The database was purged to a clean state and reseeded through the real APIs:
three accounts (**admin / cpo / driver**, emails `*@amphive.test`) plus the
CPO's tenant "AmpHive Test CPO" owning gateway `1cc3abb4fb54`, plug 1
("Volt-FastPlug-01" @ 192.168.1.6) in the public group "AmpHive Public
Station". **Credentials live in `TEST_ACCOUNTS.local.txt` at the repo root —
gitignored, never commit it.** Balances: admin 1000, driver 500, cpo 100
coins. The gateway row starts OFFLINE and flips online automatically on the
ESP32's next status/telemetry message (session starts 409 until then).

### Fake plug simulator (`tools/fake_plug.py`, added 2026-07-08, v2 2026-08-02)

A software stand-in for a **whole ESP32-C3 gateway** (not just one plug) so
the **whole stack** (session start/stop, wallet billing, live Socket.io
telemetry, the driver/CPO UI) can be exercised **without physical hardware**.
It speaks the full current MQTT contract in [MQTT_CONTRACT.md](MQTT_CONTRACT.md)
(fw 2.3.x parity): retained `online` status (with an `fw` version string) +
`offline` LWT, subscribes to the **retained multi-plug roster** on
`amphive/gateways/{gw}/config` and builds a simulated plug per roster entry
(up to `MAX_PLUGS`=4, matching real firmware) — a `--plug-id` bootstrap seed
is optional now, not required — plus the per-plug command topic (honours
`ON`/`OFF`/`SET_INTERVAL`/`SET_LIMITS`; `OTA` refuses with an
`OTA_REFUSED_SESSION_ACTIVE` alarm while a session is active, otherwise a
no-op — a fake plug has no firmware to flash). It publishes telemetry every
`--interval` seconds per plug and forwards a few realistic WARN/ERROR lines to
`amphive/gateways/{gw}/logs` (a local watchdog trip, a malformed command, a
roster overflow, an OTA refusal), matching what real firmware's log-forwarder
would send. While a session is `ON`, load **ramps** from 0 to the configured
watts over `--ramp-seconds` (soft-start realism) with `--jitter` noise on top
(default **10 kW**, `--watts`; per-plug override via `--watts-map
PLUGID=WATTS`), integrating session-relative energy just like the firmware —
so the live cost and wallet debit tick up predictably (~0.167 kWh/min ≈
0.83 coins/min at `COINS_PER_KWH=5` and the default load). A `--reset-counter
PLUGID@SECONDS[@VALUE_KWH]` flag (same shape as `tools/p110_sim`'s) forces a
mid-session counter drop, for exercising the backend's
`ENERGY_COUNTER_RESET_DROP_KWH` detection. Registration goes through the
public CPO API, never the DB.

**Where it runs.** The broker is `mqtt.amphive.app:8883` (public, direct-MQTT,
TLS + per-gateway credentials — see [MQTT_CONTRACT.md](MQTT_CONTRACT.md)), so
the MQTT loop can run from anywhere with `--broker-host mqtt.amphive.app --tls`,
or **inside the relay VM's compose network** as `--broker-host mqtt
--broker-port 1883` (plaintext, internal-only). The API registration step
(default `--api-base https://amphive.app`) works from anywhere.

```bash
# 1. One-time: create the fake gateway + plug (idempotent). Runs from anywhere.
python tools/fake_plug.py --register-only \
    --cpo-email cpo@amphive.test --cpo-password '<cpo-pw>'
#   -> prints:  gateway=fakeplug-gw-01  plug_id=<N>  (group 1 = public)

# 2. Keep the fake plug live (needs paho-mqtt). Ctrl-C stops it.
pip install paho-mqtt
python tools/fake_plug.py --run-only --plug-id <N> \
    --gateway-id fakeplug-gw-01 --broker-host mqtt.amphive.app --tls \
    --broker-user fakeplug-gw-01 --broker-pass '<MQTT_GW_PASSWORD>'
```

The gateway goes stale after `GATEWAY_LIVENESS_WINDOW_SEC` (120 s) once the
simulator stops, so keep it running while you test (a session won't *start*
against a stale gateway). `--self-test` prints the wire payloads (status,
roster, telemetry, SET_LIMITS, a sample `/logs` line) with no network for a
quick contract check. Broker creds default to `MQTT_GW_*`/`MQTT_*` env vars;
each gateway (including this one) authenticates with its **own** per-gateway
account (username == gateway_id) — the old shared `amphive-gateway` account
was retired 2026-07-10 (see [SECURITY.md](SECURITY.md) §3).

**Always-on deployment (running in prod since 2026-07-08).** For a fake plug
that stays live without a terminal open, `deploy/docker/docker-compose.fakeplug.yml`
runs the simulator as an always-on container (`restart: always`, survives crashes
and VM reboots). It reuses the `amphive_backend` image (which already has
paho-mqtt), joins the relay stack's `amphive-relay_default` network, and
reaches the broker as `mqtt:1883` with the `MQTT_GW_*` creds from
`~/amphive-relay/.env`. It is **test-only and not shipped by `deploy.ps1`** —
deploy it manually (commands in the compose file header). Currently deployed
on `amphive-relay` (the free-tier consolidated VM, see
[DEPLOYMENT.md](DEPLOYMENT.md)) as container `amphive-fake-plug` driving
gateway `fakeplug-gw-01` / plug 2 — now roster-capable, so adding a 2nd/3rd
plug to that gateway via the CPO UI/API works without touching the compose
file. Manage it with:

```bash
sudo docker logs -f amphive-fake-plug                                    # tail
cd ~/amphive-relay && sudo docker-compose -p fakeplug -f docker-compose.fakeplug.yml down   # stop
```

## 2. Coverage assessment

Backend line coverage measured 2026-07-21 (`pytest backend --cov=backend
--cov-report=term-missing`, DB-gated tests skipping locally): **68.39%**,
against the CI gate's `--cov-fail-under=66` (see "The CI gate" in §1 above).
CI itself runs higher than this local baseline since its `postgres:15`
service lets the DB-gated tests in §1 run too.

| Area | Coverage | Notes |
|------|:--------:|-----------------------|
| Payments (amount authority, idempotency) | 🟢 good | `fetch_captured_payment` amount authority + order-mismatch (unit); registration/topup concurrency has dedicated races-tests (`test_registration_races.py`, `test_wallet.py`) |
| Auth / JWT / RBAC | 🟢 good | `require_role` gates (`test_direct_rbac.py`, `test_admin_router.py`), `token_version` revocation (`test_token_revocation.py`), rate limiting, password reset |
| Sessions (start/stop billing, row locks) | 🟢 good | Wallet debit + auth-hold capping (DB-gated), session limits/reaper, `test_max_active_sessions.py`; concurrency covered per-module rather than one end-to-end TOCTOU suite |
| Pricing / tariffs (incl. time-of-day) | 🟢 good | Resolution chain + Pricing v2 segmented billing (DB-gated), N+1-free batch lookups |
| Telemetry persistence flush | 🟡 partial | Buffer bounding/drop-counting is unit-tested; full tenant/session enrichment flush path is still DB-gated-only, not exercised for every edge case |
| Socket.io streaming | 🟢 good | Connect auth, unauthorized subscribe, `get_participants` regression |
| CPO analytics / gap endpoints | 🟢 good | `test_cpo_gap_endpoints.py`, `test_driver_gap_endpoints.py` cover the redesign-v3 contract surface |
| Frontend | 🟢 good | ~570 Vitest tests across 62 files — contexts, protected routes, host-split routing, every CPO/admin page, pure-function utils |
| Firmware | ❌ none (host) | Still no host-compilable unit tests — watchdog limits, KLAP request/retry, offline ring buffer are on-device-only |

**Overall: solid on backend money/auth/pricing paths and on frontend
component behavior; the firmware gap from the original audit is unchanged.**

---

## 3. Testing roadmap (highest value first)

*Phases 1–4 below were written 2026-07-05, when the backend had 9 test files
and the frontend had none. All four are now done — kept here (struck
through) as a record of what was prioritized and closed, not as open work.*

### Phase 1 — Protect the money path (P0)
1. ~~**Session billing integration test** (throwaway Postgres): start → feed
   telemetry → stop; assert `coins_spent`, wallet debit, and ledger row are
   consistent, and that `balance_after` == actual balance (catches the
   `max(0, …)` clamp inconsistency).~~ **Done** — `test_session_limits.py`'s
   finalize/hold tests (e.g. `test_finalize_bills_full_accrued_cost_with_no_forgiven_overage_after_blocked_patch`)
   and `test_auth_holds.py` cover this end-to-end, DB-gated.
2. ~~**Payment concurrency test**: fire `/verify` and the webhook for the same
   `razorpay_payment_id` concurrently; assert exactly one credit (UNIQUE guard).~~
   **Done** — `test_wallet.py::test_credit_topup_duplicate_rolls_back_the_balance_bump`
   + `test_concurrent_credit_and_debit_serialize`.
3. ~~**Plug-claim TOCTOU test**: two concurrent `/sessions/start` on one plug;
   assert exactly one wins with 409 for the other.~~ **Done** —
   `test_max_active_sessions.py` (`test_start_rejected_at_cap`) and
   `test_session_start_plug_status.py` cover the admission-gate side of this.

### Phase 2 — Auth & access control (P1)
4. ~~RBAC matrix: driver hitting `/api/cpo/*` → 403; cpo → 200; cross-tenant plug
   access → 404.~~ **Done** — `test_admin_router.py::test_admin_gate_rejects_non_admins`,
   `test_direct_rbac.py`, and the cross-tenant 404s in `test_cpo_gap_endpoints.py`.
5. ~~JWT: expired token → 401; ephemeral-key fallback logs critical and still
   signs; SSE `?token=` ownership check.~~ **Done** — `test_token_revocation.py`
   covers the `token_version` epoch (stale/legacy/logout-bump); rate limiting
   in `test_rate_limiting.py`.

### Phase 3 — Analytics & telemetry (P1/P2)
6. ~~Un-skip `test_flush_enriches_and_inserts` against a PG container (tenant_id /
   session_id resolution, unknown-plug skip).~~ **Done** — folded into
   `test_telemetry_persistence.py`.
7. ~~CPO analytics: seed sessions across days, assert revenue/energy/telemetry
   bucket shapes and tenant isolation.~~ **Done** — `test_cpo_gap_endpoints.py`
   (`test_analytics_sessions_server_side_totals_not_page_sums` and friends).

### Phase 4 — Frontend (P2)
8. ~~Add Vitest + React Testing Library. Cover: `AuthContext` restore/login/logout,
   `TopUp` handler (verify called without amount), `ProtectedRoute`/`CpoProtectedRoute`.~~
   **Done 2026-07-07** (20 tests / 4 suites at the time) — **grown to ~570
   tests across 62 files by 2026-07-26** as the v3 redesign added the CPO
   console, the platform-admin console, and host-partitioned routing, each
   with its own co-located suite. See §1 above.

### Phase 5 — Firmware (P2/P3, still open)
9. Extract watchdog + offline-ring-buffer logic behind host-compilable units and
   test limit-tripping and ring wrap/overwrite on the host (no hardware).

### Cross-cutting — CI (P1, do alongside Phase 1)
10. ~~GitHub Actions: `pytest backend/tests` (with a `postgres:15` service) +
    `npm run lint`.~~ **Done 2026-07-07, since grown well past the original
    scope** — the gate now also runs `ruff check`, `mypy`, and a
    `--cov-fail-under=66` coverage floor on the backend job, plus a
    dedicated `frontend-tests` job for `npm test`. See "The CI gate" in §1.

---

## 4. Conventions for new tests
- Keep unit tests DB-free where possible (mock the SDK / session factory), as the
  existing suite does.
- Integration tests that need Postgres must spin up a **throwaway** container and
  never touch the VM or a shared DB.
- Every bug fix should ship with a regression test (as the Socket.io and payment
  fixes did) — that is the standard this repo has already set.
