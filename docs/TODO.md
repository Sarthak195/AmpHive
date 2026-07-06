# AmpHive — Prioritized Improvement Roadmap

*Audit 2026-07-05 @ `78cffeb`. Ordered by (highest impact ÷ lowest effort).
Cross-references [TECH_DEBT.md](TECH_DEBT.md) items as `TD#n` and
[SECURITY.md](SECURITY.md).*

*Updated 2026-07-06 (follow-up audit): new items are tagged **[2026-07-06]** and
carry their `TD#`/`SEC §` cross-refs.*

---

## Immediate (this week) — security & correctness

- [ ] **[2026-07-06] Lock down the ESP32 provisioning portal** — the setup AP is
      open (`WIFI_AUTH_OPEN`) and `/save` is unauthenticated, so anyone in Wi-Fi
      range can sniff Tapo/Wi-Fi/overlay secrets and overwrite config. Add WPA2 +
      a setup PIN/token + timeout. (SEC §8.1, **CRITICAL**)
- [ ] **[2026-07-06] Reject session starts on OFFLINE/MAINTENANCE plugs** —
      `start_charging_session` blocks only `OCCUPIED`, so a plug pins OCCUPIED with
      no charge (bills 0) and maintenance plugs stay usable. Require `AVAILABLE`.
      (TD#22, `backend/main.py:697`)
- [ ] **[2026-07-06] Consume `+/alarms`** on the backend — the firmware's
      THERMAL/OVERCURRENT cutoffs are currently dropped (no record, no alert).
      (TD#21, SEC §3)

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
- [x] **Persist the firmware energy integrator** to NVS (2026-07-06, code):
      `s_energy_wh` is restored on `tapo_init` and written throttled (per 50 Wh),
      so crash recovery keeps the energy watchdog armed. **Pending on-device flash
      + verification** (no ESP-IDF toolchain on the dev box). (TD#19)
- [ ] **[2026-07-06] Guard telemetry ingestion** — wrap the `float(...)` casts in
      `_handle_gateway_telemetry` and validate `plug.gateway_id` against the topic
      gateway before billing. (TD#25, SEC §3/§8.5)
- [ ] **[2026-07-06] Gateway staleness sweep** — mark a gateway OFFLINE when
      `last_seen_at` goes stale, not only on its LWT, so dashboards reflect reality.
      (TD#27)
- [ ] **[2026-07-06] CPO admin audit log** — record gateway/plug/group
      create-delete, status changes, and access-code regen. (TD#26)
- [ ] **[2026-07-06] Fix crash-recovery duration watchdog** — the recovered
      session resets its start time each reboot, so the time cap restarts from
      zero (energy cap still holds). Needs an SNTP wall-clock baseline. (TD#23)

## Next month — scale & polish

- [ ] **Kill N+1 queries** in `get_available_plugs`, `cpo_list_plugs`,
      `cpo_analytics_sessions`, `get_my_groups` via JOIN/`selectinload`. (TD#9)
- [ ] **Extract the access-code generator** (duplicated 3× across CPO routes). (TD#10)
- [ ] **Unify live telemetry**: retire the SSE endpoint + `sse-starlette` dep,
      or document it as an explicit fallback. (TD#12)
- [x] **cJSON on the firmware command path** (2026-07-06, code) instead of
      `strstr`/`sscanf`; MQTT buffers raised (topic 256, data 512) with an
      oversized/fragmented guard so a `session_id` can't truncate. **Pending
      on-device flash + verification.** (TD#11)
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
- [ ] **[2026-07-06] Structured logging + correlation ids** across backend
      (JSON, request ids) and a firmware log topic for field diagnostics;
      persist the broker log. (TD#28)
- [ ] **[2026-07-06] Unified wallet ledger view** — show TOPUP credits alongside
      session debits (History shows debits only). (TD#29)
- [ ] **[2026-07-06] Low-balance UX** — warn before/at start and auto-stop before
      the wallet is exhausted, instead of silently forgiving the shortfall.
- [ ] **[2026-07-06] Registration validation** — `EmailStr` + a password-strength
      rule. (TD#30)
- [ ] **[2026-07-06] Portal input CSS + reachability test** — fix `width:100%%`
      and test Wi-Fi/plug reachability before saving config (onboarding). (TD#31)

## Long term — productionization

- [ ] **[2026-07-06] Multi-plug gateway support** — the firmware drives a single
      plug: `main.c` has one `target_plug_ip`/`active_session` and
      `tapo_protocol.c` one global KLAP session + energy integrator, so a command
      for plug B toggles plug A and telemetry is misattributed. Needs a per-plug
      state table + an instance-based KLAP driver, and (recommended) the target
      `local_ip` carried in the `ON` payload. Keep broker ACLs per-gateway.
      (TD#20, SEC §8.5)
- [ ] **[2026-07-06] Device security hardening** — flash encryption + Secure Boot
      v2 (secrets are plaintext-extractable from NVS), ephemeral/tagged overlay
      keys, and button-hold provisioning instead of the boot-time open portal.
      (SEC §8.2/§8.3/§8.4)
- [ ] **[2026-07-06] Notifications** — session start/stop, low balance, plug
      offline, and safety cutoffs (once `+/alarms` is consumed, TD#21).
- [ ] **[2026-07-06] Shorter-lived JWTs / revocation + auth rate limiting**
      (currently 7-day, no blacklist; no login/register throttle). (SEC §8.6)
- [ ] **MQTT broker auth + TLS** (needs a firmware credentials field before
      `allow_anonymous false`). (SEC §3, SEC §8.3)
- [~] **Finish Path A end-to-end**: real billed session on physical hardware over
      ESP32+MQTT, feeding the session/telemetry pipeline. **Achieved 2026-07-06** —
      a real ESP32 + P110 ran a billed session; the plug delivered correct energy
      and telemetry flowed through to the wallet debit. The run surfaced (and we
      fixed in code) a **session overbilling bug**: firmware published its lifetime
      energy meter instead of session-relative energy, so every session after the
      first re-billed the plug's history. **Blocking to close:** reflash the ESP32
      with the fix (no ESP-IDF toolchain on the dev box) and re-verify one clean
      billed session bills only its own kWh. (IMPL §2, §3.35)
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
