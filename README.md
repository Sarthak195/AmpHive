# AmpHive — Shared EV Charging Platform

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

AmpHive turns budget, off-the-shelf smart plugs (TP-Link Tapo P110) into a secure,
monetizable, shared EV charging network. A driver enters a **Plug ID** in a web
app, the backend authorizes and bills against a prepaid coin wallet, and the
command reaches the plug through an ESP32 gateway that dials outbound over
direct TLS MQTT — no VPN or overlay network involved.

> **Note:** AmpHive is a **portfolio / showcase project**. The hosted instance is
> a **live demo**, not a commercial service — it runs in sandbox mode (no real
> payments) and is published as a reference implementation.

---

## Safety & Disclaimer

**⚠️ WARNING: This software controls high-voltage electrical hardware and is NOT certified for production use.** It is provided for reference and educational purposes only and contains known safety-relevant defects. **DO NOT deploy this code to control real charging equipment.** Improper use can cause fire, electric shock, equipment damage, injury or death. The billing/payment code is unaudited with known money-handling bugs and must not process real payments. **See [`SAFETY.md`](SAFETY.md) and [`LICENSE`](LICENSE) before use.**

---

```
┌──────────────┐        ┌───────────────────┐        ┌────────────────────┐
│  Driver App  │◄──────►│  FastAPI Backend  │◄──────►│  PostgreSQL 15     │
│ (React/Vite) │  REST  │    (Uvicorn)      │  SQL   │ (Docker on the VM) │
└──────────────┘  +SSE  └────────┬──────────┘        └────────────────────┘
                                 │ MQTT
                            ┌────▼─────┐
                            │ Mosquitto │
                            └────┬─────┘
                                 │ direct MQTT over TLS (mqtts://mqtt.amphive.app:8883)
                            ┌────▼──────────────────┐
                            │  ESP32-C3 Gateway      │
                            │  (Tapo P110 driver)    │
                            └───────────────────────┘
```

---

## 📖 Documentation

The **[`docs/`](docs/)** folder is the single source of truth — it describes what
the code actually does today, verified against source. Start at
[docs/README.md](docs/README.md) for the full index.

| | |
|--|--|
| [Architecture](docs/ARCHITECTURE.md) | End-to-end system + the two operating modes |
| [API Reference](docs/API_REFERENCE.md) | All REST endpoints |
| [Data Model](docs/DATA_MODEL.md) | Tables, ORM models, enums, schema drift |
| [Dependencies](docs/DEPENDENCIES.md) | Import graphs, packages, high-impact files |
| [MQTT Contract](docs/MQTT_CONTRACT.md) | Backend ↔ gateway topic/payload contract |
| [Firmware](docs/FIRMWARE.md) | ESP32 firmware + direct MQTT client |
| [Deployment](docs/DEPLOYMENT.md) | GCP VM + Docker Compose, scripts, K8s |
| [Implementation Status](docs/IMPLEMENTATION_STATUS.md) | Works / stub / aspirational + discrepancies |
| [Security](docs/SECURITY.md) | Open gaps, committed secrets, remediation |

This README plus [requirements.md](requirements.md) and
[features_list.md](features_list.md) describe the **product vision**; for the gap
between vision and current code, see
[Implementation Status](docs/IMPLEMENTATION_STATUS.md). Contributing (human or AI)?
Read [AGENTS.md](AGENTS.md).

---

## Repository structure

```
AmpHive/
├── backend/       FastAPI app: main.py (assembly/lifespan only), routers/*.py (routes), services/, database/, seed.py
├── frontend/      React 19 + Vite SPA: driver app + CPO operator portal
├── firmware/      ESP32-C3 (ESP-IDF) gateway: direct MQTT client + Tapo driver
├── agent/         Software gateway ("AmpHive Agent"): discovers LAN smart plugs (Kasa/Shelly/sim), speaks the same MQTT contract as a firmware gateway
├── deploy/        Docker Compose (dev/prod), K8s manifests, configs, runbooks
├── docs/          Technical reference (source of truth) — see below
├── tools/         Direct-Mode Tapo helpers (run on the home PC)
├── scripts/       Windows ops helpers (VM start/stop, remote compose/logs)
├── docker-compose.yml   Local-dev convenience (mirrors deploy/docker/docker-compose.dev.yml)
├── AGENTS.md / CLAUDE.md   Contributor + AI-agent guidance
├── requirements.md / features_list.md   Product vision & roadmap
```

---

## Getting started (local development)

**Backend** — Python 3.11, tests only need a venv (no live Postgres for the
non-DB-gated suite):

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1   |   Linux/macOS:  source .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest backend/tests
```

**Frontend** — Node 20:

```bash
cd frontend
npm ci
npm run dev      # Vite dev server at http://localhost:5173
npm run build    # production build
npx vitest run   # test suite
```

> **This repo does not run the full application stack (backend + PostgreSQL +
> MQTT broker) or a database locally.** The shared, canonical environment is
> the GCP VM, operated via `deploy/scripts/deploy.ps1`. See
> [AGENTS.md](AGENTS.md) for the full rule and the reasoning behind it. The
> `docker compose up` flow below is kept for optional full-stack exploration,
> but day-to-day development, tests, and CI all work from the commands above.

---

## Quick start (Docker, full stack)

Launch the full stack (backend + frontend + MQTT + PostgreSQL) with Docker:

```bash
docker compose up --build
```

| Service | URL |
|---------|-----|
| Frontend (Driver App) | http://localhost |
| Backend API (Swagger) | http://localhost:8000/docs |
| MQTT Broker | `localhost:1883` |
| PostgreSQL | `localhost:5432` (user `postgres`, pass `amphive_dev`) |

Seed sample data (tenants, CPOs, drivers, plugs, sessions; all passwords
`password123`):

```bash
docker exec -it amphive-backend-dev python seed.py
```

---

## API at a glance

The FastAPI backend exposes **86 REST endpoints** across 10 routers. Full details in
[docs/API_REFERENCE.md](docs/API_REFERENCE.md); interactive Swagger at
`http://localhost:8000/docs`.

| Group | Endpoints |
|-------|-----------|
| Health | `GET /api/health` |
| Auth | `register`, `login`, `me` |
| Groups | `join`, `my` |
| Plugs | `available`, `{id}` |
| Sessions | `start`, `stop`, `live/{id}` (SSE), `history` |
| Payments | `create-order`, `verify`, `webhook` (Razorpay) |
| Direct Mode | `plug/on`, `plug/off`, `plug/info`, `plug/energy`, `plug/health` |
| CPO Portal | `setup`, `profile`, gateway/plug/group CRUD, analytics (overview/sessions/revenue/energy) |

> Gateway/plug provisioning is done through the RBAC-gated `/api/cpo/*` endpoints
> (the old unauthenticated `gateways/register` / `plugs/register` were removed).

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Frontend | React 19, Vite 8, React Router 6, Leaflet map, Recharts, hand-written CSS |
| Backend | Python 3.11, FastAPI, Uvicorn, SQLAlchemy 2.0 (async), Pydantic |
| Database | PostgreSQL 15 (Docker container on the VM — no Cloud SQL) |
| Messaging | Eclipse Mosquitto 2.0 (MQTT), direct TLS on `:8883` |
| Reverse proxy / TLS | Caddy (HTTPS on `amphive.app` / `cpo.amphive.app`) |
| Firmware | ESP-IDF v5.x (C), FreeRTOS, direct MQTT client (no VPN/overlay) |
| Cloud | Google Cloud Platform (Compute Engine `e2-standard-2`, static IP, `asia-south1`) |

---

## Production deployment

Live on GCP (`asia-south1`, Mumbai), reached via **`amphive.app`** (driver app)
and **`cpo.amphive.app`** (CPO operator portal) over HTTPS through Caddy. The
backend on `:8000` is firewalled to the VM only; MQTT is TLS-only on `:8883`
(plaintext `:1883` is not host-published). Ship updates with:

```powershell
.\deploy\scripts\deploy.ps1
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and the runbooks in
[`deploy/docs/`](deploy/docs/) (new-device setup, GCP migration, gateway
provisioning, and more).

---

## Setup on a new development device

```bash
# 1. Clone
git clone https://github.com/Sarthak195/AmpHive.git
cd AmpHive

# 2. Python venv for backend script editing
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1   |   Linux/macOS:  source .venv/bin/activate
pip install -r backend/requirements.txt

# 3. Frontend deps
cd frontend && npm install && npm run dev   # Vite dev server at http://localhost:5173
cd ..

# 4. Full local stack (requires Docker Desktop)
docker compose up --build
```

**Firmware:** open `firmware/` in an ESP-IDF v5.x environment,
`idf.py set-target esp32c3`, then `idf.py -p COMX flash monitor`. See
[docs/FIRMWARE.md](docs/FIRMWARE.md).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, test/lint commands, and PR
conventions.

## License

MIT — see [LICENSE](LICENSE).
