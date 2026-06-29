# AmpHive: AI Agent Context & Features Roadmap

Welcome to the **AmpHive** repository. This document serves as the **source of truth** and alignment context for AI assistants and developers working on this project across different conversation sessions. 

Before making any changes or starting a task, read this document to understand what is built, what is remaining, and the architectural rules of the project.

---

## ⚡ CRITICAL RULE: HIGH DOCUMENTATION MODE ENABLED ⚡

All developers and AI agents working in this repository **must** operate under **High Documentation Mode**. This ensures that the codebase remains maintainable and that Sarthak can track, replicate, and debug every development step easily.

### 1. Document Every Code Change
* For any modification, refactor, or addition to the source code, write a clear, descriptive comment explaining the change, why it was made, and any potential side effects.
* Preserve all existing comments and docstrings unless they are outdated or explicitly requested to be changed.

### 2. Update Walkthroughs & Runbooks
* After finishing a task or testing step, update the active walkthrough file in the workspace: [walkthrough.md](file:///C:/Users/Sarthak/.gemini/antigravity-ide/brain/2c7d4db5-53a4-4914-93d2-a6c1c7a278fc/walkthrough.md).
* If you run any setup commands, install new packages, or modify configuration files on the GCP VM instance, you **must** append these actions to the deployment runbook.

### 3. Expose Architectural Decisions
* If you make an design decision (e.g., choosing a specific library, data schema, or networking port), document the trade-offs and rationale inside the code comments and relevant markdown files.

---

## 1. System Architecture & Tech Stack

AmpHive is a shared EV charging PaaS connecting 3rd-party smart plugs to a central cloud server via ESP32 gateways over a private, encrypted VPN tunnel.

```
┌────────────────────────────────────────────────────────────────────────┐
│                      AMPHIVE CLOUD CONTROL PLANE                       │
│                                                                        │
│  [ FastAPI API Server ] ◄──► [ PostgreSQL ] ◄──► [ Timeseries DB ]     │
│          ▲                                                             │
│          │ (Commands & Telemetry)                                      │
│          ▼                                                             │
│  [ EMQX/Mosquitto MQTT ]                                               │
│          ▲                                                             │
│          │ (Routed inside WireGuard Tunnel)                            │
│          ▼                                                             │
│  [ Headscale VPN Server ] ◄────────────────────────┐                   │
└──────────┬─────────────────────────────────────────┼───────────────────┘
           │ (Noise Crypt Tunnel over WAN)           │
           ▼                                         │ (VLAN 20 Outbound)
   [ ESP32 Edge Gateway ] ◄──────────────────────────┘
           │ (VLAN 20 Local Network)
           ▼
   [ Smart Plugs (Tapo P110) ]
```

### Stack Components:
* **Backend:** Python 3.11, FastAPI (Async), SQLAlchemy 2.0 (PostgreSQL driver).
* **Broker:** Eclipse Mosquitto MQTT (exchanging JSON telemetry/commands).
* **VPN Plane:** Headscale (self-hosted control plane for Tailscale WireGuard client nodes).
* **Firmware:** ESP-IDF v5.x (C), incorporating the `microlink` Tailscale client component.
* **Orchestration:** Docker Compose (local development and live cloud hosting).

---

## 2. File Directory Map

> **📖 Verified technical reference lives in [`/docs/`](file:///c:/Users/Sarthak/Documents/AmpHive/docs/).**
> Start with [docs/IMPLEMENTATION_STATUS.md](file:///c:/Users/Sarthak/Documents/AmpHive/docs/IMPLEMENTATION_STATUS.md)
> for what actually works vs. what is a stub/aspirational, and
> [docs/SECURITY.md](file:///c:/Users/Sarthak/Documents/AmpHive/docs/SECURITY.md) for committed-secret/auth gaps.
> Other refs: ARCHITECTURE, API_REFERENCE, DATA_MODEL, MQTT_CONTRACT, FIRMWARE, DEPLOYMENT.

* `/backend/`: FastAPI server files (single-module app; all 22 routes in `main.py`).
  * `/backend/main.py`: REST routes (auth, groups, plugs, gateways, sessions+SSE, payments, direct) and lifespan startup/shutdown.
  * `/backend/services/auth.py`: JWT (HS256, 7-day) + bcrypt password hashing + `get_current_user` dependency.
  * `/backend/services/mqtt_manager.py`: MQTT client. Publishes ON/OFF commands; inbound telemetry feeds the `TelemetryStore` (for live SSE stream) and persists to DB (plug power, session energy, peak power), and gateway status updates online/offline state in the DB.
  * `/backend/services/payments.py`: Razorpay create-order / verify / webhook (webhook only logs, no auto-credit yet).
  * `/backend/services/telemetry.py`: In-memory `TelemetryStore` singleton + SSE generator. **No TimescaleDB** anywhere.
  * `/backend/services/tapo_direct.py`: [Direct Mode] Tapo P110 driver (local `tapo` lib or HTTP relay via `TAPO_RELAY_URL`).
  * `/backend/database/models.py`: SQLAlchemy ORM models — **runtime source of truth** (`init_db` calls `create_all`).
  * `/backend/database/db.py` & `init_db.py`: async engine + DB initialization helpers.
  * `/backend/database/schema.sql` & `schema_v2.sql`: reference SQL **(not executed by the app)**; v2 adds charger groups + memberships.
* `/deploy/`: All deployment infrastructure.
  * `/deploy/scripts/deploy.ps1`: Main GCP VM deployment script.
  * `/deploy/scripts/startup.sh`: One-time VM bootstrap (installs Docker).
  * `/deploy/docker/docker-compose.prod.yml`: Production Docker Compose for the VM.
  * `/deploy/docker/docker-compose.dev.yml`: Local development Docker Compose (includes local Postgres).
  * `/deploy/config/mosquitto.conf`: MQTT broker configuration.
  * `/deploy/config/amphive_tunnel.conf`: WireGuard client config for the developer's PC.
  * `/deploy/config/.env.template`: Environment variable template.
  * `/deploy/docs/new_device_setup.md`: Setup instructions for configuring developer tools and code on a new workstation.
  * `/deploy/docs/deploy_guide.md`: Public cloud hosting, dynamic DNS, and VPN network setup manual.
  * `/deploy/docs/deployment_checklist.md`: Step-by-step physical deployment guide.
  * `/deploy/docs/gcp_migration_runbook.md`: Full log of AWS→GCP migration and India region migration commands.
  * `/deploy/docs/wireguard_tunnel_setup.md`: WireGuard tunnel setup guide for direct Tapo P110 control.
  * `/deploy/k8s/`: Kubernetes manifests (namespace, postgres, mosquitto, headscale, backend).
* `/frontend/`: React 19 + Vite SPA. Pages: Home, Login, Session, TopUp, Groups. Contexts: Auth/Session(real SSE)/Wallet. `api/mockSse.js` is dead leftover code.
* `/firmware/`: ESP32 gateway codebase.
  * `/firmware/main/main.c`: Main loop managing STA WiFi, captive portal, VPN connection, MQTT subscriber, and telemetry watchdogs (duration/energy/75°C thermal). Active session is RAM-only.
  * `/firmware/main/tapo_protocol.c`: **MOCK** Tapo P110 driver — no KLAP/AES; returns simulated telemetry. Must be replaced for real plug control.
  * `/firmware/components/microlink/`: Substantial from-scratch Tailscale-protocol client (Noise/ts2021, DERP, DISCO, STUN, WireGuard).
  * `/firmware/components/wireguard_lwip/`: Vendored WireGuard-over-lwIP library.
* `/tools/`: Direct-Mode helpers run on the home PC (`relay_server.py`, `local_tapo_test.py`, `turn_on/off.py`).

---

## 3. Deployment Context: Live GCP Infrastructure (asia-south1, India)

### Compute Engine VM
| Property | Value |
|----------|-------|
| **Instance Name** | `amphive-vm-in` |
| **Zone** | `asia-south1-a` (Mumbai) |
| **Machine Type** | `e2-highcpu-4` (4 vCPU, 4GB RAM) |
| **Boot Disk** | 50GB Balanced Persistent Disk |
| **Public IP** | `34.100.200.152` (ephemeral — may change on VM restart) |
| **OS** | Debian 11 |

### Cloud SQL Database
> **⚠️ DECOMMISSIONED** — `amphive-db-in` (Cloud SQL) has been deleted. PostgreSQL now runs as a
> Docker container (`postgres:15-alpine`) directly on the GCP VM. Data persists
> via the `postgres_data` named Docker volume on the VM's disk.

### Running Containers (Docker Compose on VM)
| Container | Port | Service |
|-----------|------|---------|
| `amphive-db` | internal | PostgreSQL 15 (no exposed port — internal to compose network) |
| `amphive-frontend` | `80` | React/Vite UI served via Nginx |
| `amphive-backend` | `8000` | FastAPI REST API |
| `amphive-mqtt` | `1883` | Eclipse Mosquitto MQTT Broker |

### Firewall Rule: `allow-amphive-ports`
Open ports: `TCP 80`, `TCP 8000`, `TCP 1883` from `0.0.0.0/0`.

### Firewall Rule: `allow-amphive-wireguard`
Open port: `UDP 51820` from `0.0.0.0/0` (WireGuard tunnel for direct plug control).

### WireGuard Tunnel (Direct Mode)
A WireGuard VPN tunnel runs between the VM (`10.10.0.1`) and the developer's
PC (`10.10.0.2`) to allow the cloud backend to control a Tapo P110 smart plug
on the home LAN without an ESP32 gateway. See [wireguard_tunnel_setup.md](file:///c:/Users/Sarthak/Documents/AmpHive/deploy/docs/wireguard_tunnel_setup.md).

### Deployment Method
The application is deployed via `deploy/scripts/deploy.ps1` which copies the code via `gcloud compute scp`, uploads the docker-compose file and `.env`, and builds using `docker-compose up -d --build` directly on the VM. No Cloud SQL wait step — deployment is fast (~1-2 minutes total).

### Start/Stop Infrastructure
- **Start**: Run `start-vm.bat` — starts VM only (single `gcloud compute instances start` command). All containers auto-start via `restart: always`.
- **Stop**: Run `stop-vm.bat` — stops VM only. All containers and Postgres stop gracefully. Data persists in the `postgres_data` Docker volume.

### Deployment History
For the full log of infrastructure commands (AWS→GCP migration, India region migration, old resource cleanup), see [gcp_migration_runbook.md](file:///c:/Users/Sarthak/Documents/AmpHive/deploy/docs/gcp_migration_runbook.md).

---

## 4. Features Map & Progress Log

For detailed technical specifications, integration paths, and step-by-step instructions for the remaining backlog features, refer to the **[features_list.md](file:///c:/Users/Sarthak/Documents/AmpHive/features_list.md)**.

### [x] Phase 1: Core Architecture & Communication (COMPLETED)
- **Multi-Tenant DB Schema:** Relational tables set up for tenants (CPOs), users, gateways, plugs, active sessions, and cash ledgers.
- **REST Endpoints:** Backend routes to register gateways, register plugs, and trigger start/stop session commands.
- **Bidirectional MQTT Client:** Server-side listener for status (`/status`) and telemetry (`/telemetry`) topics, and publisher for ON/OFF command actions.
- **Secure VPN Tunneling:** Embedded ESP32 Tailscale client (`microlink`) establishing peer connection with the self-hosted Headscale controller.
- **Local Smart Plug Driver:** ESP32 driver to switch ON/OFF and extract real-time power metrics from TP-Link Tapo P110 smart plugs over local network.
- **Edge Failsafe Watchdogs:** Microcontroller-level session watchdogs that auto-disconnect the plug if max session duration, energy capacity, or thermal limits (75°C) are breached.
- **Docker Compose Cloud Stack:** Distributed multi-container production stack fully deployed and verified on the GCP Compute Engine VM.

### [x] Phase 1.5: Direct Mode — ESP32 Bypass (COMPLETED)
- **WireGuard Tunnel:** Encrypted VPN tunnel from GCP VM to developer's home PC, allowing the cloud backend to reach the Tapo P110 plug on the home LAN directly.
- **Tapo Direct Driver:** Python service (`backend/services/tapo_direct.py`) using the `tapo` library to control the plug directly via HTTP, bypassing ESP32 and MQTT.
- **Direct Control Endpoints:** REST API endpoints under `/api/direct/` for ON/OFF control, device info, energy usage, and health checks.
- **WireGuard Setup Guide:** Complete documentation at `deploy/docs/wireguard_tunnel_setup.md` with pre-generated keys and configs.
- **Note:** This is a temporary development/testing feature. Will be replaced by the ESP32 gateway path once the board arrives.

### [~] Phase 2: Frontend & Driver Interfaces (MOSTLY DONE)
- **[x] Prepaid Driver Wallet Application:** React 19 + Vite SPA is built — login/register, Plug-ID start/stop, live SSE session monitor, wallet top-up, and private-group join via access code. (See [docs/ARCHITECTURE.md](file:///c:/Users/Sarthak/Documents/AmpHive/docs/ARCHITECTURE.md#frontend).)
- **[ ] CPO Administration Dashboard:** Not built. There is also **no role enforcement** in the backend yet (all users are `driver`), so CPO/admin workflows are unsupported.
- **[x] Visual Charging Analytics:** Inbound MQTT telemetry feeds the `TelemetryStore` and the database session and plug records. The live SSE session monitor displays real-time power, energy, duration, and calculated coin costs based on `COINS_PER_KWH`. (TimescaleDB itself is not used; in-memory store and session table updates are used). See [docs/IMPLEMENTATION_STATUS.md](file:///c:/Users/Sarthak/Documents/AmpHive/docs/IMPLEMENTATION_STATUS.md).

### [~] Phase 3: Financial & Third-Party Integrations (MOSTLY DONE)
- **[x] Razorpay Top-Ups:** `/api/payments/create-order` + `/api/payments/verify` (HMAC-verified) credit coins; frontend uses the Razorpay CDN checkout. 
- **[~] Razorpay Webhooks Handler:** `/api/payments/webhook` verifies the HMAC signature but **currently only logs** — it does not auto-credit `coin_balance`. (Top-ups are credited via the synchronous `/verify` path instead.)
- **[x] Virtual Ledger Audits:** Credits/debits are logged to `ledger_transactions`. ⚠️ Wallet updates are **not atomic/row-locked** (race-prone) — see [docs/SECURITY.md](file:///c:/Users/Sarthak/Documents/AmpHive/docs/SECURITY.md).

### [ ] Phase 4: Hardware & Optimization Scaling (REMAINING)
- **WiFi Onboarding Captive Portal:** ESP32 AP Captive Portal page allowing property staff to input local WiFi credentials, the Headscale Auth Key, and plug IPs without re-flashing code.
- **Dynamic Load Balancing (DLB):** Edge algorithm allowing the gateway to cycle or throttle smart plugs based on building total capacity constraints.
- **ESP32 Remote OTA Updates:** Upgrading the gateway firmware files over the air via the Headscale VPN connection.

---

## 5. Guidelines for Future AI Agents

When editing code or performing deployments in this repository:
1. **⛔ NEVER Deploy or Test on the Local Machine:** This Windows machine is a **development workstation only**. Do NOT run `docker compose up`, `deploy.ps1`, database migrations, or any deployment/testing commands locally. All deployment and testing must be performed on the **remote GCP VM** (`amphive-vm-in`) via `gcloud compute ssh` or the deploy script. If you need to verify something, SSH into the VM — never spin up containers or services on this machine.
2. **Docker Image Updates:** When updating the backend or frontend code:
   * Run `deploy/scripts/deploy.ps1` which handles uploading and rebuilding containers directly on the GCP VM.
4. **Headscale Config Changes:** Any changes to Headscale configurations must satisfy validation parameters for both the nested `noise.private_key_path` and `dns.nameservers` blocks.
5. **VLAN and Thread Safety on ESP32:** Keep the ESP32 stack tasks separated. The `microlink` VPN task requires a large stack size (32KB) and should be allocated in external PSRAM using the appropriate Spiram settings to avoid internal DRAM crashes.
6. **📝 Constantly Update Documentation:** After **every** meaningful action — code changes, deployments, new features, bug fixes, config changes, or commands run on the VM — you **must** update the relevant `.md` files in this repository. This includes but is not limited to:
   * **[agent.md](file:///c:/Users/Sarthak/Documents/AmpHive/agent.md):** Update the features progress log, file directory map, and deployment context whenever they change.
   * **[features_list.md](file:///c:/Users/Sarthak/Documents/AmpHive/features_list.md):** Mark features as complete, add new ones, or update implementation steps as work progresses.
   * **[README.md](file:///c:/Users/Sarthak/Documents/AmpHive/README.md):** Keep the directory structure, quick start instructions, and feature list current.
   * **[deploy/docs/](file:///c:/Users/Sarthak/Documents/AmpHive/deploy/docs/):** Log every deployment command, SSH session, Docker rebuild, or infrastructure change with the exact command run and its output. Future agents and Sarthak must be able to trace what happened and reproduce it.
   * **Walkthrough files:** Summarize what was done at the end of every session.
   * If no existing file is the right place, **create a new `.md` file** rather than leaving changes undocumented. Documentation is not optional — undocumented changes are unacceptable.
