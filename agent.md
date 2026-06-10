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

* `/backend/`: FastAPI server files.
  * `/backend/main.py`: REST routes and application startup/shutdown lifecycles.
  * `/backend/services/mqtt_manager.py`: Bidirectional MQTT publisher/subscriber client.
  * `/backend/database/schema.sql` & `models.py`: PostgreSQL relational tables and SQLAlchemy ORM models.
* `/deploy/`: All deployment infrastructure.
  * `/deploy/scripts/deploy.ps1`: Main GCP VM deployment script.
  * `/deploy/scripts/startup.sh`: One-time VM bootstrap (installs Docker).
  * `/deploy/docker/docker-compose.prod.yml`: Production Docker Compose for the VM.
  * `/deploy/docker/docker-compose.dev.yml`: Local development Docker Compose (includes local Postgres).
  * `/deploy/config/mosquitto.conf`: MQTT broker configuration.
  * `/deploy/config/.env.template`: Environment variable template.
  * `/deploy/docs/new_device_setup.md`: Setup instructions for configuring developer tools and code on a new workstation.
  * `/deploy/docs/deploy_guide.md`: Public cloud hosting, dynamic DNS, and VPN network setup manual.
  * `/deploy/docs/deployment_checklist.md`: Step-by-step physical deployment guide.
  * `/deploy/docs/gcp_migration_runbook.md`: Full log of AWS→GCP migration and India region migration commands.
  * `/deploy/k8s/`: Kubernetes manifests (namespace, postgres, mosquitto, headscale, backend).
* `/firmware/`: ESP32 gateway codebase.
  * `/firmware/main/main.c`: Main loop managing STA WiFi, VPN connection, MQTT subscriber, and telemetry watchdogs.
  * `/firmware/main/tapo_protocol.c`: Local network driver to control and poll TP-Link Tapo P110 smart plugs.
  * `/firmware/components/`: Submodules containing the Tailscale client (`microlink`) and WireGuard layers.

---

## 3. Deployment Context: Live GCP Infrastructure (asia-south1, India)

### Compute Engine VM
| Property | Value |
|----------|-------|
| **Instance Name** | `amphive-vm-in` |
| **Zone** | `asia-south1-a` (Mumbai) |
| **Machine Type** | `e2-highcpu-4` (4 vCPU, 4GB RAM) |
| **Boot Disk** | 50GB Balanced Persistent Disk |
| **Public IP** | `35.200.131.98` |
| **OS** | Debian 11 |

### Cloud SQL Database
| Property | Value |
|----------|-------|
| **Instance Name** | `amphive-db-in` |
| **Region** | `asia-south1` |
| **Engine** | PostgreSQL 15 |
| **Tier** | `db-f1-micro` |
| **Database** | `amphive` |
| **User** | `postgres` |

### Running Containers (Docker Compose on VM)
| Container | Port | Service |
|-----------|------|---------|
| `amphive-frontend` | `80` | React/Vite UI served via Nginx |
| `amphive-backend` | `8000` | FastAPI REST API |
| `amphive-mqtt` | `1883` | Eclipse Mosquitto MQTT Broker |

### Firewall Rule: `allow-amphive-ports`
Open ports: `TCP 80`, `TCP 8000`, `TCP 1883` from `0.0.0.0/0`.

### Deployment Method
The application is deployed via `deploy/scripts/deploy.ps1` which copies the code via `gcloud compute scp` and builds using `docker-compose up -d --build` directly on the VM.

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

### [ ] Phase 2: Frontend & Driver Interfaces (REMAINING)
- **Prepaid Driver Wallet Application:** Web or mobile interface allowing drivers to check wallet balance, top up credits via payment gateway, and scan QR codes on charging bays to start/stop sessions.
- **CPO Administration Dashboard:** Property management portal to register new gateways, pair smart plugs, define custom utility rates (per minute/kWh), and review carbon credits and revenue streams.
- **Visual Charging Analytics:** Real-time charging speed and historical energy telemetry graphs rendering inside the dashboard (leveraging TimescaleDB/Clickhouse).

### [ ] Phase 3: Financial & Third-Party Integrations (REMAINING)
- **Stripe / Adyen Top-Ups:** Secure credit card billing hookups.
- **Stripe Webhooks Handler:** Backend listener to credit the user's `coin_balance` automatically upon checkout success.
- **Virtual Ledger Audits:** Logging all database credits/debits to `ledger_transactions` with double-entry security validation.

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
