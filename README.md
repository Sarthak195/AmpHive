# AmpHive EV Charging Platform

AmpHive is an enterprise-grade Software-as-a-Service (SaaS) and Platform-as-a-Service (PaaS) solution that transforms budget, off-the-shelf smart plugs (e.g., TP-Link Tapo P110) into a secure, monetizable, shared EV charging network.

---

## 1. System Overview

The AmpHive platform consists of three core components:
1. **Central API Server:** A FastAPI application orchestrating user wallets, charging session states, database transactions, and client-gateway message routing.
2. **Overlay VPN Plane:** A self-hosted Headscale control server that configures secure, encrypted WireGuard overlay tunnels between the server and the gateways, bypassing local firewalls/CGNATs completely.
3. **ESP32 Edge Gateway:** A microcontroller gateway deployed at the charging site that connects to the private VPN, receives MQTT commands, and controls/polls local smart plugs over a dedicated physical VLAN.

---

## 2. Directory Structure

```
AmpHive/
├── backend/                  # FastAPI Backend Server Code
│   ├── database/             # PostgreSQL database schemas and SQLAlchemy models
│   ├── services/             # Background services (MQTT connection manager)
│   ├── Dockerfile            # Container build specification
│   └── requirements.txt      # Python backend dependencies
├── deploy/                   # Cloud Deployment Configs & Manifests
│   ├── k8s/                  # Kubernetes (K3s) manifests for cluster scaling
│   └── deploy_guide.md       # Detailed guide on Always-Free cloud hosting & VPN networking
├── firmware/                 # ESP32-S3 Gateway Firmware (ESP-IDF v5.x)
│   ├── components/           # External submodules (microlink Tailscale client)
│   ├── main/                 # C source files (main loop, Tapo drivers, CMake)
│   └── sdkconfig.defaults    # FreeRTOS and PSRAM configuration defaults
├── docker-compose.yml        # Multi-container local testing configuration
├── mosquitto.conf            # Mosquitto MQTT broker configuration
└── requirements.md           # Product Requirements Document (PRD) & Design Spec
```

---

## 3. Quick Start: Local Testing

To launch the backend API, Mosquitto MQTT broker, and PostgreSQL database locally on your development machine using Docker Compose:

1. **Start the containers:**
   ```bash
   docker compose up --build
   ```
2. **Access the API:**
   Once running, you can access the FastAPI Swagger interface to interact with endpoints at [http://localhost:8000/docs](http://localhost:8000/docs).
3. **MQTT Broker Port:**
   Exposed locally at `1883` for testing gateway telemetry publications.

---

## 4. Production Cloud Deployment

AmpHive is fully deployable to lightweight Kubernetes (K3s) clusters (ideal for Always-Free cloud VM tiers like Oracle Cloud or GCP).

1. Expose your cloud ports (`80/TCP`, `443/TCP`, `50443/TCP`, `51820/UDP`).
2. Log into your EC2/cloud instance.
3. Apply the Kubernetes resource suite:
   ```bash
   sudo kubectl apply -f deploy/k8s/
   ```

For detailed network routing, DNS configurations, and database persistence settings, see the [deploy_guide.md](file:///c:/Users/Sarthak/Documents/AmpHive/deploy/deploy_guide.md).

---

## 5. References & Specifications

* **Detailed Platform Specifications:** Read the [requirements.md](file:///c:/Users/Sarthak/Documents/AmpHive/requirements.md) for functional/non-functional requirements, data security frameworks, and CPO-level security designs.
* **Firmware Customizations:** View [main.c](file:///c:/Users/Sarthak/Documents/AmpHive/firmware/main/main.c) to inspect how the ESP32 manages WiFi, connects to the Headscale VPN task, receives broker commands, and runs local session watchdog fail-safes.
