# AmpHive — Technical Debt Register

*Audit performed 2026-07-05 against source at commit `78cffeb`. This is the
"why it hurts / what to do" companion to [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)
(what works) and [SECURITY.md](SECURITY.md) (security gaps). Debt is ordered by
priority within each tier.*

Legend — **Impact**: how much it costs if left. **Effort**: rough work to fix.
**Priority**: P0 (do now) → P3 (someday).

---

## Tier 1 — Correctness & security debt (P0/P1)

| # | Debt | Where | Cause | Impact | Effort | Prio |
|---|------|-------|-------|--------|--------|------|
| 1 | **Tapo credentials still live in committed `tools/*.py`** | `tools/turn_on.py`, `turn_off.py`, `relay_server.py`, `local_tapo_test.py`, `klap_probe.py` (HEAD) | The env-var refactor is staged in the working tree but **not committed**; `HEAD` still hardcodes `sjgotnfts1@gmail.com` / `Nitya@2001`. `turn_on.py` keeps the real password as an env default even in the working copy. | Real account password is in git history AND current HEAD; SECURITY.md wrongly implies it was removed. | 15 min (commit the strip) + rotate | **P0** |
| 2 | **Burned secrets not rotated** | WireGuard key, DuckDNS token, Tapo password, DB password | Removing from HEAD ≠ rotation; all remain in history. | Anyone with repo history has working credentials. | 1–2 h | **P0** |
| 3 | **MQTT broker anonymous, no TLS, publicly reachable** | `deploy/config/mosquitto.conf`, GCP firewall 1883/0.0.0.0 | Confidentiality was meant to come from the overlay, but the port is also bound publicly (`MQTT_BIND_IP` default `0.0.0.0`). | Anyone can publish forged telemetry that **feeds billing**, or send ON/OFF. | 30 min (bind overlay IP + drop firewall rule); broker auth needs a firmware credential field | **P0** |
| 4 | **CORS wildcard + credentials** | `backend/main.py:185` | `allow_origins=["*"]` with `allow_credentials=True`. | Invalid/insecure combo; must be a fixed origin before prod. | 10 min | **P1** |
| 5 | **No DB migration tool** | `backend/database/db.py` `_INPLACE_UPGRADES` | Schema evolves via `create_all` + hand-written idempotent `ALTER`s. `schema.sql`/`schema_v2.sql` are never executed and have drifted. | Column/constraint changes are manual and error-prone; unique constraints in the SQL files are silently missing from the live DB. | 2–4 h (adopt Alembic) | **P1** |
| 6 | **Money stored as `Float`** | `models.py` `coin_balance`, `coins_spent`, `amount`, `balance_after` | Float chosen for simplicity. | Rounding drift on repeated credit/debit; `round(...)` everywhere is a workaround, not a fix. | 2–3 h (migrate to `Numeric(12,2)`) | **P1** |

## Tier 2 — Structural / maintainability debt (P1/P2)

| # | Debt | Where | Cause | Impact | Effort | Prio |
|---|------|-------|-------|--------|--------|------|
| 7 | **`main.py` is a 2,291-line god module** | `backend/main.py` | All 37 routes, every Pydantic schema, and the lifespan live in one file. | Hard to navigate/test; merge-conflict magnet; no route grouping. | 3–5 h (split into `routers/` + `schemas/`) | **P1** |
| 8 | **Duplicate `init_db`** | `database/db.py:init_db` vs `database/init_db.py` | Two schema-init paths; the standalone one drops all tables. | Confusion over which to run; risk of accidental wipe. | 30 min (rename destructive one to `reset_db.py`, done partly) | **P2** |
| 9 | **N+1 query patterns** | `get_available_plugs`, `cpo_list_plugs`, `cpo_analytics_sessions`, `get_my_groups` | Per-row follow-up queries for group/plug/user names. | Latency grows linearly with row count; fine at demo scale, bad at fleet scale. | 2–3 h (JOIN/`selectinload`) | **P2** |
| 10 | **Access-code generation loops with per-try SELECT** | `cpo_create_group`, `cpo_update_group` (3 copies) | Duplicated `while True` unique-code logic inline in the route. | Copy-paste drift; extra round-trips. | 45 min (extract helper) | **P2** |
| 11 | **Firmware command JSON parsed with `strstr`/`sscanf`** | `firmware/main/main.c:394-465` | No JSON parser on the MQTT command path (cJSON is vendored but unused here). | Brittle to whitespace/ordering; `data[128]` truncation can corrupt a long command with `session_id`. | 1–2 h (use cJSON) | **P2** |
| 12 | **Two live telemetry transports** | SSE (`/api/sessions/live`) + Socket.io (`socketio_manager.py`) | Socket.io replaced SSE, but the SSE endpoint and `sse-starlette` dep remain. | Dead-ish surface, double maintenance, reader confusion. | 1 h (retire SSE or document as fallback) | **P2** |

## Tier 3 — Cleanup / hygiene debt (P2/P3)

| # | Debt | Where | Cause | Impact | Effort | Prio |
|---|------|-------|-------|--------|--------|------|
| 13 | **No CI** | repo | Solo project. | Tests/lint never run automatically; the `get_participants` bug shipped "verified". | 1–2 h (GitHub Actions: pytest + eslint) | **P2** |
| 14 | **Frontend has TS toolchain but all `.jsx`** | `frontend/tsconfig*.json`, `@types/*` | TS was set up, never adopted. | Dead config + deps; no type safety despite the appearance of it. | Decide: adopt TS or drop the toolchain | P3 |
| 15 | **K8s manifests diverge from the live VM** | `deploy/k8s/*` | Manifests predate the Cloud-SQL→VM-container migration. | Stale images, missing secrets, in-cluster PG vs VM PG — misleading if someone tries them. | 1 h (mark experimental or update) | P3 |
| 16 | **`frontend/README.md` is the stock Vite template** | `frontend/README.md` | Never replaced. | No frontend-specific onboarding. | 20 min | P3 |
| 17 | **Plug geolocation not modeled** | `models.py` `Plug` (gateway has lat/long, plug doesn't) | Map added before the data model. | `MapComponent` uses random fallback coordinates. | 1 h (add columns + surface in API) | P3 |
| 18 | **`CpoSetup` calls `navigate()` during render** | `frontend/src/pages/cpo/CpoSetup.jsx:26-29` | Redirect placed in the render body, not an effect. | React anti-pattern; warns/asserts under StrictMode, can cause a render loop. | 10 min (`useEffect` or `<Navigate>`) | P3 |
| 19 | **Firmware energy meter resets on reboot** | `tapo_protocol.c` `s_energy_wh` | Integrator is in RAM, not persisted; crash recovery restores `start_energy_kwh` but the meter restarts at 0. | Post-reboot `consumed_kwh` goes negative → energy watchdog can't trip until it re-accumulates. Backend billing is protected by `max(live, persisted)`, so revenue is safe; the *safety limit* is the concern. | 1–2 h (persist integrator to NVS) | P2 |

---

## How to burn it down

1. **This week (P0):** commit the `tools/` secret strip, rotate all burned
   secrets, take MQTT off the public internet (`MQTT_BIND_IP` + firewall).
2. **This month (P1):** lock CORS, adopt Alembic, move money to `Numeric`,
   split `main.py` into routers, add a GitHub Actions CI pipeline.
3. **Backlog (P2/P3):** kill N+1s, unify telemetry transport, cJSON on the
   firmware command path, decide TS-or-not, refresh K8s or mark experimental.
