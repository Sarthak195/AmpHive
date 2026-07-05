# AmpHive — Maintainer Guide

*For whoever owns this repo next. Written 2026-07-05. Read
[AGENTS.md](../AGENTS.md) and [ARCHITECTURE.md](ARCHITECTURE.md) first; this page
is the operational "how do I actually work on and run this thing" layer.*

---

## 1. Mental model in 60 seconds

AmpHive turns TP-Link Tapo P110 smart plugs into a billed, shared EV-charging
network. A driver enters a **Plug ID** in a React app → FastAPI authorizes and
bills a prepaid **coin** wallet → a command reaches the plug one of two ways:

- **Path A (product design):** backend → MQTT → **ESP32-S3 gateway** (over a
  Headscale/WireGuard overlay) → local Tapo plug. Backend-complete; the on-device
  Tapo driver is real (KLAP v2) but the end-to-end billed session on hardware
  is not yet the committed reality.
- **Path B (current dev/test reality):** backend calls a **relay/tapo lib** over
  a WireGuard tunnel straight to a plug on the developer's home LAN. This is what
  the committed `.env` enables (`DIRECT_MODE`).

Everything cloud-side is identical between the two; only the last hop differs.

## 2. Where things live

| You want to change… | Go to |
|---|---|
| A REST endpoint or its schema | `backend/main.py` (all 37 routes + all Pydantic models are here) |
| Auth / JWT / password hashing | `backend/services/auth.py` |
| Role gating on `/api/cpo/*` | `backend/services/rbac.py` |
| MQTT publish/ingest contract | `backend/services/mqtt_manager.py` + [MQTT_CONTRACT.md](MQTT_CONTRACT.md) |
| Live telemetry (WebSocket) | `backend/services/socketio_manager.py` + `services/telemetry.py` |
| Time-series persistence | `backend/services/telemetry_persistence.py` |
| Payments (Razorpay) | `backend/services/payments.py` |
| DB tables | `backend/database/models.py` (**runtime source of truth**, not the `.sql` files) |
| Driver UI | `frontend/src/pages/*`, `components/*`, `contexts/*` |
| CPO operator portal | `frontend/src/pages/cpo/*` |
| ESP32 firmware | `firmware/main/*.c` (app) + `firmware/components/microlink/*` (overlay client) |
| Deploy | `deploy/scripts/deploy.ps1`, `deploy/docker/docker-compose.prod.yml` |

## 3. Golden rules (from AGENTS.md — do not break these)

1. **⛔ Never deploy or test on this Windows box.** No `docker compose up`,
   `deploy.ps1`, migrations, or seeds locally. All deploy/test happens on the GCP
   VM (`amphive-vm-in`, `asia-south1-a`).
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
- **`create_all` never alters existing tables.** New columns need a hand-written
  idempotent `ALTER` in `db.py:_INPLACE_UPGRADES` (there is no migration tool).
  The `.sql` files are reference-only and have drifted.
- **Money is stored as `Float`.** Expect rounding; don't compare balances for
  exact equality.
- **MQTT is anonymous and (by default) public.** Forged telemetry feeds billing.
  Set `MQTT_BIND_IP` and drop the public 1883 firewall rule before real users.
- **Firmware builds on ESP-IDF v5.3.3**, not v6 (v6 causes a LoadProhibited panic
  on custom netif registration). `eim_config.toml` mentions v6.0.1 — ignore it
  for building; use v5.3.x.
- **Two live-telemetry transports exist** (SSE + Socket.io). The frontend uses
  Socket.io; the SSE endpoint is legacy/fallback.

## 8. First things to fix if you inherit this today
See [TODO.md](TODO.md) and [TECH_DEBT.md](TECH_DEBT.md). The top three:
1. Commit the `tools/` secret strip and **rotate** all burned secrets.
2. Take MQTT off the public internet.
3. Add CI (pytest + eslint) so "verified" means something.
