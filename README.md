# AmpHive — Shared EV Charging Platform

AmpHive turns budget, off-the-shelf smart plugs (TP-Link Tapo P110) into a secure,
monetizable, shared EV charging network. A driver enters a **Plug ID** in a web
app, the backend authorizes and bills against a prepaid coin wallet, and the
command reaches the plug — either through an ESP32 gateway over an encrypted
overlay network, or (today, for dev/test) directly over a WireGuard tunnel.

```
┌──────────────┐        ┌───────────────────┐        ┌────────────────────┐
│  Driver App  │◄──────►│  FastAPI Backend  │◄──────►│  PostgreSQL 15     │
│ (React/Vite) │  REST  │    (Uvicorn)      │  SQL   │ (Docker on the VM) │
└──────────────┘  +SSE  └────────┬──────────┘        └────────────────────┘
                                 │ MQTT
                            ┌────▼─────┐
                            │ Mosquitto │
                            └────┬─────┘
                                 │ WireGuard / Headscale overlay
                            ┌────▼──────────────────┐
                            │  ESP32-S3 Gateway      │
                            │  (Tapo P110 driver)    │
                            └───────────────────────┘
```

---

## 📖 Documentation

The **[`docs/`](docs/)** folder is the single source of truth — it describes what
the code actually does today, verified against source.

| | |
|--|--|
| [Architecture](docs/ARCHITECTURE.md) | End-to-end system + the two operating modes |
| [API Reference](docs/API_REFERENCE.md) | All 35 REST endpoints |
| [Data Model](docs/DATA_MODEL.md) | Tables, ORM models, enums, schema drift |
| [Dependencies](docs/DEPENDENCIES.md) | Import graphs, packages, high-impact files |
| [MQTT Contract](docs/MQTT_CONTRACT.md) | Backend ↔ gateway topic/payload contract |
| [Firmware](docs/FIRMWARE.md) | ESP32 firmware + `microlink` overlay client |
| [Deployment](docs/DEPLOYMENT.md) | GCP VM + Docker Compose, scripts, K8s |
| [Implementation Status](docs/IMPLEMENTATION_STATUS.md) | Works / stub / aspirational + discrepancies |
| [Security](docs/SECURITY.md) | Open gaps, committed secrets, remediation |

This README plus [requirements.md](requirements.md) and
[features_list.md](features_list.md) describe the **product vision**; for the gap
between vision and current code, see
[Implementation Status](docs/IMPLEMENTATION_STATUS.md). Contributing (human or AI)?
Read [AGENTS.md](AGENTS.md).

---

## Repository layout

```
AmpHive/
├── docs/          Technical reference (source of truth) — see above
├── backend/       FastAPI app: main.py (all routes), services/, database/, seed.py
├── frontend/      React 19 + Vite SPA: driver app + CPO operator portal
├── firmware/      ESP32-S3 (ESP-IDF) gateway: microlink overlay client + Tapo driver
├── deploy/        Docker Compose (dev/prod), K8s manifests, configs, runbooks
├── scripts/       Windows ops helpers (VM start/stop, remote compose/logs, DuckDNS)
├── tools/         Direct-Mode Tapo helpers (run on the home PC)
├── context_repos/ Read-only reference submodules (ChargeHub, headscale, ESP32-WoL)
├── docker-compose.yml   Local-dev convenience (mirrors deploy/docker/docker-compose.dev.yml)
├── AGENTS.md / CLAUDE.md   Contributor + AI-agent guidance
├── requirements.md / features_list.md   Product vision & roadmap
```

---

## Quick start (local development)

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

The FastAPI backend exposes **35 REST endpoints**. Full details in
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
| Messaging | Eclipse Mosquitto 2.0 (MQTT) |
| Overlay VPN | Headscale + WireGuard (Tailscale-compatible) |
| Firmware | ESP-IDF v5.x (C), FreeRTOS, custom `microlink` Tailscale client |
| Cloud | Google Cloud Platform (Compute Engine `e2-standard-2`, static IP, `asia-south1`) |

---

## Production deployment

Live on GCP (`asia-south1`, Mumbai) at static IP **`8.231.81.12`**
(frontend `:80`, backend `:8000/docs`, MQTT `:1883`). Ship updates with:

```powershell
.\deploy\scripts\deploy.ps1
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and the runbooks in
[`deploy/docs/`](deploy/docs/) (device setup, cloud/VPN guide, deployment
checklist, GCP migration log, WireGuard tunnel setup).

---

## Setup on a new development device

```bash
# 1. Clone with submodules
git clone https://github.com/Sarthak195/AmpHive.git
cd AmpHive
git submodule update --init --recursive

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
`idf.py set-target esp32s3`, then `idf.py -p COMX flash monitor`. See
[docs/FIRMWARE.md](docs/FIRMWARE.md).
