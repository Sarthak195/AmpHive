# AmpHive — Testing Guide & Roadmap

*Audit 2026-07-05. Current state of automated testing across the stack, plus a
prioritized roadmap to close the biggest coverage gaps.*

---

## 1. What exists today

### Backend (`backend/tests/`, pytest + pytest-asyncio)
| File | Covers | Kind |
|------|--------|------|
| `test_payments.py` | `fetch_captured_payment()` — amount comes from Razorpay not the client; order-mismatch rejected; unconfigured/API-error paths | Unit (mocked SDK) |
| `test_socketio.py` | Socket.io `connect` auth (token in auth vs query), unauthorized `subscribe_session`, success path, and a **regression test** for the `get_participants` bug (real task, real `sio`, stubbed emit) | Unit + light integration |
| `test_telemetry_persistence.py` | MQTT handler forwards voltage/current/status into the buffer; buffer is bounded and counts drops; flush/enrich path is a **skipped** outline needing real Postgres | Unit |
| `test_active_session_speedup.py` | **Regression** for the login/`/me` `MultipleResultsFound` crash: `check_and_speed_up_active_session` must handle a user with 0/1/many ACTIVE sessions (mock result mirrors SQLAlchemy's raise-on-many semantics) | Unit |
| `test_gateway_liveness.py` | Session-start liveness gate: `gateway_is_live` matrix (offline/stale/fresh/naive-legacy/missing `last_seen_at`) + `MQTTManager` telemetry-driven `last_seen_at` refresh and its 1/min throttle | Unit |
| `test_registration_races.py` | **Regression** for duplicate-insert races: `/api/auth/register` + `/api/cpo/setup` must map a concurrent-duplicate `IntegrityError` to the same 400 as the sequential path (and roll back) | Unit |
| `test_session_reaper.py` | Reaper sweep logic: every stale id finalized with the reap reason, user-stop races not double-counted, one failure doesn't abort the sweep, staleness query COALESCEs `last_telemetry_at`/`started_at` | Unit |
| `test_reconnect_off_republish.py` | Gateway `online` status republishes OFF (no-wait) to its plugs without an ACTIVE session; plugs with a live session untouched; `offline` and no-plug cases are no-ops | Unit |
| `test_migrations.py` | Alembic: `upgrade head` on an empty DB builds a schema matching the models (drift check via `compare_metadata`), and a pre-Alembic DB gets stamped, not re-migrated. **CI-only** (needs `TEST_DATABASE_URL` → postgres service); skipped locally | Integration (CI PG) |

Run:
```bash
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest backend/tests
```
> Per repo policy, run tests against a throwaway DB / in CI — **never** against
> the VM or a real database (see [AGENTS.md](../AGENTS.md)).

### Frontend
No tests. No test runner configured (`package.json` has `lint` but no `test`).

### Firmware
No host-side unit tests. Validation is on-device via serial monitor
(`tools/read_serial.py`) and the throwaway `tools/klap_probe.py` KLAP probe.

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

### Fake plug simulator (`tools/fake_plug.py`, added 2026-07-08)

A software stand-in for an ESP32 gateway + Tapo P110 so the **whole stack**
(session start/stop, wallet billing, live Socket.io telemetry, the driver/CPO
UI) can be exercised **without physical hardware**. It speaks the exact MQTT
contract in [MQTT_CONTRACT.md](MQTT_CONTRACT.md): retained `online` status +
`offline` LWT, subscribes to the per-plug command topic (honours
`ON`/`OFF`/`SET_INTERVAL`; `OTA` is a no-op), and publishes telemetry every
`--interval` seconds. While a session is `ON` it draws a **constant load**
(default **10 kW**, `--watts`) and integrates session-relative energy just like
the firmware — so the live cost and wallet debit tick up predictably
(~0.167 kWh/min ≈ 0.83 coins/min at `COINS_PER_KWH=5`). Registration goes
through the public CPO API, never the DB.

**Where it runs.** The broker is overlay-bound (`MQTT_BIND_IP`, not public), so
the MQTT loop must run where it can reach the broker — the **GCP VM host**
(`--broker-host 100.87.241.70`, the VM's own overlay IP) or **inside the compose
network** (`--broker-host mqtt`). The API registration step works from anywhere.

```bash
# 1. One-time: create the fake gateway + plug (idempotent). Runs from anywhere.
python tools/fake_plug.py --register-only \
    --cpo-email cpo@amphive.test --cpo-password '<cpo-pw>'
#   -> prints:  gateway=fakeplug-gw-01  plug_id=<N>  (group 1 = public)

# 2. Keep the fake plug live (run ON THE VM; needs paho-mqtt). Ctrl-C stops it.
pip install paho-mqtt
python tools/fake_plug.py --run-only --plug-id <N> \
    --gateway-id fakeplug-gw-01 --broker-host 100.87.241.70 \
    --broker-user amphive-gateway --broker-pass '<MQTT_GW_PASSWORD>'
```

The gateway goes stale after `GATEWAY_LIVENESS_WINDOW_SEC` (120 s) once the
simulator stops, so keep it running while you test (a session won't *start*
against a stale gateway). `--self-test` prints the wire payloads with no network
for a quick contract check. Broker creds default to `MQTT_GW_*`/`MQTT_*` env
vars; the fake plug uses the gateway MQTT account, not the backend's.

**Always-on deployment (running in prod since 2026-07-08).** For a fake plug
that stays live without a terminal open, `deploy/docker/docker-compose.fakeplug.yml`
runs the simulator as an always-on container (`restart: always`, survives crashes
and VM reboots). It reuses the `amphive_backend` image (which already has
paho-mqtt), joins the stack's `amphive_default` network, and reaches the broker
as `mqtt:1883` with the `MQTT_GW_*` creds from `~/amphive/.env`. It is **test-only
and not shipped by `deploy.ps1`** — deploy it manually (commands in the compose
file header). Currently deployed on `amphive-vm-in` as container
`amphive-fake-plug` driving gateway `fakeplug-gw-01` / plug 2. Manage it with:

```bash
sudo docker logs -f amphive-fake-plug                                    # tail
cd ~/amphive && sudo docker-compose -p fakeplug -f docker-compose.fakeplug.yml down   # stop
```

## 2. Coverage assessment

| Area | Coverage | Biggest untested risk |
|------|:--------:|-----------------------|
| Payments (amount authority, idempotency) | 🟡 partial | The **route** `/api/payments/verify` + `_credit_topup` concurrency (only the helper is unit-tested) |
| Auth / JWT / RBAC | ❌ none | `require_role` gating, ephemeral-key fallback, token expiry |
| Sessions (start/stop billing, row locks) | ❌ none | The money path: wallet debit, `max(live, persisted)` energy, plug-claim TOCTOU |
| Telemetry persistence flush | 🟡 outline only | tenant/session enrichment, unknown-plug skip |
| Socket.io streaming | 🟡 good | reconnect / multi-listener refcount |
| CPO analytics endpoints | ❌ none | `date_trunc` bucketing, tenant scoping |
| Frontend | ❌ none | payment handler, session lifecycle, protected routes |
| Firmware | ❌ none (host) | watchdog limits, KLAP request/retry, offline ring buffer |

**Overall: low.** The tests that exist are high-quality and target the two
most recently-fixed bugs, but the core money and session flows — the things that
must not break — have no automated coverage.

---

## 3. Testing roadmap (highest value first)

### Phase 1 — Protect the money path (P0)
1. **Session billing integration test** (throwaway Postgres): start → feed
   telemetry → stop; assert `coins_spent`, wallet debit, and ledger row are
   consistent, and that `balance_after` == actual balance (catches the
   `max(0, …)` clamp inconsistency).
2. **Payment concurrency test**: fire `/verify` and the webhook for the same
   `razorpay_payment_id` concurrently; assert exactly one credit (UNIQUE guard).
3. **Plug-claim TOCTOU test**: two concurrent `/sessions/start` on one plug;
   assert exactly one wins with 409 for the other.

### Phase 2 — Auth & access control (P1)
4. RBAC matrix: driver hitting `/api/cpo/*` → 403; cpo → 200; cross-tenant plug
   access → 404.
5. JWT: expired token → 401; ephemeral-key fallback logs critical and still
   signs; SSE `?token=` ownership check.

### Phase 3 — Analytics & telemetry (P1/P2)
6. Un-skip `test_flush_enriches_and_inserts` against a PG container (tenant_id /
   session_id resolution, unknown-plug skip).
7. CPO analytics: seed sessions across days, assert revenue/energy/telemetry
   bucket shapes and tenant isolation.

### Phase 4 — Frontend (P2)
8. ~~Add Vitest + React Testing Library. Cover: `AuthContext` restore/login/logout,
   `TopUp` handler (verify called without amount), `ProtectedRoute`/`CpoProtectedRoute`.~~
   **Done 2026-07-07** — 20 tests in 4 suites (`src/**/*.test.jsx`): all of
   the above plus the multi-session `SessionContext`
   (restore-all/switch/start/stop). The guards were extracted from `App.jsx`
   into `components/ProtectedRoutes.jsx` to be testable. Setup:
   `vite.config.js` `test` block (jsdom; `src/test-setup.js` registers
   jest-dom + RTL cleanup); `npm test` runs in CI alongside lint.

### Phase 5 — Firmware (P2/P3)
9. Extract watchdog + offline-ring-buffer logic behind host-compilable units and
   test limit-tripping and ring wrap/overwrite on the host (no hardware).

### Cross-cutting — CI (P1, do alongside Phase 1)
10. ~~GitHub Actions: `pytest backend/tests` (with a `postgres:15` service) +
    `npm run lint`.~~ **Done 2026-07-07** — `.github/workflows/ci.yml` runs
    both on push/PR; the postgres service is provisioned and waiting for the
    Phase-1 integration tests (exported as `TEST_DATABASE_URL`).

---

## 4. Conventions for new tests
- Keep unit tests DB-free where possible (mock the SDK / session factory), as the
  existing suite does.
- Integration tests that need Postgres must spin up a **throwaway** container and
  never touch the VM or a shared DB.
- Every bug fix should ship with a regression test (as the Socket.io and payment
  fixes did) — that is the standard this repo has already set.
