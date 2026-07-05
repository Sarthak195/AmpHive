# AmpHive — Prioritized Improvement Roadmap

*Audit 2026-07-05 @ `78cffeb`. Ordered by (highest impact ÷ lowest effort).
Cross-references [TECH_DEBT.md](TECH_DEBT.md) items as `TD#n` and
[SECURITY.md](SECURITY.md).*

---

## Immediate (this week) — security & correctness

- [x] **Commit the `tools/` secret strip.** Done (`3e20dbd`, 2026-07-05) — all
      five helpers now read `TAPO_EMAIL` / `TAPO_PASSWORD` / `TAPO_PLUG_IP` from
      env and fail closed; no real secret remains in HEAD. (TD#1)
- [ ] **Rotate every burned secret**: WireGuard keypair, DuckDNS token, DB
      password (**Tapo password rotated 2026-07-05**). They remain in git
      history regardless of HEAD. (TD#2, SEC §1)
- [ ] **Take MQTT off the public internet**: set `MQTT_BIND_IP=<overlay IP>` in
      the VM `.env`, redeploy, then delete the GCP firewall rule for tcp:1883. (TD#3, SEC §3)
- [ ] **Lock CORS** to the known frontend origin(s); drop the `["*"]` +
      `allow_credentials=True` combo. (TD#4, `main.py:185`)
- [ ] **Fix the `stop_charging_session` ledger inconsistency**: when
      `final_cost > balance`, the `max(0, …)` clamp forgives debt but writes a
      ledger `amount = -final_cost` with `balance_after = 0` — the ledger no
      longer reconciles. Debit only what's available (or block on insufficient
      funds) and make `amount` match the balance delta. (`main.py:784`)

## Next week — reliability & structure

- [ ] **Add CI** (GitHub Actions): `pytest backend/tests` with a `postgres:15`
      service + `npm run lint`. Would have caught the `get_participants`
      "verified-but-broken" bug. (TD#13)
- [ ] **Adopt Alembic** and retire the hand-written `_INPLACE_UPGRADES` +
      unexecuted `schema.sql`/`schema_v2.sql`. (TD#5)
- [ ] **Split `backend/main.py`** (2,291 lines) into `routers/` + `schemas/`;
      keep the lifespan in `main.py`. (TD#7)
- [ ] **Money → `Numeric(12,2)`** for `coin_balance`, `coins_spent`, ledger
      `amount`/`balance_after`; add a DB-level non-negative-balance check. (TD#6, SEC §5)
- [ ] **Unique `razorpay_payment_id`** is in place — confirm the pre-lock SELECT
      is backed by it under load, then drop the redundant SELECT if desired. (SEC checklist)
- [ ] **Persist the firmware energy integrator** to NVS so crash recovery keeps
      the energy watchdog armed. (TD#19)

## Next month — scale & polish

- [ ] **Kill N+1 queries** in `get_available_plugs`, `cpo_list_plugs`,
      `cpo_analytics_sessions`, `get_my_groups` via JOIN/`selectinload`. (TD#9)
- [ ] **Extract the access-code generator** (duplicated 3× across CPO routes). (TD#10)
- [ ] **Unify live telemetry**: retire the SSE endpoint + `sse-starlette` dep,
      or document it as an explicit fallback. (TD#12)
- [ ] **cJSON on the firmware command path** instead of `strstr`/`sscanf`; raise
      the 128-byte MQTT topic/data buffers so a `session_id` can't truncate. (TD#11)
- [ ] **Model plug geolocation** (lat/long on `Plug`) so the map stops using
      random fallback coordinates. (TD#17)
- [ ] **Decide TypeScript**: adopt it (all app code is `.jsx` despite the TS
      toolchain + `@types/*`) or remove the toolchain. (TD#14)
- [ ] **Fix `CpoSetup` redirect-during-render** (`useEffect`/`<Navigate>`). (TD#18)
- [ ] **Add frontend tests** (Vitest + RTL) for auth, payment handler, protected
      routes. (TESTING Phase 4)

## Long term — productionization

- [ ] **MQTT broker auth + TLS** (needs a firmware credentials field before
      `allow_anonymous false`). (SEC §3)
- [ ] **Finish Path A end-to-end**: real billed session on physical hardware over
      ESP32+MQTT, feeding the session/telemetry pipeline. (IMPL §2)
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
