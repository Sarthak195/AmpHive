# AmpHive EV Charging Platform

AmpHive is an enterprise-grade Software-as-a-Service (SaaS) and Platform-as-a-Service (PaaS) solution that transforms budget, off-the-shelf smart plugs (e.g., TP-Link Tapo P110) into a secure, monetizable, shared EV charging network.

---

## 1. System Overview

The AmpHive platform consists of four core components:
1. **Central API Server:** A FastAPI application orchestrating user wallets, charging session states, database transactions, and client-gateway message routing.
2. **Driver Web App:** A React + Vite single-page application providing EV drivers with a Plug-ID-driven interface to start/stop charging, monitor live sessions, manage prepaid wallets, and join private charger groups via access codes.
3. **Overlay VPN Plane:** A self-hosted Headscale control server that configures secure, encrypted WireGuard overlay tunnels between the server and the gateways, bypassing local firewalls/CGNATs completely.
4. **ESP32 Edge Gateway:** A microcontroller gateway deployed at the charging site that connects to the private VPN, receives MQTT commands, and controls/polls local smart plugs over a dedicated physical VLAN.

---

## 2. Architecture

```
┌──────────────┐        ┌──────────────────┐        ┌──────────────┐
│  Driver App  │◄──────►│  FastAPI Backend  │◄──────►│  PostgreSQL  │
│  (React/Vite)│  REST  │  (Uvicorn)       │  SQL   │  (Cloud SQL) │
└──────────────┘        └────────┬─────────┘        └──────────────┘
       │                         │
       │                         │ MQTT
       │                    ┌────▼─────┐
       │                    │ Mosquitto │
       │                    │  Broker   │
       │                    └────┬─────┘
       │                         │ WireGuard VPN (Headscale)
       │                    ┌────▼─────────────────┐
       │                    │  ESP32-S3 Gateway     │
       │                    │  (Tapo P110 Driver)   │
       │                    └──────────────────────┘
```

---

## 3. Directory Structure

```
AmpHive/
├── docs/                     # ⭐ Technical reference docs (verified vs source)
│   ├── ARCHITECTURE.md       #    System architecture & the two operating modes
│   ├── API_REFERENCE.md      #    All 22 REST endpoints
│   ├── DATA_MODEL.md         #    DB tables, models, enums, schema drift
│   ├── MQTT_CONTRACT.md      #    Backend↔gateway MQTT topic/payload contract
│   ├── FIRMWARE.md           #    ESP32 firmware + microlink Tailscale client
│   ├── DEPLOYMENT.md         #    Compose, deploy scripts, K8s, tools
│   ├── IMPLEMENTATION_STATUS.md  # What works / stub / aspirational + discrepancies
│   └── SECURITY.md           #    Committed secrets, open broker, auth gaps
├── backend/                  # FastAPI Backend Server Code
│   ├── database/
│   │   ├── db.py             #    Async engine + init_db (create_all)
│   │   ├── init_db.py        #    Standalone DB-init helper
│   │   ├── models.py         #    SQLAlchemy ORM models (runtime source of truth)
│   │   ├── schema.sql        #    Reference SQL (NOT executed by the app)
│   │   └── schema_v2.sql     #    Migration delta: charger groups + memberships
│   ├── services/
│   │   ├── auth.py           #    JWT (HS256) + bcrypt + get_current_user dep
│   │   ├── mqtt_manager.py   #    paho-mqtt bridge (publish cmds; inbound = stub)
│   │   ├── payments.py       #    Razorpay orders/verify/webhook
│   │   ├── tapo_direct.py    #    Direct-Mode Tapo driver (lib or HTTP relay)
│   │   └── telemetry.py      #    In-memory TelemetryStore + SSE generator
│   ├── main.py               #    FastAPI app, lifespan, all 22 REST routes
│   ├── Dockerfile            #    python:3.11-slim
│   └── requirements.txt
├── frontend/                 # React + Vite Driver Web Application
│   ├── src/
│   │   ├── api/              #    client.js (fetch wrapper) + mockSse.js (dead)
│   │   ├── components/       #    Navbar, SessionMonitor, WalletCard
│   │   ├── contexts/         #    Auth, Session (real SSE), Wallet
│   │   ├── pages/            #    Home, Login, Session, TopUp, Groups
│   │   ├── styles/           #    global.css (glassmorphic dark theme)
│   │   ├── App.jsx           #    Router & layout shell
│   │   └── main.jsx          #    React DOM entry point
│   ├── Dockerfile            #    Multi-stage build (Node → Nginx)
│   ├── nginx.conf            #    Serves SPA + proxies /api/ → backend:8000
│   └── package.json          #    React 19, React Router 6 (Vite 8)
├── firmware/                 # ESP32-S3 Gateway Firmware (ESP-IDF v5.x)
│   ├── components/           # External submodules (MicroLink Tailscale client)
│   ├── main/                 # C source files (main loop, Tapo drivers, CMake)
│   └── sdkconfig.defaults    # FreeRTOS and PSRAM configuration defaults
├── deploy/                   # All deployment configs, scripts & docs
│   ├── scripts/              # Deployment & bootstrap scripts
│   │   ├── deploy.ps1        # Main GCP VM deployment script
│   │   └── startup.sh        # One-time VM bootstrap (installs Docker)
│   ├── docker/               # Docker Compose definitions
│   │   ├── docker-compose.prod.yml  # Production (deployed to VM)
│   │   └── docker-compose.dev.yml   # Local dev (includes local Postgres)
│   ├── config/               # Service configuration files
│   │   ├── mosquitto.conf    # MQTT broker config
│   │   └── .env.template     # Environment variable template
│   ├── docs/                 # Deployment guides & runbooks
│   │   ├── new_device_setup.md       # New developer machine setup
│   │   ├── deploy_guide.md           # Cloud hosting & VPN networking guide
│   │   ├── deployment_checklist.md   # Step-by-step site deployment
│   │   ├── ec2_deployment_runbook.md # Historical AWS EC2 setup log
│   │   └── gcp_migration_runbook.md  # AWS→GCP & India region migration log
│   └── k8s/                  # Kubernetes (K3s) manifests for cluster scaling
│       ├── namespace.yaml
│       ├── backend.yaml
│       ├── frontend.yaml
│       ├── mosquitto.yaml
│       ├── headscale.yaml
│       └── postgres.yaml
├── tools/                    # Direct-Mode helpers (run on the home PC)
│   ├── relay_server.py       #   HTTP relay the backend's TAPO_RELAY_URL calls
│   ├── local_tapo_test.py    #   Tapo connection self-test
│   └── turn_on.py / turn_off.py  # Manual plug on/off
├── *.bat                     # GCP VM/container helper scripts (start-vm, logs, …)
├── setup_duckdns.sh          # DuckDNS dynamic-DNS updater
├── docker-compose.yml        # Convenience local dev compose (runs from root)
├── agent.md                  # AI-agent context, file map & progress log
├── features_list.md          # Detailed features roadmap & specifications
└── requirements.md           # Product Requirements Document (PRD) & Design Spec
```

---

> ### 📖 Technical Documentation
> The [`docs/`](docs/) folder is the **technical reference** describing what the
> code actually does today (verified against source):
> [Architecture](docs/ARCHITECTURE.md) ·
> [API Reference](docs/API_REFERENCE.md) ·
> [Data Model](docs/DATA_MODEL.md) ·
> [MQTT Contract](docs/MQTT_CONTRACT.md) ·
> [Firmware](docs/FIRMWARE.md) ·
> [Deployment](docs/DEPLOYMENT.md) ·
> [Implementation Status](docs/IMPLEMENTATION_STATUS.md) ·
> [Security Notes](docs/SECURITY.md).
>
> This README and [requirements.md](requirements.md) / [features_list.md](features_list.md)
> describe the *product vision*; for the gap between vision and current code, see
> [Implementation Status](docs/IMPLEMENTATION_STATUS.md).

---

## 4. Implemented & Working Features

### Backend (FastAPI)
* **REST API Endpoints:** Health check, gateway registration, plug registration, session start/stop — all orchestrated via MQTT commands to edge gateways.
* **MQTT Communication Layer:** Telemetry published periodically, commands sent to gateways with Last Will and Testament tracking for automatic offline alerts.
* **Multi-Tenant Database Models (PostgreSQL):** Tables and SQLAlchemy models for CPOs (tenants), gateways, plugs, sessions, users, and prepaid wallets.
* **Lifespan-Managed MQTT Client:** Async context manager ensuring clean broker connect/disconnect on server start/stop.

### Frontend (React + Vite)
* **Plug ID Landing Page (`/`):** Drivers enter the Plug ID (printed on the charging outlet) to start a session. Supports public chargers and access-code-gated private groups.
* **Live Session Monitor (`/session`):** Real-time telemetry display (charging power, current, duration, energy consumed, cost).
* **Wallet Top-Up (`/topup`):** Prepaid coin balance management and top-up interface (Razorpay: UPI, cards, wallets, net banking).
* **Context-Based State Management:** `AuthContext`, `SessionContext`, and `WalletContext` providers for global state.
* **Glassmorphic Dark Theme:** Modern, mobile-responsive UI with frosted-glass aesthetics.

### Infrastructure & Networking
* **Secure VPN Overlay (Headscale/WireGuard):** Automatic node coordination mapping traffic securely over NATs and firewalls.
* **Production Docker Stack:** Multi-container orchestration (Backend + Frontend + MQTT) with health checks.
* **Kubernetes Manifests:** K3s-ready YAML manifests for all services (backend, frontend, MQTT, Headscale, PostgreSQL).

### Firmware (ESP32-S3)
* **Smart Plug Driver:** Embedded TP-Link Tapo P110 protocol driver over local VLAN subnets.
* **Edge Safety Watchdogs:** Auto-shutdown on session duration, capacity limits, or thermal breaches (75°C).
* **VPN Integration:** WireGuard tunnel via MicroLink Tailscale component for secure cloud connectivity.

---

## 5. Quick Start: Local Development

Launch the full stack (Backend + Frontend + MQTT Broker + PostgreSQL) locally using Docker Compose:

1. **Start the containers:**
   ```bash
   docker compose up --build
   ```
2. **Access the services:**
   | Service | URL |
   |---------|-----|
   | **Frontend (Driver App)** | [http://localhost](http://localhost) |
   | **Backend API (Swagger)** | [http://localhost:8000/docs](http://localhost:8000/docs) |
   | **MQTT Broker** | `localhost:1883` |
   | **PostgreSQL** | `localhost:5432` (user: `postgres`, pass: `amphive_dev`) |

---

## 6. Production Cloud Deployment

AmpHive is deployed to **Google Cloud Platform** in the `asia-south1` (Mumbai, India) region.

### Live Infrastructure
| Resource | Name | Specs |
|----------|------|-------|
| **Compute Engine VM** | `amphive-vm-in` | `e2-highcpu-4` (4 vCPU, 4GB RAM), 50GB pd-balanced, `asia-south1-a` |
| **Cloud SQL** | `amphive-db-in` | PostgreSQL 15, `db-f1-micro`, `asia-south1` |

### Live Endpoints
| Service | URL |
|---------|-----|
| **Frontend (Driver App)** | http://35.200.131.98 |
| **Backend API (Swagger)** | http://35.200.131.98:8000/docs |
| **MQTT Broker** | `35.200.131.98:1883` |

### Deploying Updates
```powershell
.\deploy\scripts\deploy.ps1
```
This script waits for Cloud SQL, generates `.env` with the DB IP, SCPs files to the VM, and runs `docker-compose up -d --build`.

### Deployment Documentation
* [new_device_setup.md](deploy/docs/new_device_setup.md) — Step-by-step setup for a new development device
* [deploy_guide.md](deploy/docs/deploy_guide.md) — Cloud hosting & VPN networking guide
* [deployment_checklist.md](deploy/docs/deployment_checklist.md) — Step-by-step site deployment checklist
* [gcp_migration_runbook.md](deploy/docs/gcp_migration_runbook.md) — Full log of AWS→GCP migration and India region migration
* [wireguard_tunnel_setup.md](deploy/docs/wireguard_tunnel_setup.md) — Direct-Mode WireGuard tunnel setup
* [phase2_walkthrough.md](deploy/docs/phase2_walkthrough.md) — Phase-2 work log
* [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Consolidated deployment reference (compose, scripts, K8s)

---

## 7. API Reference

The FastAPI backend exposes **22 REST endpoints** across auth, charger groups,
plugs, gateways, sessions (incl. an SSE live stream), Razorpay payments, and a
Direct-Mode Tapo control surface.

| Group | Endpoints |
|-------|-----------|
| Health | `GET /api/health` |
| Auth | `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me` |
| Groups | `POST /api/groups/join`, `GET /api/groups/my` |
| Plugs | `GET /api/plugs/available`, `GET /api/plugs/{id}`, `POST /api/plugs/register` |
| Gateways | `POST /api/gateways/register` |
| Sessions | `POST /api/sessions/start`, `POST /api/sessions/stop`, `GET /api/sessions/live/{id}` (SSE), `GET /api/sessions/history` |
| Payments | `POST /api/payments/create-order`, `POST /api/payments/verify`, `POST /api/payments/webhook` |
| Direct Mode | `POST /api/direct/plug/on`, `POST /api/direct/plug/off`, `GET /api/direct/plug/info`, `GET /api/direct/plug/energy`, `GET /api/direct/plug/health` |

**Full request/response details:** see [docs/API_REFERENCE.md](docs/API_REFERENCE.md).
Interactive Swagger UI: `http://localhost:8000/docs` when running locally.

---

## 8. Tech Stack

| Layer | Technology |
|-------|------------|
| **Frontend** | React 19, Vite 8, React Router 6, CSS (Glassmorphism) |
| **Backend** | Python 3.11, FastAPI, Uvicorn, SQLAlchemy 2.0 (async), Pydantic |
| **Database** | PostgreSQL 15 (Cloud SQL in production) |
| **Messaging** | Eclipse Mosquitto 2.0 (MQTT) |
| **VPN** | Headscale + WireGuard (Tailscale-compatible) |
| **Firmware** | ESP-IDF v5.x (C), FreeRTOS, MicroLink Tailscale |
| **Containers** | Docker, Docker Compose, Nginx (frontend serving) |
| **Cloud** | Google Cloud Platform (Compute Engine, Cloud SQL) |
| **Orchestration** | Kubernetes (K3s) manifests available |

---

## 9. Setup on a New Development Device

If you are setting up your workspace on a brand new development device, follow these steps to restore the environment:

### Step 1: Clone the Repository & Submodules
Clone this repository and download the referenced context repositories (ChargeHub, ESP32 Tailscale gateway, and Headscale) automatically:
```bash
git clone https://github.com/Sarthak195/AmpHive.git
cd AmpHive
git submodule update --init --recursive
```

### Step 2: Setup Local Python Virtual Environment
Initialize a local environment for backend script editing and testing:
```bash
# Create the virtual environment
python -m venv .venv

# Activate the environment
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux / macOS:
source .venv/bin/activate

# Install backend dependencies
pip install -r backend/requirements.txt
```

### Step 4: Setup Frontend Development
Install Node.js dependencies for the driver web application:
```bash
cd frontend
npm install
npm run dev    # Starts Vite dev server at http://localhost:5173
cd ..
```

### Step 5: Run the Full Local Stack
Make sure you have **Docker Desktop** installed on your new device, then start all services:
```bash
docker compose up --build
```

### Step 6: Configure the ESP-IDF Toolchain
To modify or flash the ESP32 gateway firmware, install the **ESP-IDF** extension (v5.x recommended) in VS Code or follow the command-line setup guide from Espressif:
1. Open the `/firmware` directory in your IDE.
2. Set the build target: `idf.py set-target esp32s3`
3. Flash the gateway: `idf.py -p COMX flash monitor` (replace `COMX` with your serial COM port).

---

## 10. References & Specifications

* **Product Requirements Document:** See [requirements.md](requirements.md) for functional/non-functional requirements, data security frameworks, and CPO-level security designs.
* **Features Roadmap:** See [features_list.md](features_list.md) for the detailed implementation catalog covering Razorpay billing, CPO admin portal, captive portal WiFi provisioning, OTA firmware updates, and dynamic load balancing.
* **Firmware Source:** View [main.c](firmware/main/main.c) to inspect how the ESP32 manages WiFi, connects to the Headscale VPN task, receives broker commands, and runs local session watchdog fail-safes.
