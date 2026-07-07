# AmpHive — Prioritized Improvement Roadmap

*Audit 2026-07-05 @ `78cffeb`. Ordered by (highest impact ÷ lowest effort).
Cross-references [TECH_DEBT.md](TECH_DEBT.md) items as `TD#n` and
[SECURITY.md](SECURITY.md).*

---

## Immediate (this week) — security & correctness

- [x] **Commit the `tools/` secret strip.** Done (`3e20dbd`, 2026-07-05) — all
      five helpers now read `TAPO_EMAIL` / `TAPO_PASSWORD` / `TAPO_PLUG_IP` from
      env and fail closed; no real secret remains in HEAD. (TD#1)
- [x] **Rotate every burned secret**: WireGuard keypair, DuckDNS token, Tapo &
      DB passwords — all rotated at the source 2026-07-06. Dead old values remain
      in git history (optional scrub). (TD#2, SEC §1)
- [x] **Take MQTT off the public internet**: broker bound to the overlay IP
      `100.87.241.70` and the GCP firewall restricted to tcp:80/8000 (1883
      dropped), 2026-07-06. Broker *auth* still pending. (TD#3, SEC §3)
- [x] **Lock CORS** to the known frontend origin(s): the wildcard is replaced by
      an explicit allowlist in `backend/main.py:187` (localhost, `amphive.duckdns.org`,
      VM IP; http+https). Committed and **deployed** 2026-07-06 (verified in prod:
      allowed origin echoed, evil origin gets no ACAO header). (TD#4)
- [x] **Fix the `stop_charging_session` ledger inconsistency** (2026-07-06):
      previously, when `final_cost > balance`, the `max(0, …)` clamp forgave debt
      but still wrote a ledger `amount = -final_cost` with `balance_after = 0`, so
      the ledger no longer reconciled (running balance couldn't be derived by
      summing `amount`). Now debits `actual_debit = min(final_cost, balance)`, and
      the ledger `amount`, `balance_after`, `session.coins_spent`, and the API
      response all use that same delta. Any forgiven shortfall is noted in the
      ledger description and logged as a warning. (`backend/main.py:797`)

## Next week — reliability & structure

- [x] **Fix the login/`/me` `MultipleResultsFound` crash** (2026-07-06):
      `check_and_speed_up_active_session` assumed at most one ACTIVE session per
      user and 500'd login/session-restore for users with more (observed in prod
      with 3). Now iterates all; regression tests in
      `backend/tests/test_active_session_speedup.py`. (IMPL §3.39)
- [x] **Reject session start when the gateway is offline** (2026-07-06):
      `/api/sessions/start` now 409s unless the gateway is *live* —
      `gateway_is_live` requires status ONLINE **and** `last_seen_at` within
      `GATEWAY_LIVENESS_WINDOW_SEC` (env, default 120 s). Telemetry now
      refreshes `last_seen_at` (throttled 1/min per gateway in `MQTTManager`),
      so long-connected gateways stay live without reconnecting; the model's
      misleading `onupdate=now` hook is removed. Tests:
      `backend/tests/test_gateway_liveness.py`. (IMPL §3.40)
- [x] **Add a backend session reaper** (2026-07-06): lifespan-owned
      `SessionReaperService` sweeps every `SESSION_REAPER_INTERVAL_SEC` (60 s)
      and finalizes ACTIVE sessions with no telemetry for
      `SESSION_STALE_TIMEOUT_SEC` (300 s; `COALESCE(last_telemetry_at,
      started_at)`). Reaping goes through the same `finalize_charging_session`
      path as a user stop — which now **locks the session row** and re-checks
      ACTIVE, also closing the pre-existing double-stop double-debit race.
      Tests: `backend/tests/test_session_reaper.py`. (IMPL §3.42)
- [ ] **Decide one-active-session-per-user** (enforce at start) or make
      `/api/sessions/active` + the UI handle several; today older active
      sessions are unreachable/un-stoppable by the user.
- [x] **Map duplicate-registration race to 400** (2026-07-06):
      `/api/auth/register` and `/api/cpo/setup` caught only the sequential
      duplicate via exists-check; the concurrent one escaped as a raw
      `IntegrityError` 500. Both now catch it at commit/flush and return the
      same 400 as the sequential path. Tests:
      `backend/tests/test_registration_races.py`. (IMPL §3.41)
- [x] **Republish OFF when a gateway reconnects with no ACTIVE session on its
      plug** (2026-07-07). Observed on-device: the ESP32 lost power mid-session,
      the reaper finalized the session, and on reboot the firmware's NVS crash
      recovery resumed it — relay back ON, telemetry `occupied`, nobody billing.
      `_persist_gateway_status` now re-sends OFF (best-effort, no-wait) to each
      of the gateway's plugs without an ACTIVE session whenever it reports
      `online`. Tests: `backend/tests/test_reconnect_off_republish.py`.
      (IMPL §3.43)
- [x] **Add CI** (2026-07-07): `.github/workflows/ci.yml` — `pytest
      backend/tests` (python 3.11, postgres:15 service provisioned for the
      planned Phase-1 integration tests) + `npm run lint`. Runs on push to
      main and on PRs; both jobs verified green locally before committing.
      (TD#13)
- [x] **Adopt Alembic** (2026-07-07): `backend/migrations/` with a frozen-DDL
      baseline (`0001_baseline`, compiled from the models — captures
      everything the retired `_INPLACE_UPGRADES` produced); startup
      (`db.py:init_db`) stamps pre-Alembic databases at the baseline then
      runs `upgrade head`; `schema.sql`/`schema_v2.sql` deleted. CI proves
      the baseline == models on the postgres service
      (`backend/tests/test_migrations.py`). Future schema changes ship as
      revisions: `alembic -c backend/alembic.ini revision --autogenerate`
      (needs a reachable DB — CI or the VM). (TD#5)
- [x] **Split `backend/main.py`** (2026-07-07): 2,384 → 221 lines. Routes
      moved verbatim to `backend/routers/{auth,groups,plugs,sessions,payments,
      direct,cpo}.py`; schemas to `backend/schemas.py`; runtime handles to
      `backend/state.py` (attribute access — survives the lifespan rebind);
      session helpers to `services/session_lifecycle.py`. OpenAPI parity
      verified: 36 operations before and after. (TD#7)
- [x] **Money → `Numeric(12,2)`** (2026-07-06) for `coin_balance`, `coins_spent`,
      ledger `amount`/`balance_after`. Columns migrated in place via a guarded
      `ALTER … TYPE NUMERIC(12,2)` in `db.py:_INPLACE_UPGRADES`; all wallet math now
      goes through `services/money.to_money` (Decimal, half-up 2 dp), eliminating
      float drift. Energy/power stay `Float` (measurements). **Still open:** a
      DB-level non-negative-balance CHECK constraint. (TD#6, SEC §5)
- [x] **Unique `razorpay_payment_id`** is in place and enforced — `db.py`
      creates `uq_ledger_razorpay_payment_id`, and `_credit_topup` relies on the
      `IntegrityError` from a concurrent insert (not just the pre-lock SELECT) to
      stay idempotent across the /verify + webhook race. (SEC checklist)
- [x] **Persist the firmware energy integrator** to NVS (2026-07-06):
      `s_energy_wh` is restored on `tapo_init` and written throttled (per 50 Wh),
      so crash recovery keeps the energy watchdog armed. **Flashed on-device
      2026-07-06** (ESP-IDF v5.3.3 now installed at `C:\esp\v5.3.3`); the
      cross-reboot restore hasn't been explicitly exercised yet — needs ≥ 50 Wh
      accrued to hit the throttled write. (TD#19)

## Next month — scale & polish

- [x] **Kill N+1 queries** (2026-07-07) in `get_available_plugs`,
      `cpo_list_plugs`, `cpo_analytics_sessions`, `get_my_groups` — each is a
      single JOINed statement now; driver endpoints verified byte-identical
      against prod before/after. (TD#9)
- [ ] **Extract the access-code generator** (duplicated 3× across CPO routes). (TD#10)
- [x] **Unify live telemetry** (2026-07-07): the legacy SSE endpoint
      (`/api/sessions/live/{id}`) and the `sse-starlette` dep are retired —
      the frontend has been Socket.io-only since 2026-07-04 and had zero
      references to it. (TD#12)
- [x] **Set `TELEMETRY_RETENTION_DAYS` in prod** (2026-07-07): 90 days, wired
      through `.env` → compose → `telemetry_persistence` (prune runs ~hourly
      in the flush loop).
- [x] **cJSON on the firmware command path** (2026-07-06) instead of
      `strstr`/`sscanf`; MQTT buffers raised (topic 256, data 512) with an
      oversized/fragmented guard so a `session_id` can't truncate. **Flashed +
      verified on-device 2026-07-06** — ON commands carrying `session_id` parsed
      correctly through E2E sessions #77–79. (TD#11)
- [x] **Model plug geolocation** (2026-07-06): `Plug` now has nullable
      `latitude`/`longitude`; the driver/plug APIs return effective coords (the
      plug's own, else its gateway's site), and CPOs can set them via
      create/update. `MapComponent` plots only plugs with real coords and no
      longer uses `Math.random()` (which also moved markers on every re-render).
      (TD#17)
- [ ] **Decide TypeScript**: adopt it (all app code is `.jsx` despite the TS
      toolchain + `@types/*`) or remove the toolchain. (TD#14)
- [x] **Fix `CpoSetup` redirect-during-render** (2026-07-06): the render-body
      `navigate()` is replaced with a declarative `<Navigate to=… replace />`. (TD#18)
- [ ] **Add frontend tests** (Vitest + RTL) for auth, payment handler, protected
      routes. (TESTING Phase 4)

## Long term — productionization

- [x] **MQTT broker auth** — **enforced 2026-07-07** (SEC §3):
      `allow_anonymous false` + passwd file generated by `deploy.ps1` from
      `.env` (backend + gateway-fleet accounts), authenticated compose
      healthcheck, backend env plumbed, firmware credentials in NVS
      (`mqtt_user`/`mqtt_pwd`, optional portal fields) seeded + flashed on the
      dev gateway. Verified in prod: anonymous → `not authorised`; backend +
      ESP32 authenticate and telemetry flows. **TLS remains open** (overlay
      provides confidentiality today). (SEC §3)
- [x] **Finish Path A end-to-end**: real billed session on physical hardware over
      ESP32+MQTT, feeding the session/telemetry pipeline. **Achieved 2026-07-06** —
      a real ESP32 + P110 ran a billed session; the plug delivered correct energy
      and telemetry flowed through to the wallet debit. The run surfaced (and we
      fixed) a **session overbilling bug**: firmware published its lifetime energy
      meter instead of session-relative energy. **Closed later the same day:** the
      ESP32 was reflashed (ESP-IDF v5.3.3 toolchain installed) and consecutive
      billed sessions #77–79 verified each session's `kwh` starts at 0 and bills
      only its own energy; raw broker payloads confirmed the session-relative
      `kwh` + `session_id` echo. Note: the reflash also required refreshing the
      NVS `tapo_pwd` (the Tapo password rotation had stranded the gateway's
      provisioned copy → KLAP `handshake1 auth mismatch`). (IMPL §2, §3.35)
- [ ] **OTA firmware updates**: current single-app partition table precludes the
      spec'd dual-partition rollback. (IMPL 15)
- [ ] **Reconcile or retire K8s manifests** — they diverge from the live VM
      (stale images, missing secrets, in-cluster PG). (TD#15)
- [ ] **TimescaleDB** (hypertables/retention/continuous-aggregates) for
      `telemetry_readings` if telemetry volume grows. (IMPL 2)
- [ ] **Token revocation / shorter-lived JWTs** (currently 7-day, no blacklist).
- [ ] **Replace `frontend/README.md`** stock Vite template with real docs. (TD#16)

---

### Done recently (context, so these don't read as open)
RBAC enforced · unauthenticated provisioning removed · wallet updates row-locked
· webhook auto-credit idempotent · SSE authenticated · server-authoritative
payment amount · JWT known-default refusal · Socket.io `get_participants`
regression fixed · firmware KLAP v2 driver · NVS session persistence + offline
buffering · over-current/thermal cutoffs. See
[IMPLEMENTATION_STATUS.md §3](IMPLEMENTATION_STATUS.md) for the full list.
