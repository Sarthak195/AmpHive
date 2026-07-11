# AmpHive — Prioritized Improvement Roadmap

*Audit 2026-07-05 @ `78cffeb`. Ordered by (highest impact ÷ lowest effort).
Cross-references [TECH_DEBT.md](TECH_DEBT.md) items as `TD#n` and
[SECURITY.md](SECURITY.md).*

*Items tagged **[2026-07-06 audit]** come from the follow-up audit; only the
ones still open as of 2026-07-11 are listed (the audit's alarm-ingestion,
ledger-view, staleness, low-balance, and JWT-revocation findings had already
been fixed by the 2026-07-08…11 work — see the shipped sections below).*

---

## Immediate (this week) — security & correctness

- [ ] **[2026-07-06 audit] Lock down the ESP32 provisioning portal** — the setup
      AP is open (`WIFI_AUTH_OPEN`) and `/save` is unauthenticated, so anyone in
      Wi-Fi range can sniff Tapo/Wi-Fi/MQTT secrets or overwrite config. Add
      WPA2 + a setup PIN/token + timeout. (SEC §8.1, **CRITICAL**)
- [ ] **[2026-07-06 audit] Reject session starts on OFFLINE/MAINTENANCE plugs** —
      `start_charging_session` blocks only `OCCUPIED` plug status (the
      dead-gateway case is closed by the liveness 409), so a plug a CPO took out
      of service is still startable. Require `AVAILABLE`. (TD#22)
- [ ] **[2026-07-06 audit] Guard telemetry ingestion** — wrap the `float(...)`
      casts in `_handle_gateway_telemetry` and validate
      `plug.gateway_id == <topic gateway>` before billing (broker ACLs confine
      topics, not payload claims). (TD#25, SEC §3/§8.5)

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
- [x] **Decide one-active-session-per-user** (2026-07-07): **decided — allow
      up to 2 concurrent sessions per user** (`MAX_ACTIVE_SESSIONS_PER_USER`,
      env, default 2), enforced in `/api/sessions/start` under a user-row
      lock (concurrent starts serialize, so the cap can't be exceeded by a
      race). `/api/sessions/active` now returns **all** active sessions
      (top-level fields still mirror the newest for older clients), and the
      frontend lists every session (Home banners, Session-page switcher), so
      none is unreachable/un-stoppable anymore. Tests:
      `backend/tests/test_max_active_sessions.py`. (IMPL §3.45)
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
      float drift. Energy/power stay `Float` (measurements). The DB-level
      non-negative-balance CHECK landed 2026-07-07 as Alembic revision
      `0002_wallet_non_negative` — the first post-baseline revision (clamps
      legacy negatives to 0, then adds `ck_users_coin_balance_non_negative`).
      (TD#6, SEC §5)
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

- [ ] **[2026-07-06 audit] CPO admin audit log** — record gateway/plug/group
      create-delete, status changes, and access-code regen. (TD#26)
- [ ] **[2026-07-06 audit] Fix crash-recovery duration watchdog** — the
      recovered firmware session resets its start time each reboot, so the time
      cap restarts from zero (energy cap still holds). Needs an SNTP wall-clock
      baseline or persisted elapsed time. (TD#23)
- [ ] **[2026-07-06 audit] Stamp `session_id` into the offline ring buffer** —
      live telemetry is session-id-attributed now, but readings buffered across
      an MQTT outage still attach to the plug's current ACTIVE session on
      resync. (TD#24)
- [ ] **[2026-07-06 audit] Structured logging + correlation ids** across backend
      (JSON, request ids) and a firmware log topic for field diagnostics;
      persist the broker log. (TD#28)
- [ ] **[2026-07-06 audit] Registration validation** — `EmailStr` + a
      password-strength rule. (TD#30)
- [ ] **[2026-07-06 audit] Portal input CSS + reachability test** — fix
      `width:100%%` and test Wi-Fi/plug reachability before saving config
      (onboarding). (TD#31)
- [x] **Kill N+1 queries** (2026-07-07) in `get_available_plugs`,
      `cpo_list_plugs`, `cpo_analytics_sessions`, `get_my_groups` — each is a
      single JOINed statement now; driver endpoints verified byte-identical
      against prod before/after. (TD#9)
- [x] **Extract the access-code generator** (2026-07-07): the 3 duplicated
      inline loops in the CPO group routes are now one
      `generate_unique_access_code(db)` helper (`backend/routers/cpo.py`),
      with tests in `backend/tests/test_access_codes.py`. (TD#10)
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
- [x] **Decide TypeScript** (2026-07-07): **decided — removed the toolchain**
      (`typescript`, `typescript-eslint`, `@types/*`, `tsconfig*.json`;
      `vite.config.ts` → `.js`). All app code stays plain JSX. Found in the
      process: the ESLint config only matched `**/*.{ts,tsx}`, so **lint had
      been passing while checking zero files** — it now lints `js/jsx`, and
      the 32 findings it surfaced were fixed (unused React imports under the
      automatic JSX runtime, use-before-define fetch functions, a dead
      `RAZORPAY_KEY_ID` const; `react-hooks/set-state-in-effect` disabled as
      policy with a comment). (TD#14)
- [x] **Fix `CpoSetup` redirect-during-render** (2026-07-06): the render-body
      `navigate()` is replaced with a declarative `<Navigate to=… replace />`. (TD#18)
- [x] **Add frontend tests** (2026-07-07): Vitest + RTL, 20 tests across 4
      suites — `AuthContext` (restore/login/logout/failed-restore cleanup),
      `ProtectedRoute`/`CpoProtectedRoute` (extracted from `App.jsx` into
      `components/ProtectedRoutes.jsx` for testability), the `TopUp` payment
      handler (asserts `/verify` sends **only** the Razorpay ids/signature —
      no client amount), and the multi-session `SessionContext`
      (restore/switch/start/stop). `npm test` wired into CI. (TESTING Phase 4)

## Launch readiness (opened 2026-07-11)

- [x] **Web HTTPS — Caddy TLS front door** (2026-07-11, **deployed + verified
      in prod**): `deploy.ps1` ships `docker-compose.tls.yml` by default
      (Caddy on 80/443, auto Let's Encrypt for `CADDY_DOMAIN`, Caddyfile
      generated from `.env`; `-NoTls` = plain-HTTP rollback); bare-IP requests
      are served, not redirected, so a DNS outage can't kill the site.
      Verified live: `https://amphive.duckdns.org` 200 with a validated LE
      cert (expires 2026-10-09, auto-renew), http→https 308, `/api` +
      Socket.io over https, CPO login; broker + gateways unaffected. The
      rollout rode out a real **DuckDNS nameserver outage** (~1 h; Caddy
      auto-retried the cert in — `deploy/docs/web_tls_rollout.md`). (SEC §3)
- [ ] **Web HTTPS follow-ups**: drop tcp:8000 from `allow-amphive-ports`,
      add HSTS + flip bare-IP back to a redirect, and **replace DuckDNS with
      a real domain** (proven SPOF; also needed for Razorpay live-mode
      legal pages). (SEC §3/§6)

## Long term — productionization

- [ ] **[2026-07-06 audit] Multi-plug ESP32 gateway support** — the firmware
      drives a single plug: `main.c` has one `target_plug_ip`/`active_session`
      and `tapo_protocol.c` one global KLAP session + energy integrator, so a
      command for plug B toggles plug A and telemetry is misattributed. Needs a
      per-plug state table + an instance-based KLAP driver, and (recommended)
      the target `local_ip` carried in the `ON` payload. The software AmpHive
      Agent already handles multi-plug — this is ESP32-only. (TD#20, SEC §8.5)
- [ ] **[2026-07-06 audit] Device security hardening** — flash encryption +
      Secure Boot v2 (NVS secrets are plaintext-extractable) and button-hold
      provisioning instead of the boot-time open-portal fallback. The
      overlay-key/anonymous-broker half of the original item was resolved
      2026-07-10 (direct MQTT: per-gateway creds + ACLs + TLS).
      (SEC §8.2/§8.4)
- [ ] **[2026-07-06 audit] Driver notifications** — session start/stop, low
      balance, plug offline, and safety cutoffs. The CPO side ships (alarm
      events feed + ack); the driver side is still in-app-only. (TD#21 done)
- [ ] **[2026-07-06 audit] Auth rate limiting** on `/api/auth/login` +
      `/api/auth/register` (brute-force / enumeration). JWT revocation itself
      shipped 2026-07-08. (SEC §8.6)
- [x] **MQTT broker auth** — **enforced 2026-07-07** (SEC §3):
      `allow_anonymous false` + passwd file generated by `deploy.ps1` from
      `.env` (backend + gateway-fleet accounts), authenticated compose
      healthcheck, backend env plumbed, firmware credentials in NVS
      (`mqtt_user`/`mqtt_pwd`, optional portal fields) seeded + flashed on the
      dev gateway. Verified in prod: anonymous → `not authorised`; backend +
      ESP32 authenticate and telemetry flows. (SEC §3)
- [~] **MQTT broker TLS** (2026-07-08, code complete, staged rollout): a TLS
      listener on **8883** with a self-signed CA + server cert
      (`deploy/config/gen_mqtt_certs.sh`, SAN `IP:100.87.241.70`); firmware
      `1.2.0` embeds the CA and dials `mqtts://…:8883` (validates chain + IP
      SAN; no clock needed since `MBEDTLS_HAVE_TIME_DATE` is off). The
      plaintext `1883` listener stays up during transition (backend internal +
      OTA-rollback target), bound internal-only once all gateways are on 8883.
      Deploy ships certs via `deploy.ps1`; firmware ships via OTA. (SEC §3)
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
- [x] **OTA firmware updates** (2026-07-07): dual-OTA partition table
      (`partitions_ota.csv`) + `esp_https_ota` with bootloader rollback
      (`firmware/main/ota_update.c`), triggered by an `OTA` MQTT command /
      `POST /api/cpo/gateways/{id}/ota` (`send_gateway_ota`). Refuses
      mid-session; the new image cancels rollback only once it re-reaches the
      broker; `online` status now carries the `fw` version.
      **Verified end-to-end on-device 2026-07-08**: `1.1.0 → 1.1.1` pushed
      over MQTT (image served on the LAN) downloaded into `ota_1`, rebooted,
      booted `fw 1.1.1`, reconnected, and cancelled rollback
      (`marking image valid`). (IMPL 15)
- [x] **Reconcile or retire K8s manifests** (2026-07-07): **retired** —
      `deploy/k8s/README.md` banner-marks them unmaintained reference
      material (divergence documented); `docs/DEPLOYMENT.md` no longer
      presents them as a parallel deployment model. (TD#15)
- [ ] **TimescaleDB** (hypertables/retention/continuous-aggregates) for
      `telemetry_readings` if telemetry volume grows. (IMPL 2)
- [x] **Signed OTA + public HTTPS image host** (2026-07-10, **rolled out**):
      fw ≥ 1.4.0 verifies an ECDSA app signature on every update
      (`SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT`; key
      `firmware/secure_boot_signing_key.pem`, gitignored — **back it up**)
      and refuses plain-http downloads (`ALLOW_HTTP` removed; backend
      `CpoGatewayOtaRequest` now `^https://`, **deployed**). Images live on the
      public-read bucket `gs://amphive-fw`. The real gateway `1cc3abb4fb54`
      was OTA'd end-to-end over direct-MQTT from `1.3.2-direct` → signed
      **`1.5.0-direct`** (`OTA_OK_REBOOTING` → offline → back online on 1.5.0,
      rollback cancelled). From 1.4.0 onward only signed images install.
      Runbook: `deploy/docs/ota_image_publishing.md`. (SEC §3)

## Shipped 2026-07-10 — safety, alarms & live UX

- [x] **Firmware unauthorized physical-on guard (fw 1.5.0).** The relay ON with
      no active session (physical button / Tapo app / stale NVS resume) is forced
      OFF locally every poll and alarmed once per episode (`UNAUTHORIZED_ON`,
      rising-edge) using the plug's real `device_on`. Live on the real gateway;
      backend ingestion verified in prod. Remote physical-press trigger is
      unit-tested + by-construction (no LAN path to press it remotely).
- [x] **Trapezoidal energy integration (fw 1.5.0).** Driver-side kWh integrator
      switched from left-rectangle to the trapezoidal rule (average of
      consecutive power samples) for lower error on ramping loads at 10 s cadence.
- [x] **Backend alarm ingestion + CPO events feed.** Subscribes
      `amphive/gateways/+/alarms`; persists `gateway_events`
      (Alembic `0005_gateway_events`); broadcasts `gateway_alarm` Socket.io;
      `GET /api/cpo/events` + `POST /api/cpo/events/{id}/ack`. Tests in
      `test_mqtt_manager.py`; verified live in prod.
- [x] **Driver gateway-offline UX.** `/api/plugs/available` + `/api/plugs/{id}`
      now return `gateway_online`; Home dims/disables unreachable chargers.
- [x] **Live-monitor robustness.** Socket.io `telemetry` now carries
      `relay_on`/`voltage_v`/`is_stale`/`age_sec`; the session monitor shows a
      client-side ticking clock, a "reconnecting" staleness banner, a voltage +
      relay line, and per-plug alarm banners. Tests in `SessionMonitor.test.jsx`.
- [x] **Gateway firmware-version tracking.** `gateways.firmware_version`
      (Alembic `0006`) populated from the `online` status `fw`; exposed in
      `GET /api/cpo/gateways` and shown in the CPO plugs table. Verified live
      (real gateway → `1.5.0-direct`).
- [x] **CPO Gateways page + OTA-from-UI.** New `/cpo/gateways` fleet view
      (status, fw, last-seen, plug count) with a one-click OTA modal
      (`POST /api/cpo/gateways/{id}/ota`), so operators push updates without curl.
- [x] **Unified wallet ledger.** `GET /api/wallet/ledger` returns top-up
      credits **and** session debits with running balance; the driver History
      page is now tabbed (Charging Sessions / Wallet Ledger). Closes the
      old debits-only gap.
- [x] **Pricing clarity.** Public `GET /api/config` (tariff, min-balance,
      coin↔INR); Home shows the rate + balance-covers-≈-kWh with a top-up nudge;
      session monitor reads the rate from config. `MIN_START_BALANCE_COINS` env.
- [x] **Prepaid protection: auto-stop on balance exhaustion.** Backend
      finalizes a session once accrued cost reaches the wallet balance (env
      `AUTO_STOP_ON_BALANCE_EXHAUSTED`), so a drained wallet can't keep charging
      for free. Driver-facing low-balance warning in the monitor pairs with it.
      Tests in `test_mqtt_manager.py` + `SessionMonitor.test.jsx`.
- [x] **Post-session receipt.** The stop path returns a full billing summary
      (energy, peak power, duration, coins charged/shortfall, balance before→
      after, plug, timestamps); the Session page shows a `SessionReceipt` card
      on stop. **Verified live end-to-end** (real billed session on the fake
      plug: 0.101 kWh → 0.51 coins, wallet debited + ledger reconciled).
- [x] **CPO session CSV export** and **operator-side amps** in the load
      analytics (`avg_current_a`/`max_current_a`; dashboard shows peak W + A).
- [x] **Token revocation / shorter-lived JWTs** (2026-07-08): every JWT
      carries the user's `token_version` epoch (`tv` claim), re-checked per
      request; `POST /api/auth/logout` bumps it to revoke all of a user's
      tokens ("log out everywhere", no blacklist table). `JWT_EXPIRY_DAYS` is
      now env-configurable (default 7). Migration `0003_token_version`;
      frontend `logout()` calls the endpoint; tests in
      `backend/tests/test_token_revocation.py` +
      `frontend/src/contexts/AuthContext.test.jsx`.
- [x] **Replace `frontend/README.md`** (2026-07-07): real package docs (stack,
      commands, env vars, layout, conventions) pointing at `docs/` as the
      canonical source. (TD#16)

---

### Done recently (context, so these don't read as open)
RBAC enforced · unauthenticated provisioning removed · wallet updates row-locked
· webhook auto-credit idempotent · SSE authenticated · server-authoritative
payment amount · JWT known-default refusal · Socket.io `get_participants`
regression fixed · firmware KLAP v2 driver · NVS session persistence + offline
buffering · over-current/thermal cutoffs. See
[IMPLEMENTATION_STATUS.md §3](IMPLEMENTATION_STATUS.md) for the full list.
