# AmpHive — Maintainer Guide

*For whoever owns this repo next. Written 2026-07-05. Read
[AGENTS.md](../AGENTS.md) and [ARCHITECTURE.md](ARCHITECTURE.md) first; this page
is the operational "how do I actually work on and run this thing" layer.*

---

## 1. Mental model in 60 seconds

AmpHive turns TP-Link Tapo P110 smart plugs into a billed, shared EV-charging
network. A driver enters a **Plug ID** in a React app → FastAPI authorizes and
bills a prepaid **coin** wallet → a command reaches the plug via:

- **Direct MQTT (the operating path since 2026-07-10):** backend → MQTT →
  **ESP32-C3 gateway**, which dials *outbound* to `mqtts://mqtt.amphive.app:8883`
  (public), authenticated by TLS plus per-gateway username/password and topic
  ACLs → local Tapo plug (on-device driver is KLAP v2). Firmware builds with
  `AMPHIVE_DIRECT_MQTT=1`; there is no VPN/overlay hop on devices.

*(Historical: two earlier paths are retired — a Headscale/WireGuard overlay
that gateways once tunneled through, and a separate "Direct Mode" that had the
backend call a relay/tapo lib over a WireGuard tunnel to a plug on the
developer's home LAN. The `tapo_direct` / `/direct/*` backend code remains but
is dormant.)

## 2. Where things live

| You want to change… | Go to |
|---|---|
| A REST endpoint | `backend/routers/{auth,groups,plugs,sessions,payments,direct,cpo}.py` (since the 2026-07-07 split; `main.py` is assembly only) |
| A request/response schema | `backend/schemas.py` |
| Runtime handles (mqtt_manager, telemetry_store, …) | `backend/state.py` (set by the lifespan — always access as `state.x`, never from-import) |
| Auth / JWT / password hashing | `backend/services/auth.py` |
| Role gating on `/api/cpo/*` | `backend/services/rbac.py` |
| MQTT publish/ingest contract | `backend/services/mqtt_manager.py` + [MQTT_CONTRACT.md](MQTT_CONTRACT.md) |
| Live telemetry (WebSocket) | `backend/services/socketio_manager.py` + `services/telemetry.py` |
| Time-series persistence | `backend/services/telemetry_persistence.py` |
| Payments (Razorpay) | `backend/services/payments.py` |
| DB tables | `backend/database/models.py` (**runtime source of truth**, not the `.sql` files) |
| Driver UI | `frontend/src/pages/*`, `components/*`, `contexts/*` |
| CPO operator portal | `frontend/src/pages/cpo/*` |
| ESP32-C3 firmware | `firmware/main/*.c` (app); the old `firmware/components/microlink/*` overlay client is retired/compiled out |
| Deploy | `deploy/scripts/deploy.ps1`, `deploy/docker/docker-compose.prod.yml` |

## 3. Golden rules (from AGENTS.md — do not break these)

1. **Don't run the app stack or a database on this Windows box** (dev workstation
   — no local `docker compose up`, no local Postgres, no migrations/seeds against
   a local DB). Operating the GCP VM (`amphive-vm-in`, `asia-south1-a`) *from*
   this box is allowed — `deploy.ps1`, `gcloud compute ssh`, secret rotation;
   confirm before destructive/irreversible VM actions. (Updated 2026-07-05.)
2. **Deploy via the script**, not by hand: `.\deploy\scripts\deploy.ps1`. It
   validates `.env` (aborts on a weak/default `JWT_SECRET_KEY`), tars
   `backend` + `frontend`, copies configs, and rebuilds containers on the VM.
3. **Don't commit secrets.** App `.env` is gitignored. Several already-committed
   secrets still need rotation — see [SECURITY.md](SECURITY.md).
4. **`docs/` is the source of truth.** Update it (not scattered root copies) when
   behavior changes, and record works/stub/aspirational deltas in
   [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## 4. Local development (editing, not deploying)

```bash
# backend deps for editing / running pytest
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest backend/tests            # DB-free unit tests

# frontend
cd frontend && npm install && npm run dev   # Vite dev server :5173
```
The `docker-compose.yml` at the root exists for convenience, but per rule #1,
run the full stack on the VM, not here.

## 5. Deploy checklist

1. `.env` exists at repo root with a **strong** `JWT_SECRET_KEY` (≥32 chars),
   `POSTGRES_PASSWORD`, Razorpay keys, and `MQTT_BIND_IP` set to the VM's overlay
   IP (not `0.0.0.0`).
2. `gcloud` is authenticated and can SSH to `amphive-vm-in`.
3. Run `.\deploy\scripts\deploy.ps1`. It prints the frontend/backend URLs at the
   end.
4. Verify: `GET http://<vm-ip>:8000/api/health` and the frontend loads.
5. First boot only: seed with `docker exec -it amphive-backend python seed.py`
   (default accounts, all `password123`).

## 6. Runbooks
Live infra procedures are in [`deploy/docs/`](../deploy/docs/): device setup,
GCP migration log, WireGuard tunnel setup, deployment checklist. **Log real
commands you run against the VM there.**

## 7. Gotchas that will bite you

- **The VM public IP is ephemeral** and is recorded inconsistently across docs.
  Always re-check with `gcloud compute instances list`. Prefer the DuckDNS name.
- **Schema changes are Alembic revisions** (since 2026-07-07): add a revision
  under `backend/migrations/versions/` (`alembic -c backend/alembic.ini
  revision --autogenerate` — needs a reachable DB: the CI postgres or the VM;
  never edit `0001_baseline`). Startup runs `upgrade head` automatically and
  stamps pre-Alembic databases. The old `_INPLACE_UPGRADES` and `.sql` files
  are gone; CI fails if the migrations drift from the models.
- **Parallel-branch migration renumbering:** revisions are sequential,
  zero-padded (`00NN_...`), and must form a single linear head — no forked
  history. When two branches add migrations in parallel, the branch that
  merges *second* renumbers its revision (and `down_revision`) to sit after
  the first branch's, before merging. Run `test_migrations.py` to confirm a
  single linear chain before merging.
- **Money is `NUMERIC(12,2)`/Decimal** (since 2026-07-06); route all wallet
  math through `services/money.to_money`, never raw floats.
- **MQTT requires authentication** (since 2026-07-07): broker credentials come
  from `.env` (`MQTT_*`), and every gateway needs `mqtt_user`/`mqtt_pwd` in
  NVS — an unprovisioned gateway cannot connect.
- **Firmware builds on ESP-IDF v5.3.3**, not v6 (v6 causes a LoadProhibited panic
  on custom netif registration); use v5.3.x.
- **Live telemetry is Socket.io only** (the legacy SSE endpoint was retired
  2026-07-07).

## 8. First things to fix if you inherit this today
See [TODO.md](TODO.md) and [TECH_DEBT.md](TECH_DEBT.md). The top three:
1. Commit the `tools/` secret strip and **rotate** all burned secrets.
2. Take MQTT off the public internet.
3. Add CI (pytest + eslint) so "verified" means something.
