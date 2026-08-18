# AmpHive — Prioritized Improvement Roadmap

*Audit 2026-07-05 @ `78cffeb`. Ordered by (highest impact ÷ lowest effort).
Cross-references [TECH_DEBT.md](TECH_DEBT.md) items as `TD#n` and
[SECURITY.md](SECURITY.md).*

*Items tagged **[2026-07-06 audit]** come from the follow-up audit; only the
ones still open as of 2026-07-11 are listed (the audit's alarm-ingestion,
ledger-view, staleness, low-balance, and JWT-revocation findings had already
been fixed by the 2026-07-08…11 work — see the shipped sections below).*

---

## OPEN — 2026-08-04 reliability & integration backlog

*Surfaced by two prod incidents on 2026-08-04 (activating the DB
privilege-separation crash-looped the backend; the admin-uploaded firmware
would not OTA to the real gateway) plus a follow-up reliability sweep. Common
theme: all are the "passes CI / passes unit tests, fails the first time it hits
real infra or hardware" class — CI validates logic and bytes, not "does asyncpg
accept this SQL against a live DB" or "can a constrained ESP32 finish a 1 MB TLS
download through the proxy". Both incidents are RESOLVED (fix PRs #107/#108
merged+deployed; DB separation is live and proven; 2.5.0 OTA repointed to GCS);
the items below are the follow-ups so they don't recur. Security audit from the
same day is fully remediated — see [SECURITY.md](SECURITY.md) §"2026-08-04"; not
repeated here.*

- [x] **OTA upload feature is unreliable for real devices — keep OTA on GCS.**
      **DONE 2026-08-11** (deploy + IAM grant pending): the upload endpoint now
      publishes the `.bin` to `gs://amphive-fw` (keyless, via the VM
      service-account identity — `backend/services/firmware_publish.py`) and
      registers the public `storage.googleapis.com` bucket URL, replacing the
      self-hosted `FileResponse`. **One-time infra prereq before it works in
      prod:** grant `roles/storage.objectCreator` on `gs://amphive-fw` to the
      compute SA — see `deploy/docs/ota_image_publishing.md`.
      **(High — recurs on every UI upload.)** The admin firmware-**upload**
      path self-hosts images at `GET /api/firmware/images/{file}`
      (`backend/routers/firmware_images.py:46`, Caddy→nginx→FastAPI
      `FileResponse`); the release URL is minted from
      `PUBLIC_API_ORIGIN||FRONTEND_ORIGIN` (`backend/routers/admin.py:1163`).
      A real ESP32 cannot finish that download — the TLS stream breaks mid-way
      (`esp-tls-mbedtls: read error :-0x7100:` → `download failed/incomplete`),
      even though curl fetches a byte-identical image. **Compounding:** the
      `firmware_images` volume + `PUBLIC_API_ORIGIN`/`FIRMWARE_IMAGE_DIR` env are
      NOT shipped by `deploy.ps1` (it ships only `backend/`+`frontend/`), so an
      uploaded `.bin` is also non-persistent — it vanishes on the next redeploy
      and 404s. Every historically-successful OTA used `gs://amphive-fw`.
      **Fix:** make the upload endpoint push the image to GCS and register the
      GCS URL (or, if keeping self-host, verify the volume+env are on the VM AND
      tune nginx/Caddy for large streamed downloads AND validate against a real
      device). Until fixed, publish via `deploy/scripts/publish_firmware.ps1`
      and point releases at the bucket. (2.5.0-direct release row was repointed
      to GCS by hand as the immediate unblock.)

- [x] **Least-privilege role has no test/boot coverage — add the guardrail that
      would have caught the crash.** **DONE 2026-08-11:** the boot check is now a
      transactional zero-row `UPDATE` write-probe (rolled back — proves the DML
      grant, changes nothing) instead of `SELECT 1`, so an incomplete grant
      crash-loops fast; and a DB-gated live test now runs real
      INSERT/UPDATE/DELETE + `nextval` **as** the provisioned role
      (`backend/tests/test_privilege_separation_live.py`).
      **(Med — highest leverage.)** Every
      DB-gated test connects as the Postgres owner/superuser (all the
      `DROP TABLE IF EXISTS alembic_version` fixtures), and the runtime-role
      boot check is only `SELECT 1` (`backend/database/db.py:271-272`), so
      NEITHER CI NOR startup ever exercises `amphive_app`. That's exactly why
      the `format()`/asyncpg provisioning bug reached prod, and the fix
      (`cast(:x AS text)`, `db.py:190-199`) still has no automated coverage.
      **Fix:** add a DB-gated test that provisions the role against a live
      Postgres and then does INSERT+UPDATE+DELETE+`nextval` **as `amphive_app`**;
      change the boot check to a transactional write-then-rollback.

## OPEN — 2026-08-18 production-readiness audit follow-ups

*Everything the audit could safely fix in code is done (see
[SECURITY.md](SECURITY.md) §"2026-08-18"). These are the residue: operator
actions, and two deliberate deferrals.*

- [x] ~~**Hand-edit the live Caddyfile** for the new headers.~~ **NOT
      REQUIRED on this deployment — verified 2026-08-18.** The standing
      caveat (carried since the 2026-08-04 audit) assumed the live Caddyfile
      contained the `header` block that `deploy/relay/deploy-relay.sh`
      generates, which would SET (replace) nginx's security headers for the
      named domains and therefore mask any change shipped in
      `frontend/nginx.conf`. It does not. The live file is four lines — two
      `reverse_proxy frontend:80` blocks and nothing else:

      ```
      amphive.app, cpo.amphive.app { reverse_proxy frontend:80 }
      136-117-94-209.sslip.io      { reverse_proxy frontend:80 }
      ```

      So **every** security header on prod comes from nginx and passes straight
      through Caddy, and a normal `deploy.ps1` delivers header changes.
      Confirmed after deploying: `Permissions-Policy`, CSP `frame-ancestors` +
      `object-src`, `server_tokens off` and gzip are all live, and http->https
      still works because Caddy's automatic HTTPS handles the named domains
      without an explicit `redir`. The generator's warning stays accurate for a
      *fresh bootstrap* (where it does write a header block), which is the case
      it was written for — but it should not be repeated as a blocker for
      routine deploys.
- [ ] **Verify a real password-reset / verification email still arrives.** The
      no-SMTP console fallback now REDACTS the message body by default (it was
      printing live single-use account-takeover tokens into the logs). Prod has
      `SMTP_HOST` set so nothing should change, but confirm a real send after
      deploying rather than assuming.
- [ ] **Containers still run as root.** DEFERRED, not forgotten: the 2026-08-04
      batch built non-root + `cap_drop` and reverted it because `deploy.ps1`
      ships only source (it cannot apply the compose half) and the live
      root-owned `firmware_images` volume must be chowned first. Needs a
      dedicated infra deploy, not a bundle. (`backend/Dockerfile`,
      `agent/Dockerfile` still have no `USER`.)
- [ ] **`python-jose` -> PyJWT.** The one remaining dependency advisory is
      `ecdsa` PYSEC-2026-1325, which has NO upstream fix, arrives transitively
      via python-jose, and is unreachable here (ES*-only; `JWT_ALGORITHM` pins
      HS256 on both encode and decode). CI ignores exactly that ID with a
      written justification. Migrating off python-jose would remove it
      entirely, but an auth-path swap did not belong in this batch.
- [ ] **`users.email` uniqueness is byte-exact, not case-insensitive.**
      `models.py` has a plain `unique=True` while every lookup uses
      `func.lower(User.email)`. Application code has normalised to lowercase
      since early on, so a case-variant duplicate almost certainly does not
      exist — but if one did, `scalar_one_or_none()` on login would raise
      `MultipleResultsFound` -> 500. Deliberately NOT fixed here: the correct
      fix is a functional unique index, expression indexes reflect poorly
      through `compare_metadata` (which `test_migrations.py` gates on), and a
      migration that can fail on real data is exactly the startup-migration
      hazard this file warns about below. Check first with:
      `SELECT lower(email), count(*) FROM users GROUP BY 1 HAVING count(*) > 1;`
- [ ] **Gateway-ID squatting is still possible on the legacy manual path.**
      `POST /api/cpo/gateways` lets any CPO register any not-yet-registered
      `gateway_id`, and a MAC is not a secret. Severity is limited because the
      squatter still receives no telemetry unless an operator also provisions
      that gateway's broker credentials to them (`add_gateway_user.ps1`), and
      the claim-code path is enumeration-safe by design. The fix — retire the
      manual path in favour of claim codes — is a product decision, not a bug
      fix.
- [ ] **Backups have no alerting.** The nightly cron writes only to
      `~/amphive-relay/backup.log`. This batch made it resolve the DB container
      instead of hardcoding a name that depends on which compose file the VM
      runs (it would otherwise have failed silently every night after a rebuild
      from the repo compose file) and made it stamp
      `backups/LAST_SUCCESS`, so staleness is one `ls -l` away — but a
      silently-failing cron is still a backup you do not have. Verified live
      2026-08-18: backups are current.

---

- [ ] **`mosquitto message_size_limit` is repo-only, not on the prod broker.**
      **(Low, quick.)** `backend/services/mqtt/router.py:16-23` comments that the
      broker's `message_size_limit 8192` (`deploy/config/mosquitto.conf:45`) is
      "the primary gate", but `deploy.ps1` never ships broker config, so the prod
      broker almost certainly still runs mosquitto's 256 MB default — the only
      real ceiling is the 16 KB app-layer guard. `SECURITY.md` claims the broker
      caps at 8 KB (true of the repo, not the running broker). **Fix:** `scp` the
      conf to the VM + recreate the `mqtt` container; reconcile the stale
      `deploy/k8s/mosquitto.yaml` copy.

- [x] **Firmware release "deactivate" is a one-way trap.** **DONE 2026-08-11:**
      added `POST /api/admin/firmware-releases/{id}/reactivate` + a Reactivate
      button in `AdminFirmwareReleases.jsx`, and re-uploading/re-registering a
      **deactivated** version now overwrites its url/notes and reactivates it
      (an ACTIVE version is still a hard 400). **(Med.)** There was a
      `.../deactivate` endpoint (`backend/routers/admin.py:1379`) but no
      reactivate, and the upload endpoint rejected any existing version even when
      it was deactivated — so a deactivated release could neither be re-enabled
      nor re-uploaded (stranded the 2.5.0 release; unblocked by a hand DB
      `is_active=true`).

- [x] **Startup-migration hazards (latent outage).** **DONE 2026-08-11:** the
      CI single-head guard shipped in PR #110
      (`backend/tests/test_migration_heads.py`, pure/no-DB), and the
      writing-a-migration conventions (out-of-band backfills,
      `CREATE INDEX CONCURRENTLY` via `autocommit_block` on hot tables,
      single-head, least-priv grant limits) are now documented in
      [DATA_MODEL.md](DATA_MODEL.md) §4. **(Low, preventive.)**
      `init_db` runs `alembic upgrade head` on every boot inside the single
      backend container (`backend/database/db.py:259-260`). A future slow
      migration (a big backfill `UPDATE`, or a non-`CONCURRENTLY` index on the
      large `telemetry_readings` table) would hold locks and stall startup =
      downtime, since `up -d` recreates the only backend container. Separately,
      the "PROVISIONAL NUMBERING" pattern + parallel agents can produce two
      migration heads, which wedges boot until hand-merged. **Fix:** convention
      to run data backfills out-of-band and use `CREATE INDEX CONCURRENTLY`
      (Alembic `autocommit_block`) on hot tables; add a CI single-head check.

- [x] **Document the least-privilege role's object-class limits (latent).**
      **DONE 2026-08-11:** noted in the [DATA_MODEL.md](DATA_MODEL.md) §4
      writing-a-migration conventions (cross-referencing
      `_provision_runtime_role`'s in-code OBJECT-CLASS LIMIT note).
      **(Low.)** The runtime grant is `... ON ALL TABLES` + `ALTER DEFAULT
      PRIVILEGES` in schema `public` (`backend/database/db.py:201-211`). This
      covers tables/views/sequences created by the owner in `public` — but NOT
      materialized views, objects in a new schema, or objects created by a
      different role. A future migration adding any of those would give
      `amphive_app` "permission denied" at runtime, invisible to CI. **Fix:**
      note "public-schema tables/views/sequences only" in the migration guide;
      grant explicitly for anything else.

- [ ] **`.env.template` missing the firmware-upload keys.** **(Low.)** Add
      `PUBLIC_API_ORIGIN` and `FIRMWARE_IMAGE_DIR` to
      `deploy/config/.env.template` (only referenced in a compose comment today),
      so a new deploy doesn't silently mis-configure the upload feature. Part of
      the broader deploy.ps1-doesn't-ship-`deploy/` drift (mosquitto.conf,
      compose, Caddyfile are all VM-managed — see the config-drift note in the
      2026-08-04 sweep).

- [ ] **On-hardware verification backlog (operator, no code).** **(Reality-
      check.)** fw 2.5.0-direct's WiFi-reconnect fix is built + on GCS but has
      never run on the gateway — verify by power-cycling the router after it
      installs and confirming it reconnects unaided. Also the standing
      "on-device verify pending" cluster that only rides to the field via manual
      OTA: crash-recovery duration watchdog (fw 2.1.0), offline-resync per-entry
      `session_id` billing attribution (fw 2.1.0), energy-integrator NVS restore
      (needs ≥50 Wh accrued), and the two-real-plug multi-plug concurrency path
      (needs a second unit). `docs/TESTING.md` notes firmware has zero
      host-runnable tests — these billing/safety paths are on-device-only.

- [ ] **(Note, not a bug) `_persist_telemetry` loads the `Plug` row without
      `with_for_update`** (`backend/services/mqtt/telemetry.py:395`), so
      concurrent frames for one plug can last-writer-wins on
      `current_power_w`/`last_telemetry_at`/`last_*_energy_kwh`. Session energy
      is under the session row lock (not affected); the unmetered-consumption
      baseline race is already documented in-code as an accepted best-effort
      safety net. Recorded so it isn't mistaken for an untracked bug.

---

## Immediate (this week) — security & correctness

- [x] **[2026-07-06 audit] Lock down the ESP32 provisioning portal.** Done
      fw 1.6.0 (2026-07-11) — a per-device setup code (random, NVS-persisted,
      printed over serial for the unit label) is the WPA2 passphrase of the
      setup AP **and** a constant-time-checked token on `/save` (wrong code →
      1 s throttle + 403); the portal runs AP-only and the device reboots
      after 10 min of portal inactivity (also self-heals the §8.4 Wi-Fi-loss
      fallback). (SEC §8.1, was **CRITICAL**)
- [x] **[2026-07-06 audit] Reject session starts on OFFLINE/MAINTENANCE plugs.**
      Done 2026-07-11 — any non-`AVAILABLE` status now 409s; new plugs (default
      OFFLINE) need a CPO enable before first use. (TD#22)
- [x] **[2026-07-06 audit] Guard telemetry ingestion.** Done 2026-07-11 —
      guarded casts + int plug_id + finiteness check, and
      `plug.gateway_id == <topic gateway>` verified before session totals AND
      the raw-sample enqueue. (TD#25, SEC §3/§8.5)

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

- [x] **[2026-07-06 audit] CPO admin audit log.** Done 2026-07-12 — new
      `audit_logs` table (Alembic `0007_audit_log`) + `services/audit.py`
      (non-fatal write: a failure is logged, never breaks the admin action),
      wired into gateway create, plug create, plug status change, group
      create/delete, and access-code regen in `routers/cpo.py`; read via
      `GET /api/cpo/audit`. Gateway/plug delete have no endpoint yet to hook.
      (TD#26)
- [x] **[2026-07-06 audit] Fix crash-recovery duration watchdog.** Done —
      persisted-elapsed (not SNTP: no wall-clock dependency, and SNTP would
      over-count power-off time): `session_params_t.elapsed_s` is re-persisted
      on a 30 s throttle in the telemetry task, and recovery back-dates
      `start_time_s = now − elapsed_s` so the duration cap counts total
      elapsed across reboots (worst-case overrun ≈ one persist interval).
      Shipped in fw 2.1.0-direct; on-device verify pending. (TD#23)
- [x] **[2026-07-06 audit] Stamp `session_id` into the offline ring buffer.**
      Done — each `offline_log` entry stores a compact `uint32_t` session id
      (+ occupied state) at capture time; the resync payload echoes it (plus
      `relay`/`offline:true`) so the backend attributes each buffered reading
      to its exact session and drops frames for finalized ones. Shipped in
      fw 2.1.0-direct; on-device verify pending. (TD#24)
- [x] **[2026-07-06 audit] Structured logging + correlation ids — backend.**
      Done 2026-07-12 — `backend/logging_config.py` (JSON-lines formatter on
      the root logger, `correlation_id` ContextVar + `logging.Filter`, env
      `LOG_LEVEL`/`LOG_FORMAT`); a FastAPI middleware binds/echoes
      `X-Request-ID` per request so an HTTP request traces through to the
      MQTT command it triggers. Hot-path f-strings converted in
      `routers/auth.py`, `routers/sessions.py`,
      `services/session_lifecycle.py`, `services/mqtt_manager.py`. Broker log
      now also persists to a file on the `mosquitto_log` volume (durable
      across container recreation), stdout bounded via the compose
      `logging:` driver. Tests: `backend/tests/test_logging.py`. Still open:
      a firmware log topic for field diagnostics (serial-only today). (TD#28)
- [x] **[2026-07-06 audit] Registration validation.** Done 2026-07-11 —
      `EmailStr` + 8-72 char password rule; login left unvalidated for
      pre-rule accounts. (TD#30)
- [x] **[2026-07-06 audit] Portal reachability test.** Done — the portal's
      `/save` now pre-checks the submitted Wi-Fi credentials by briefly
      associating in AP+STA mode (`portal_precheck_wifi`, ≤ 20 s, fail-open:
      only a definite association failure blocks the save). The plug-IP half
      is moot — plug IPs come from the backend's retained roster (fw ≥ 2.0.0),
      so the portal no longer collects them. Limitation: the single radio may
      briefly drop the installer's phone off the setup AP during the test.
      Shipped in fw 2.1.0-direct; on-device verify pending. The CSS half of
      TD#31 was closed in fw 1.6.0 (`box-sizing:border-box`; the reported
      `width:100%%` was a mis-diagnosis — the HTML *is* printf-formatted, so
      `%%` already rendered as `%`). (TD#31)
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
- [~] **Web HTTPS follow-ups** (2/4 done 2026-07-11): ~~drop tcp:8000 from
      `allow-amphive-ports`~~ (tcp:80-only now; :8000 stays VM-local) and
      ~~add HSTS~~ (max-age=31536000 in the generated Caddyfile) — both
      **deployed + verified in prod**. **Real domain acquired 2026-07-20:
      `amphive.app`** (name.com; .app is HSTS-preloaded → https-only) —
      CORS/Socket.io allowlists + Caddy multi-domain serving (`amphive.app,
      amphive.duckdns.org` transition) shipped; cert issues automatically
      once the name.com A records point `@` and `cpo` at 8.231.81.12
      (parking IP still resolving at ship time). **Cutover verified live +
      duckdns names retired 2026-07-20** (allowlists + Caddy site blocks
      dropped). DECISION: bare-IP serve-mode stays (not flipped to a
      redirect) — it is the DNS-outage escape hatch and the frontend's
      unsplit bare-IP mode depends on it; a redirect would defeat both.
      (SEC §3/§6)
- [x] **Database + config backups** (2026-07-11, **live + verified
      end-to-end**): nightly `backup_db.sh` cron (21:00 UTC) does
      `pg_dump -Fc` + ops-config tarball (broker passwd/ACL hashes exist only
      on the VM) → `gs://amphive-db-backups` (private, 30-day lifecycle),
      last 3 sets kept locally; daily **disk snapshots** (14-day retention)
      attached to the VM disk. VM uploads keylessly (`devstorage.read_write`
      scope, ~48 s approved downtime); bucket IAM is create+view only —
      deletes from the VM are **denied**. Upload set verified in the bucket;
      **restore tested** into a scratch DB (row counts matched). Remaining
      ritual: quarterly restore drill (`deploy/docs/db_backup_restore.md`).

## Account & auth features (backlog)

- [x] **Password reset ("forgot password") flow.** Done 2026-07-20 —
      `POST /api/auth/forgot-password` issues a single-use, time-boxed token
      (SHA-256 digest in the new `password_reset_tokens` table, migration
      `0023`; `RESET_TOKEN_TTL_MIN`, default 30 min; always the same generic
      200 — no enumeration) and `POST /api/auth/reset-password` consumes it:
      same 8-72 password rule as registration, bcrypt rehash, `token_version`
      bump (revokes all sessions), token stamped used. Both rate-limited via
      `services/rate_limit.py` (`FORGOT_PASSWORD_RATE_LIMIT` 5/3600,
      `RESET_PASSWORD_RATE_LIMIT` 10/3600). Email via the new pluggable
      `services/email.py`: STARTTLS `smtplib` when `SMTP_HOST` / `SMTP_PORT` /
      `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` are set, otherwise the
      `FRONTEND_ORIGIN/reset-password?token=...` link is logged at WARNING
      (console fallback — the flow works without a provider; plug in real
      SMTP creds when one is chosen). Frontend: "Forgot password?" link on
      `Login` + `/forgot-password` and `/reset-password` pages. Tests:
      `backend/tests/test_password_reset.py` + the two page suites. (No
      security §; new capability.)
- [x] **Google login ("Sign in with Google" / OAuth2).** **SHIPPED 2026-08-02**
      (backend-driven authorization-code flow in `routers/auth.py`
      `google_login`/`google_callback` + a fragment-`code` exchange endpoint —
      no JS SDK): the callback verifies the id_token, upserts by **verified
      email** (linking `users.google_sub`, a nullable column with a partial
      unique index, to an existing email/password account when one exists), and
      issues the same app JWT the password flow does. **Live-gated:** hidden
      everywhere (`GET /api/config` `google_login_enabled: false`,
      `/api/auth/google/login` 503s) unless `GOOGLE_CLIENT_ID` /
      `GOOGLE_CLIENT_SECRET` / `GOOGLE_OAUTH_REDIRECT_URI` are set. **LIVE IN
      PROD (verified 2026-08-11):** the real Google Cloud OAuth client is
      configured — all three `GOOGLE_*` are set on the VM, `/api/config`
      reports `google_login_enabled: true`, and `/api/auth/google/login` 302s
      to `accounts.google.com` with the real client id + registered
      `redirect_uri=https://amphive.app/api/auth/google/callback`. Only residual
      is a full interactive click-through smoke (needs a real Google account;
      the front leg is confirmed). NOTE: because Google login is live, the
      OAuth account-pre-hijacking hole was a LIVE takeover — closed 2026-08-11
      (PR #118: link evicts the old password + bumps `token_version`), and the
      complete fix (email verification at registration) shipped 2026-08-11 too.
      (Original ask below.)
      Social login to cut
      onboarding friction. Add a Google OAuth2 authorization-code flow:
      `GET /api/auth/google/login` → Google consent → `GET /api/auth/google/callback`
      verifies the returned id_token, upserts a user by **verified email**
      (linking to an existing email/password account when one exists), and issues
      the same app JWT the password flow does. Persist the provider + subject
      (nullable columns or a `user_identities` table) so OAuth users need no
      password. **Needs a Google Cloud OAuth client** (client id/secret in `.env`,
      registered redirect URIs) and ideally a **real domain** for the redirect
      (DuckDNS is a demonstrated SPOF — see the web-HTTPS follow-up). Frontend: a
      "Continue with Google" button on `Login`. Consider generalizing to a
      provider table so more IdPs (Apple, etc.) can be added later.

## Wallet & payments features (backlog)

- [x] **CPO offline coin top-up, funded by the CPO's own earnings.** Done
      2026-07-21 — lets a CPO manually credit a driver's coin wallet for a
      cash payment collected offline, without creating coins out of thin
      air: `POST /api/cpo/topups` `{driver_email, amount_coins, note?}`
      computes the tenant's currently-available top-up pool (unsettled net
      session earnings since the settlement watermark, minus offline
      top-ups already issued in that same window —
      `services/payouts.py tenant_earnings_summary`'s `available_pool_coins`,
      the exact math `GET /api/cpo/earnings` displays as `topup_pool`),
      409s with the actual available figure if the amount would exceed it,
      row-locks the tenant then the driver, credits via `credit_wallet()`,
      and writes a driver-side `LedgerTransaction` (new `tx_type` enum value
      `cpo_topup`, added by `ALTER TYPE` in migration `0026` — the first
      migration in this repo to extend a native Postgres enum) plus a new
      `offline_topups` audit-ledger row and a driver notification.
      `GET /api/cpo/topups` lists the tenant's history (paginated
      `{total,items}`). The existing `POST /api/cpo/payouts` bank-payout
      request now pays out that same reduced figure (net minus top-ups
      already issued since the watermark) instead of the raw unsettled net,
      so a CPO can never draw the same earnings out twice — once as cash,
      once by bank/UPI; 400s cleanly when top-ups have already consumed the
      whole window. Console: `/cpo/earnings` gets an "Offline top-ups" card
      (available-pool figure + explainer, a Modal-then-ConfirmDialog
      "Credit a driver" flow stating the money math plainly, and a history
      table); the driver wallet ledger renders the credit as "Top-up by
      operator (cash)" (`utils/statusCopy.js` `txTypeLabel`). Tests:
      `backend/tests/test_offline_topups.py` (pool math, the
      payout-watermark interaction incl. a real-concurrency race test,
      409/404 paths, ledger reconciliation, tenant scoping, role gating).

## Long term — productionization

- [x] **[2026-07-06 audit] Multi-plug ESP32 gateway support** (shipped fw
      **1.7.1-direct**, **verified on-device 2026-07-12** — single-plug charging
      regression on the real gateway; two-real-plug test still needs a second
      unit). The firmware no longer drives a single plug: `main.c` keeps a `plugs_mutex`-guarded
      per-plug slot table (each slot = DB `plug_id` + LAN IP + a per-plug
      `tapo_plug_t` KLAP context + its own session/watchdog state), and
      `tapo_protocol.c` moved the KLAP session + energy integrator into that
      per-plug context (own mutex + NVS meter `wh_<plug_id>`). `session_nvs`
      persists **all** per-plug sessions in one atomic blob (each with `plug_id`
      + `local_ip`) so crash recovery restores every plug. The backend ships the
      target `local_ip` on ON/OFF (`send_plug_command(..., local_ip=…)`), which
      is how the gateway drives the right plug and learns unseen ones — no
      on-device roster, so the per-gateway broker ACLs and the backend
      `plug.gateway_id` check are untouched (SEC §8.5). The one physical gateway
      has a single plug, so a two-real-plug on-device test needs a second unit.
      (TD#20, SEC §8.5)
- [ ] **[2026-07-06 audit] Device security hardening** — flash encryption +
      Secure Boot v2 (NVS secrets are plaintext-extractable) and button-hold
      provisioning instead of the boot-time portal fallback (now LOW: the
      portal is WPA2-locked + code-gated + idle-times-out since fw 1.6.0).
      The overlay-key/anonymous-broker half of the original item was resolved
      2026-07-10 (direct MQTT: per-gateway creds + ACLs + TLS).
      (SEC §8.2/§8.4)
- [x] **[2026-07-06 audit] Driver notifications.** Done 2026-07-11 — per-user
      `notifications` feed (`services/notifications.py` + Alembic `0007`)
      written at every stop path (user/auto-stop/reaper/**safety cutoff** —
      cutoff alarms now also *finalize* the session instead of leaving it to
      the reaper), low-balance warning (once per session at
      `LOW_BALANCE_WARN_FRACTION`, default 80%), charger-offline (LWT with an
      ACTIVE session), and top-up credit. Delivered in-app (navbar bell +
      Socket.io user rooms) **and via Web Push** (VAPID, `pywebpush`;
      `frontend/public/sw.js`; keys in `.env`, push off gracefully when
      unset). Email deferred (no SMTP provider). Endpoints under
      `/api/notifications*` — see API_REFERENCE.md.
- [x] **[2026-07-06 audit] Auth rate limiting.** Done 2026-07-11 — per-IP
      in-process sliding window on `/api/auth/login` + `/register`
      (429 + Retry-After; `LOGIN_RATE_LIMIT`/`REGISTER_RATE_LIMIT` env,
      defaults 10/60s and 10/3600s), `services/rate_limit.py` + tests.
      Closes SEC §8.6 (JWT revocation half shipped 2026-07-08). (SEC §8.6)
- [x] **MQTT broker auth** — **enforced 2026-07-07** (SEC §3):
      `allow_anonymous false` + passwd file generated by `deploy.ps1` from
      `.env` (backend + gateway-fleet accounts), authenticated compose
      healthcheck, backend env plumbed, firmware credentials in NVS
      (`mqtt_user`/`mqtt_pwd`, optional portal fields) seeded + flashed on the
      dev gateway. Verified in prod: anonymous → `not authorised`; backend +
      ESP32 authenticate and telemetry flows. (SEC §3)
- [x] **MQTT broker TLS** — COMPLETE 2026-07-20: plaintext 1883 is no longer
      host-published (compose change; backend + fake-plug use the internal
      Docker network), so the only externally reachable listener is TLS 8883.
      Original staged rollout below. (SEC §3)
      (2026-07-08, code complete, staged rollout): a TLS
      listener on **8883** with a self-signed CA + server cert
      (`deploy/config/gen_mqtt_certs.sh`, SAN `IP:100.87.241.70`); firmware
      `1.2.0` embeds the CA and dials `mqtts://…:8883` (validates chain + IP
      SAN; no clock needed since `MBEDTLS_HAVE_TIME_DATE` is off). The
      plaintext `1883` listener stays up during transition (backend internal +
      OTA-rollback target), bound internal-only once all gateways are on 8883.
      Deploy ships certs via `deploy.ps1`; firmware ships via OTA. (SEC §3)
- [x] **MQTT broker DNS un-pinning — operator rollout** — **Done: verified
      2026-08-02** (`mqtt.amphive.app` A record resolves to the relay VM
      136.117.94.209; fw ≥2.3.0 fleet connects via the hostname over TLS —
      post-consolidation the broker, cert, and DNS all live on/point at
      `amphive-relay`). Original steps (code done, fw 2.3.0-direct): firmware default is now `mqtts://mqtt.amphive.app:8883`
      (was pinned `8.231.81.12`; NVS `broker_url` override + legacy-IP
      self-migration). Remaining operator steps, in order: reissue the server
      cert (same CA, dual DNS+IP SANs) → create the `mqtt.amphive.app` A
      record → deploy broker → OTA fleet. Runbook:
      `deploy/docs/mqtt_dns_rollout.md`.
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
