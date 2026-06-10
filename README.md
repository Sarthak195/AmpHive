# AmpHive EV Charging Platform

AmpHive is an enterprise-grade Software-as-a-Service (SaaS) and Platform-as-a-Service (PaaS) solution that transforms budget, off-the-shelf smart plugs (e.g., TP-Link Tapo P110) into a secure, monetizable, shared EV charging network.

---

## 1. System Overview

The AmpHive platform consists of four core components:
1. **Central API Server:** A FastAPI application orchestrating user wallets, charging session states, database transactions, and client-gateway message routing.
2. **Driver Web App:** A React + Vite single-page application providing EV drivers with a QR-code-driven interface to start/stop charging, monitor live sessions, and manage prepaid wallets.
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
├── backend/                  # FastAPI Backend Server Code
│   ├── database/             # PostgreSQL schemas (schema.sql) & SQLAlchemy models
│   ├── services/             # Background services (MQTT connection manager)
│   ├── main.py               # FastAPI app entry point & REST endpoints
│   ├── Dockerfile            # Container build specification
│   └── requirements.txt      # Python backend dependencies
├── frontend/                 # React + Vite Driver Web Application
│   ├── src/
│   │   ├── api/              # Backend API client functions
│   │   ├── components/       # Reusable UI (Navbar, SessionMonitor, WalletCard)
│   │   ├── contexts/         # React Context providers (Auth, Session, Wallet)
│   │   ├── pages/            # Route pages (Home, Session, TopUp)
│   │   ├── styles/           # CSS stylesheets (glassmorphic dark theme)
│   │   ├── App.jsx           # Router & layout shell
│   │   └── main.jsx          # React DOM entry point
│   ├── Dockerfile            # Multi-stage build (Node → Nginx)
│   ├── nginx.conf            # Reverse proxy config for SPA routing
│   └── package.json          # Node dependencies (React 19, React Router 6)
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
├── docker-compose.yml        # Convenience local dev compose (runs from root)
├── features_list.md          # Detailed features roadmap & specifications
└── requirements.md           # Product Requirements Document (PRD) & Design Spec
```

---

## 4. Implemented & Working Features

### Backend (FastAPI)
* **REST API Endpoints:** Health check, gateway registration, plug registration, session start/stop — all orchestrated via MQTT commands to edge gateways.
* **MQTT Communication Layer:** Telemetry published periodically, commands sent to gateways with Last Will and Testament tracking for automatic offline alerts.
* **Multi-Tenant Database Models (PostgreSQL):** Tables and SQLAlchemy models for CPOs (tenants), gateways, plugs, sessions, users, and prepaid wallets.
* **Lifespan-Managed MQTT Client:** Async context manager ensuring clean broker connect/disconnect on server start/stop.

### Frontend (React + Vite)
* **QR-Code Landing Page (`/`):** Drivers scan a QR code on the charging outlet to land on the app with their plug pre-selected.
* **Live Session Monitor (`/session`):** Real-time telemetry display (charging power, current, duration, energy consumed, cost).
* **Wallet Top-Up (`/topup`):** Prepaid coin balance management and top-up interface.
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
* [ec2_deployment_runbook.md](deploy/docs/ec2_deployment_runbook.md) — Historical AWS EC2 setup log

---

## 7. API Reference

The FastAPI backend exposes the following REST endpoints:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Service health check |
| `POST` | `/api/gateways/register` | Register a new ESP32 gateway |
| `POST` | `/api/plugs/register` | Register a smart plug on a gateway's VLAN |
| `POST` | `/api/sessions/start` | Start a charging session (sends MQTT ON command) |
| `POST` | `/api/sessions/stop` | Stop a charging session (sends MQTT OFF command) |

Full interactive documentation is available at the [Swagger UI](http://localhost:8000/docs) when running locally.

---

## 8. Tech Stack

| Layer | Technology |
|-------|------------|
| **Frontend** | React 19, Vite 8, React Router 6, CSS (Glassmorphism) |
| **Backend** | Python 3.12, FastAPI, Uvicorn, SQLAlchemy, Pydantic |
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

### Step 2: Restore and Secure the EC2 Private Key
1. Securely copy the private key `AmpHive EC2.pem` from your old device or backups and place it in the root folder of this project.
2. **Restrict Key Permissions (Required for OpenSSH):**
   * **On Windows (PowerShell):** Open SSH requires tight key permissions to work. Run these commands to restrict access only to your Windows user:
     ```powershell
     icacls.exe ".\AmpHive EC2.pem" /reset
     icacls.exe ".\AmpHive EC2.pem" /grant:r "$($env:USERNAME):(R)"
     icacls.exe ".\AmpHive EC2.pem" /inheritance:r
     ```
   * **On Linux / macOS:** Run:
     ```bash
     chmod 400 "AmpHive EC2.pem"
     ```

### Step 3: Setup Local Python Virtual Environment
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
* **Features Roadmap:** See [features_list.md](features_list.md) for the detailed implementation catalog covering Stripe billing, CPO admin portal, captive portal WiFi provisioning, OTA firmware updates, and dynamic load balancing.
* **Firmware Source:** View [main.c](firmware/main/main.c) to inspect how the ESP32 manages WiFi, connects to the Headscale VPN task, receives broker commands, and runs local session watchdog fail-safes.
