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
- [ ] **Add a backend session reaper**: periodic task that auto-stops ACTIVE
      sessions with no telemetry for N minutes (bill persisted energy). Today the
      only timeout lives in the firmware — useless if the gateway never got the
      ON or died mid-session.
- [ ] **Decide one-active-session-per-user** (enforce at start) or make
      `/api/sessions/active` + the UI handle several; today older active
      sessions are unreachable/un-stoppable by the user.
- [x] **Map duplicate-registration race to 400** (2026-07-06):
      `/api/auth/register` and `/api/cpo/setup` caught only the sequential
      duplicate via exists-check; the concurrent one escaped as a raw
      `IntegrityError` 500. Both now catch it at commit/flush and return the
      same 400 as the sequential path. Tests:
      `backend/tests/test_registration_races.py`. (IMPL §3.41)
- [ ] **Set `TELEMETRY_RETENTION_DAYS` in prod** — default `0` keeps telemetry
      forever; `telemetry_readings` grows ~1 row/s during live streaming.
- [ ] **Add CI** (GitHub Actions): `pytest backend/tests` with a `postgres:15`
      service + `npm run lint`. Would have caught the `get_participants`
      "verified-but-broken" bug. (TD#13)
- [ ] **Adopt Alembic** and retire the hand-written `_INPLACE_UPGRADES` +
      unexecuted `schema.sql`/`schema_v2.sql`. (TD#5)
- [ ] **Split `backend/main.py`** (2,291 lines) into `routers/` + `schemas/`;
      keep the lifespan in `main.py`. (TD#7)
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

- [ ] **Kill N+1 queries** in `get_available_plugs`, `cpo_list_plugs`,
      `cpo_analytics_sessions`, `get_my_groups` via JOIN/`selectinload`. (TD#9)
- [ ] **Extract the access-code generator** (duplicated 3× across CPO routes). (TD#10)
- [ ] **Unify live telemetry**: retire the SSE endpoint + `sse-starlette` dep,
      or document it as an explicit fallback. (TD#12)
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

- [ ] **MQTT broker auth + TLS** (needs a firmware credentials field before
      `allow_anonymous false`). (SEC §3)
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
