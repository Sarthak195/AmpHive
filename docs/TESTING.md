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
8. Add Vitest + React Testing Library. Cover: `AuthContext` restore/login/logout,
   `TopUp` handler (verify called without amount), `ProtectedRoute`/`CpoProtectedRoute`.

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
