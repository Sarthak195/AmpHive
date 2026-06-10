# GCP Migration & Deployment Runbook (June 10, 2026)

This document logs the complete sequence of infrastructure operations performed to migrate **AmpHive** from AWS EC2 to Google Cloud Platform, including the subsequent region migration from `us-central1` to `asia-south1` (Mumbai, India).

---

## 1. Phase 1: AWS → GCP Initial Migration (us-central1)

### 1.1. Architecture Change
- **Removed** the local PostgreSQL container from `docker-compose.yml`.
- **Added** `DATABASE_URL` as an environment variable loaded from `.env`, pointing to an external Cloud SQL instance.
- **Created** `.env.template` with placeholder values for production configuration.

### 1.2. Infrastructure Provisioning (us-central1)
The following resources were created in `us-central1`:

```bash
# Cloud SQL instance (PostgreSQL 15, micro tier)
gcloud sql instances create amphive-db \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region=us-central1 \
  --root-password="amphive_db_admin"

# Compute Engine VM (e2-micro)
gcloud compute instances create amphive-vm \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --image-family=debian-11 \
  --image-project=debian-cloud \
  --metadata-from-file=startup-script=startup.sh

# Create database inside Cloud SQL
gcloud sql databases create amphive --instance=amphive-db
```

### 1.3. Firewall Rules
Opened required ports for the application:
```bash
gcloud compute firewall-rules create allow-amphive-ports \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:80,tcp:8000,tcp:1883 \
  --source-ranges=0.0.0.0/0
```

**Ports opened:**
| Port | Protocol | Service |
|------|----------|---------|
| 80 | TCP | Frontend (Nginx serving React app) |
| 8000 | TCP | Backend (FastAPI) |
| 1883 | TCP | MQTT Broker (Mosquitto) |

### 1.4. Application Deployment
- Created `deploy.ps1` script to automate: wait for Cloud SQL → create DB → fetch IP → generate `.env` → SCP files → docker-compose up.
- Deployed `backend`, `frontend`, and `mqtt` containers via Docker Compose.
- Initial deployment IP: `136.112.1.67`

---

## 2. Phase 2: Region Migration to India (asia-south1)

### 2.1. Motivation
User requested migration to India for lower latency + VM upgrade to 4 vCPUs.

### 2.2. New Infrastructure Provisioning

```bash
# New Cloud SQL instance in Mumbai
gcloud sql instances create amphive-db-in \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region=asia-south1 \
  --root-password="amphive_db_admin"

# New Compute Engine VM in Mumbai (upgraded specs)
gcloud compute instances create amphive-vm-in \
  --zone=asia-south1-a \
  --machine-type=e2-highcpu-4 \
  --image-family=debian-11 \
  --image-project=debian-cloud \
  --boot-disk-size=50GB \
  --boot-disk-type=pd-balanced \
  --metadata-from-file=startup-script=startup.sh

# Create database inside new Cloud SQL instance
gcloud sql databases create amphive --instance=amphive-db-in
```

### 2.3. VM Specifications (Current Production)
| Property | Value |
|----------|-------|
| **Instance Name** | `amphive-vm-in` |
| **Zone** | `asia-south1-a` (Mumbai) |
| **Machine Type** | `e2-highcpu-4` (4 vCPU, 4GB RAM) |
| **Boot Disk** | 50GB Balanced Persistent Disk (pd-balanced) |
| **OS** | Debian 11 |
| **Public IP** | `35.200.131.98` |

### 2.4. Cloud SQL Specifications (Current Production)
| Property | Value |
|----------|-------|
| **Instance Name** | `amphive-db-in` |
| **Region** | `asia-south1` |
| **Engine** | PostgreSQL 15 |
| **Tier** | `db-f1-micro` |
| **Database Name** | `amphive` |
| **Root User** | `postgres` |

### 2.5. Application Deployment to India VM
```bash
# Fetched Cloud SQL IP and generated .env
gcloud sql instances describe amphive-db-in --format="value(ipAddresses[0].ipAddress)"

# Copied application code to VM
gcloud compute scp --quiet --recurse backend mosquitto.conf docker-compose.yml .env \
  amphive-vm-in:~/amphive/ --zone=asia-south1-a

# Built and started containers
gcloud compute ssh --quiet amphive-vm-in --zone=asia-south1-a \
  --command="cd ~/amphive && sudo docker-compose up -d --build"
```

### 2.6. Frontend Deployment
The React/Vite frontend was separately deployed with its Docker container (Nginx serving static build):
```bash
gcloud compute scp --quiet --recurse frontend amphive-vm-in:~/amphive/ --zone=asia-south1-a
gcloud compute ssh --quiet amphive-vm-in --zone=asia-south1-a \
  --command="cd ~/amphive && sudo docker-compose up -d --build"
```

---

## 3. Old Infrastructure Cleanup

The following US-region resources were permanently deleted after verifying the India deployment:

```bash
# Deleted old US VM
gcloud compute instances delete amphive-vm --zone=us-central1-a --quiet

# Deleted old US Cloud SQL instance
gcloud sql instances delete amphive-db --quiet
```

> [!WARNING]
> The old `amphive-vm` and `amphive-db` in `us-central1` are permanently deleted. All data from the US deployment is gone.

---

## 4. Current Live Endpoints

| Service | URL |
|---------|-----|
| **Frontend (UI)** | http://35.200.131.98 |
| **Backend API** | http://35.200.131.98:8000 |
| **API Docs (Swagger)** | http://35.200.131.98:8000/docs |
| **MQTT Broker** | `35.200.131.98:1883` |

---

## 5. Active GCP Resources & Billing

| Resource | Name | Region | Tier/Type | Estimated Monthly Cost |
|----------|------|--------|-----------|----------------------|
| Compute Engine VM | `amphive-vm-in` | `asia-south1-a` | `e2-highcpu-4` (4vCPU, 4GB) | ~$75/month |
| Cloud SQL | `amphive-db-in` | `asia-south1` | `db-f1-micro` (PostgreSQL 15) | ~$8/month |
| Boot Disk | attached to VM | `asia-south1-a` | 50GB pd-balanced | ~$5/month |
| Firewall Rule | `allow-amphive-ports` | global | TCP 80,8000,1883 | Free |

---

## 6. Deployment Structure Restructuring (Same Day)

Later in the same session, the deployment files were reorganized from a flat root layout into a structured `deploy/` directory:

| Old Location | New Location |
|---|---|
| `deploy.ps1` (root) | `deploy/scripts/deploy.ps1` |
| `startup.sh` (root) | `deploy/scripts/startup.sh` |
| `mosquitto.conf` (root) | `deploy/config/mosquitto.conf` |
| `.env.template` (root) | `deploy/config/.env.template` |
| `docker-compose.yml` (root) | `deploy/docker/docker-compose.prod.yml` (for VM) |
| _(new)_ | `deploy/docker/docker-compose.dev.yml` (local dev with Postgres) |
| `deploy/deploy_guide.md` | `deploy/docs/deploy_guide.md` |
| `deploy/deployment_checklist.md` | `deploy/docs/deployment_checklist.md` |
| `deploy/ec2_deployment_runbook.md` | `deploy/docs/ec2_deployment_runbook.md` |

The root `docker-compose.yml` was converted into a local development convenience file that includes a local Postgres container and references configs from `deploy/config/`.
