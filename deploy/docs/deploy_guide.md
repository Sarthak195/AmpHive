# AmpHive Cloud Deployment & Infrastructure Guide

This guide details how to deploy the **AmpHive** PaaS EV charging platform to a distributed cloud environment at **zero cost ($0/month)**, without owning a domain name, and without needing physical on-premise server hardware.

---

## 1. Domain-Less Networking Architecture

Typically, hosting a public PaaS requires a registered domain name (like `amphive.com`) and SSL certificates. However, because AmpHive integrates a self-hosted **Headscale VPN overlay**, we can bypass this requirement entirely for both CPO gateways and administrative access.

```
                  ┌──────────────────────────────────────────────┐
                  │          AmpHive Private Mesh Network        │
                  │             (Subnet: 100.64.0.0/10)          │
                  └──────────────────────┬───────────────────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 ▼                                               ▼
     ┌──────────────────────┐                         ┌──────────────────────┐
     │  AmpHive Backend VM  │                         │    ESP32 Gateway     │
     │                      │                         │                      │
     │  VPN IP: 100.64.0.1  │◄══[Encrypted Tunnel]═══►│  VPN IP: 100.64.0.5  │
     │  (Hosts Backend API  │                         │  (Connects to        │
     │   & MQTT Broker)     │                         │   mqtt://100.64.0.1) │
     └──────────────────────┘                         └──────────────────────┘
```

### How Gateway-to-Server Communication Works Without a Domain:
1. **The Private Overlay IP:** Once your cloud server boots Headscale, the server is assigned a static virtual IP inside the Tailscale network (typically `100.64.0.1`).
2. **Fixed Gateway Config:** When compiling the ESP32 firmware, the `MQTT_BROKER_URL` is hardcoded to the server's private VPN IP: `mqtt://100.64.0.1:1883`.
3. **No Domain Needed:** The ESP32 connects to the server entirely inside the encrypted WireGuard tunnel. The public internet never sees your database, your API, or your MQTT traffic.

### Exposing the Admin Dashboard Without a Domain (3 Options):
* **Option A: VPN-Only Access (Most Secure):** You add your admin computer to the Headscale VPN network. You can then access the FastAPI docs and dashboards at `http://100.64.0.1:8000/docs` directly from your browser. No public port is exposed.
* **Option B: Cloudflare Tunnels (Free & Public):** You run Cloudflare's free tunnel daemon (`cloudflared`) on your server. It maps the local container port `8000` to a free public subdomain provided by Cloudflare (e.g., `amphive.trycloudflare.com`).
* **Option C: Free Dynamic DNS (DuckDNS):** Register a free subdomain at `duckdns.org` (e.g., `amphive.duckdns.org`). Run a simple cronjob on your server to update DuckDNS whenever the cloud provider changes your VM's public IP.

---

## 2. Distributed Cloud Hosting (Always-Free Tiers)

To run your platform 24/7 without hosting hardware at home, leverage these "Always Free" cloud hosting offers:

### 1. Oracle Cloud Infrastructure (OCI) - Recommended
* **What you get:** 
  * Up to 4 ARM Ampere virtual CPUs (A1 Compute) with **24 GB of RAM** and 200 GB of NVMe block storage.
  * 10 TB of free outbound network egress per month.
  * A static, public IPv4 address.
* **Why it's perfect:** 24 GB of RAM is enough to run a production-grade Kubernetes cluster (K3s), databases, Headscale, and brokers simultaneously at zero cost.

### 2. Google Cloud Platform (GCP)
* **What you get:** 
  * 1 `e2-micro` VM instance (2 vCPUs, 1 GB RAM).
  * 30 GB of standard boot disk storage.
  * 1 GB of free egress (very limited egress, making it better for low-traffic testing only).

---

## 3. Container Orchestration Options: Docker Compose vs. Kubernetes

### Option A: Lightweight Kubernetes (K3s) - Scalable & Distributed
If you want to use Kubernetes without the high billing of cloud-managed K8s (like GKE or EKS, which cost $70+/month for the control plane alone), use **K3s** (Rancher's lightweight Kubernetes distribution).
* You install K3s on your free Oracle Cloud VM with a single command:
  ```bash
  curl -sfL https://get.k3s.io | sh -
  ```
* This gives you a fully functional, production-ready Kubernetes environment using only ~500MB of RAM.
* We have provided the Kubernetes manifests in `deploy/k8s/` to deploy PostgreSQL, Mosquitto, Headscale, and FastAPI.

### Option B: Docker Compose - Simple & Resource-Efficient
For small-to-medium deployments, Docker Compose is far easier to maintain and has zero runtime resource overhead.
* Simply copy the `docker-compose.yml` to your cloud VM and run `docker compose up -d`.
* Since all containers run in a bridge network, they can easily talk to each other using their service names (`db`, `mqtt`).

---

## 4. Key Deployment Hurdles & How to Solve Them

When deploying stateful, distributed systems (like databases and VPNs), you will encounter the following pitfalls:

### 1. The Persistent Volume Trap
* **The Problem:** In Kubernetes, Pods are ephemeral. If a database Pod restarts, any data written to the database is lost.
* **The Solution:** We configure **PersistentVolumeClaims (PVC)** using the standard `local-path` storage provider in K3s. This mounts a directory from your host VM's disk into the database container, keeping your users' wallet coins safe across restarts.

### 2. Opening Ports on Cloud Firewalls
* **The Problem:** By default, cloud providers (OCI, GCP) block all incoming ports. Even if you configure Docker or Kubernetes correctly, your ESP32 won't connect because the network drops the packets.
* **The Solution:** You must log into your Cloud Console (e.g., OCI Console) and add **Ingress Security Rules** for:
  * **TCP Port 80 / 443** (HTTP/HTTPS for Headscale node registrations).
  * **UDP Port 51820** (WireGuard tunnel data traffic).
  * **TCP Port 1883** (If you want to expose MQTT publicly - *not needed if using the VPN overlay*).

### 3. Database Migration Management
* **The Problem:** When you update your backend code (e.g., adding a new table field), your database schema in the cloud must update automatically without dropping existing tables.
* **The Solution:** Integrate **Alembic** (SQLAlchemy's migration tool) into your backend container. Set the container's entrypoint to run `alembic upgrade head` before starting the Uvicorn FastAPI server.

### 4. The Public Wi-Fi & Mobile Network Trap
* **The Problem:** When deploying on a public coffee shop Wi-Fi or a mobile hotspot (CGNAT), the Headscale VPN will successfully bypass the firewall and connect to the cloud. However, public Wi-Fi networks usually enable **Client Isolation (AP Isolation)**, preventing the ESP32 from talking to the Tapo plug locally. Furthermore, exposing the smart plug on a public network is a security risk.
* **The Solution:** For mobile or public deployments, use a low-cost **4G LTE router** or a travel router acting as a Wi-Fi repeater. Have it broadcast a hidden, private SSID. Connect the ESP32 and Tapo plugs to this private network. This isolates your hardware from public users and ensures local communication succeeds, while the VPN seamlessly punches out through the LTE connection.
