# AmpHive — Technical Debt Register

*Audit performed 2026-07-05 against source at commit `78cffeb`. This is the
"why it hurts / what to do" companion to [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)
(what works) and [SECURITY.md](SECURITY.md) (security gaps). Debt is ordered by
priority within each tier.*

*A follow-up code audit on 2026-07-06 added **TD#20–TD#32** (multi-plug firmware,
dropped safety alarms, startable offline plugs, logging/audit/observability, and
device security — the last cross-referenced from [SECURITY.md §8](SECURITY.md#8-firmware--gateway-device-security--backend-follow-up-gaps-2026-07-06-audit)).*

Legend — **Impact**: how much it costs if left. **Effort**: rough work to fix.
**Priority**: P0 (do now) → P3 (someday).

---

## Tier 1 — Correctness & security debt (P0/P1)

| # | Debt | Where | Cause | Impact | Effort | Prio |
|---|------|-------|-------|--------|--------|------|
| 1 | ✅ ~~**Tapo credentials in committed `tools/*.py`**~~ **Done (`3e20dbd`, 2026-07-05)** | `tools/turn_on.py`, `turn_off.py`, `relay_server.py`, `local_tapo_test.py`, `klap_probe.py` | Env-var refactor committed; all five scripts read `TAPO_EMAIL` / `TAPO_PASSWORD` / `TAPO_PLUG_IP` and fail closed when unset. | Removed from HEAD; **password rotated 2026-07-05**. Values remain in git history (see TD#2). | — | ~~P0~~ Done |
| 2 | ✅ ~~**Burned secrets not rotated**~~ **Done 2026-07-06** | WireGuard key, DuckDNS token, Tapo password, DB password | All four rotated at the source. Dead old values remain in git history. | Repo-history copies are now worthless; *optional* history scrub to purge them entirely. | — | ~~P0~~ Done |
| 3 | 🟡 ~~**MQTT publicly reachable**~~ **Network exposure closed 2026-07-06** | `deploy/config/mosquitto.conf`, GCP firewall | Broker now binds to overlay IP `100.87.241.70`; firewall restricted to tcp:80/8000 (1883 dropped). | Public forge / ON-OFF path closed. **Still open:** broker *auth* — anonymous overlay peers can still publish; needs a firmware credentials field. | — | P2 (auth) |
| 4 | ✅ ~~**CORS wildcard + credentials**~~ **Done 2026-07-06** | `backend/main.py:187` | Was `allow_origins=["*"]` with `allow_credentials=True`; replaced with an explicit allowlist. | Committed + **deployed**; verified in prod (allowed origin echoed, evil origin gets no ACAO header). | — | ~~P1~~ Done |
| 5 | **No DB migration tool** | `backend/database/db.py` `_INPLACE_UPGRADES` | Schema evolves via `create_all` + hand-written idempotent `ALTER`s. `schema.sql`/`schema_v2.sql` are never executed and have drifted. | Column/constraint changes are manual and error-prone; unique constraints in the SQL files are silently missing from the live DB. | 2–4 h (adopt Alembic) | **P1** |
| 6 | ✅ ~~**Money stored as `Float`**~~ **Done 2026-07-06** | `models.py` `coin_balance`, `coins_spent`, `amount`, `balance_after` | Migrated to `Numeric(12,2)` (→ Decimal); all wallet math routed through `services/money.to_money` (half-up, 2 dp). In-place guarded `ALTER … TYPE` in `db.py`. | Float drift eliminated. **Still open:** DB-level non-negative-balance CHECK. | — | ~~P1~~ Done |
| 20 | **Firmware actuates ONE plug regardless of the command's target** *(2026-07-06 audit)* | `firmware/main/main.c:434,462,551,570` + `firmware/main/tapo_protocol.c` (global `s_sess`, `s_energy_wh`) | Driver + gateway are single-instance: one global KLAP session + energy integrator; ON/OFF always drive `target_plug_ip`; telemetry publishes under the last-commanded `active_plug_id`. | With >1 plug/gateway (the backend allows it), a command for plug B toggles plug A and A's telemetry is billed to the wrong id. **Blocks multi-plug.** | 1–2 d (per-plug state table + instance-based KLAP driver) | **P1** |
| 21 | **Backend never consumes `+/alarms`** *(2026-07-06 audit)* | `backend/services/mqtt_manager.py` `_on_connect`/`_on_message` | Subscribes only to `+/telemetry` + `+/status`; the firmware's `THERMAL_CUTOFF`/`OVERCURRENT_CUTOFF` alarms match no handler. | Safety cutoffs are silently dropped — no record, no CPO/driver notification (accountability gap). | 1 h (subscribe + persist + notify) | **P1** |
| 22 | **Sessions startable on OFFLINE/MAINTENANCE plugs** *(2026-07-06 audit)* | `backend/main.py:697` | `start_charging_session` rejects only `OCCUPIED`. | Plug pinned OCCUPIED with no charge (bills 0); a plug a CPO deliberately took out of service is still usable. | 30 min (require `AVAILABLE`) | **P1** |

## Tier 2 — Structural / maintainability debt (P1/P2)

| # | Debt | Where | Cause | Impact | Effort | Prio |
|---|------|-------|-------|--------|--------|------|
| 7 | **`main.py` is a 2,291-line god module** | `backend/main.py` | All 37 routes, every Pydantic schema, and the lifespan live in one file. | Hard to navigate/test; merge-conflict magnet; no route grouping. | 3–5 h (split into `routers/` + `schemas/`) | **P1** |
| 8 | **Duplicate `init_db`** | `database/db.py:init_db` vs `database/init_db.py` | Two schema-init paths; the standalone one drops all tables. | Confusion over which to run; risk of accidental wipe. | 30 min (rename destructive one to `reset_db.py`, done partly) | **P2** |
| 9 | **N+1 query patterns** | `get_available_plugs`, `cpo_list_plugs`, `cpo_analytics_sessions`, `get_my_groups` | Per-row follow-up queries for group/plug/user names. | Latency grows linearly with row count; fine at demo scale, bad at fleet scale. | 2–3 h (JOIN/`selectinload`) | **P2** |
| 10 | **Access-code generation loops with per-try SELECT** | `cpo_create_group`, `cpo_update_group` (3 copies) | Duplicated `while True` unique-code logic inline in the route. | Copy-paste drift; extra round-trips. | 45 min (extract helper) | **P2** |
| 11 | 🟡 ~~**Firmware command JSON parsed with `strstr`/`sscanf`**~~ **Fixed in code 2026-07-06 (pending on-device flash)** | `firmware/main/main.c` | Command path now parses with cJSON (vendored `json` component); buffers widened (topic 256, data 512) with an oversized/fragmented-payload guard, so a `session_id` no longer truncates. | Robust to whitespace/ordering + truncation. **Not yet compiled/flashed** — no ESP-IDF toolchain on the dev box. | — | ~~P2~~ code done |
| 12 | **Two live telemetry transports** | SSE (`/api/sessions/live`) + Socket.io (`socketio_manager.py`) | Socket.io replaced SSE, but the SSE endpoint and `sse-starlette` dep remain. | Dead-ish surface, double maintenance, reader confusion. | 1 h (retire SSE or document as fallback) | **P2** |
| 23 | **Crash-recovery resets the duration watchdog** *(2026-07-06 audit)* | `firmware/main/main.c:738` | The recovered session's `start_time_s` is reset to "now" (tick-based, no wall clock). | The max-duration limit restarts from zero on every reboot; a reboot loop can overrun the time cap (energy cap still holds). | 2–4 h (SNTP wall-clock baseline, or persist accumulated elapsed) | **P2** |
| 24 | **Offline-resync telemetry can bill the wrong session** *(2026-07-06 audit)* | `backend/services/mqtt_manager.py:245-255`, `firmware/main/offline_log.*` | Ring-buffer entries carry no `session_id`; on resync the backend attributes them to the plug's *current* ACTIVE session. | If the plug was reused between buffering and reconnect, stale energy overwrites the new session's kWh (billing corruption). Narrow window. | 2–3 h (stamp session id, or monotonic guard) | **P2** |
| 25 | **Unguarded float casts in the telemetry handler** *(2026-07-06 audit)* | `backend/services/mqtt_manager.py:137-140` | `float(payload.get(...))` with no try/except; the broker is anonymous. | A malformed/hostile value throws inside the paho callback and drops the message before persistence. | 30 min (validate + try/except) | **P2** |
| 26 | **No CPO admin audit trail** *(2026-07-06 audit)* | `backend/main.py` CPO routes | Gateway/plug/group create-delete, status changes, and access-code regen are not recorded. | No accountability for admin actions in a multi-tenant billing system. | 3–4 h (`audit_log` table + helper) | **P2** |
| 27 | **No gateway staleness sweep / silent-offline detection** *(2026-07-06 audit)* | backend (no scheduled sweep) | `gateways.last_seen_at` only advances on status messages; only the LWT flips a gateway OFFLINE. | A gateway that just goes quiet still shows ONLINE, so the CPO dashboard lies. | 1–2 h (periodic sweep marking stale gateways OFFLINE) | **P2** |

## Tier 3 — Cleanup / hygiene debt (P2/P3)

| # | Debt | Where | Cause | Impact | Effort | Prio |
|---|------|-------|-------|--------|--------|------|
| 13 | **No CI** | repo | Solo project. | Tests/lint never run automatically; the `get_participants` bug shipped "verified". | 1–2 h (GitHub Actions: pytest + eslint) | **P2** |
| 14 | **Frontend has TS toolchain but all `.jsx`** | `frontend/tsconfig*.json`, `@types/*` | TS was set up, never adopted. | Dead config + deps; no type safety despite the appearance of it. | Decide: adopt TS or drop the toolchain | P3 |
| 15 | **K8s manifests diverge from the live VM** | `deploy/k8s/*` | Manifests predate the Cloud-SQL→VM-container migration. | Stale images, missing secrets, in-cluster PG vs VM PG — misleading if someone tries them. | 1 h (mark experimental or update) | P3 |
| 16 | **`frontend/README.md` is the stock Vite template** | `frontend/README.md` | Never replaced. | No frontend-specific onboarding. | 20 min | P3 |
| 17 | ✅ ~~**Plug geolocation not modeled**~~ **Done 2026-07-06** | `models.py` `Plug` | Added nullable `latitude`/`longitude`; APIs return effective coords (plug's own, else its gateway's); CPOs set them via create/update. `MapComponent` plots only real coords (no more `Math.random()`, which also moved markers each re-render). | — | ~~P3~~ Done |
| 18 | ✅ ~~**`CpoSetup` calls `navigate()` during render**~~ **Done 2026-07-06** | `frontend/src/pages/cpo/CpoSetup.jsx` | Replaced the render-body `navigate()` with a declarative `<Navigate … replace />`. | — | ~~P3~~ Done |
| 19 | 🟡 ~~**Firmware energy meter resets on reboot**~~ **Fixed in code 2026-07-06 (pending on-device flash)** | `tapo_protocol.c` `s_energy_wh` | Integrator now persists to NVS (blob) — restored on `tapo_init` and written throttled (once per 50 Wh accrued). Post-reboot the meter and `start_energy_kwh` are on the same scale, so `consumed_kwh` no longer goes negative and the energy watchdog stays armed. | **Not yet compiled/flashed** — no ESP-IDF toolchain on the dev box. | — | ~~P2~~ code done |
| 28 | **Unstructured, stdout-only logging** *(2026-07-06 audit)* | backend `logging.basicConfig(INFO)`, firmware `ESP_LOGI` (serial), `mosquitto.conf` `log_dest stdout` | f-string logs, no JSON / correlation ids / rotation; firmware logs only to serial; broker log not persisted. | Can't trace an HTTP request → MQTT command → session; no field diagnostics for a deployed gateway. | 2–3 h (structured logging + request/correlation ids; optional firmware log topic) | **P3** |
| 29 | **No unified wallet ledger view** *(2026-07-06 audit)* | `frontend` `History.jsx` (`GET /api/sessions/history`) | The history screen shows only session debits, not TOPUP credits. | Users can't reconcile their balance from one screen. | 1–2 h (ledger endpoint + view) | **P3** |
| 30 | **Registration lacks validation** *(2026-07-06 audit)* | `backend/main.py:210` (`RegisterRequest.email`) | `email: str` despite `EmailStr` being imported; no password strength/length rule. | Malformed emails accepted; trivially weak passwords allowed. | 30 min (`EmailStr` + a length/complexity check) | **P3** |
| 31 | **Captive-portal input CSS bug + no reachability test** *(2026-07-06 audit)* | `firmware/main/main.c:148` `portal_html` | `width:100%%` is sent verbatim (the string isn't printf-formatted) → invalid CSS; the portal never tests Wi-Fi/plug reachability before saving. | Setup inputs render at default width; a wrong `local_ip`/Wi-Fi password only surfaces at charge time (bad onboarding). | 30 min–2 h | **P3** |
| 32 | **Shared `asyncio.Event` per plug across telemetry consumers** *(2026-07-06 audit)* | `backend/services/telemetry.py:120,158` | One `Event` per plug; a consumer's `.clear()` can swallow another consumer's wakeup. | SSE + Socket.io (or two SSE clients) on one plug add up to ~1 s latency (self-heals via the 1 s timeout). Subsumed by retiring SSE (TD#12). | — (fixed by TD#12) | P3 |

---

## How to burn it down

1. **This week (P0):** ✅ done — `tools/` secret strip (`3e20dbd`), all four
   secrets rotated at the source (2026-07-06), and MQTT taken off the public
   internet (overlay bind + firewall). **Remaining:** commit + deploy the CORS
   lock, add MQTT broker auth.
2. **This month (P1):** lock CORS, adopt Alembic, move money to `Numeric`,
   split `main.py` into routers, add a GitHub Actions CI pipeline.
3. **Backlog (P2/P3):** kill N+1s, unify telemetry transport, cJSON on the
   firmware command path, decide TS-or-not, refresh K8s or mark experimental.

### 2026-07-06 follow-up audit — new work (TD#20–32)

- **P1 correctness/safety:** multi-plug firmware (TD#20 — see the design sketch in
  [SECURITY.md §8](SECURITY.md#8-firmware--gateway-device-security--backend-follow-up-gaps-2026-07-06-audit)),
  consume `+/alarms` (TD#21), reject starts on offline/maintenance plugs (TD#22).
- **P2 reliability/accountability:** duration-watchdog reboot reset (TD#23),
  offline-resync mis-billing (TD#24), telemetry cast guard (TD#25), CPO audit log
  (TD#26), gateway staleness sweep (TD#27).
- **P3 polish/onboarding:** structured logging (TD#28), unified ledger view
  (TD#29), registration validation (TD#30), portal CSS + reachability test
  (TD#31), shared telemetry Event (TD#32, closed by TD#12).
- **Device security (P0/P1)** lives in [SECURITY.md §8](SECURITY.md#8-firmware--gateway-device-security--backend-follow-up-gaps-2026-07-06-audit):
  open provisioning AP + unauthenticated `/save`, no flash-encryption/secure-boot
  (plaintext NVS secrets), reusable overlay key + anonymous broker, and the
  boot-time fallback into the open portal.
