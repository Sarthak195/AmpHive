# AmpHive — Deployment

*Verified against `deploy/`, root scripts, and `tools/` on 2026-06-29.*

There are **two parallel deployment models** in the repo:
1. **Docker Compose on a GCP Compute Engine VM** — this is the **live/canonical**
   deployment.
2. **Kubernetes (K3s) manifests** under `deploy/k8s/` — present and internally
   consistent, but **not** what production runs. Treat as an alternative/future.

---

## 1. Live deployment — GCP VM + Docker Compose

| Resource | Value |
|----------|-------|
| Compute VM | `amphive-vm-in`, zone `asia-south1-a` (Mumbai), `e2-standard-2` (2 vCPU / 8GB RAM), Debian 11, 50 GB disk |
| Static IP | `8.231.81.12` (reserved as `amphive-static-ip` — **does not change on restart**) |
| Database | PostgreSQL 15 running as a **Docker container** (`amphive-db`) on the VM. Data persists in the `postgres_data` named Docker volume. |
| ~~Cloud SQL~~ | ~~`amphive-db-in`~~ — **Decommissioned and deleted** on 2026-06-29. |

The VM runs `docker-compose.prod.yml` (version `3.7`) from a flat `~/amphive/` directory
(`docker-compose.yml`, `mosquitto.conf`, `.env`, `backend/`, `frontend/`).

### Containers (prod compose)

| Container | Image/build | Port | Notes |
|-----------|-------------|------|---------|
| `amphive-db` | `postgres:15-alpine` | internal | Postgres on the VM itself. `POSTGRES_PASSWORD=amphive_db_admin` hardcoded for compose v3.7 compat. |
| `amphive-backend` | build `./backend` | 8000 | env via `${...}` from `.env`; depends on `db` + `mqtt`. |
| `amphive-frontend` | build `./frontend` | 80 | Nginx serves the SPA + proxies `/api/` → backend |
| `amphive-mqtt` | `eclipse-mosquitto:2.0` | 1883, 9001 | persistence volumes + healthcheck |

Restart policy `always`.

### Compose file differences

| | root `docker-compose.yml` | `deploy/docker/docker-compose.dev.yml` | `deploy/docker/docker-compose.prod.yml` |
|--|--|--|--|
| Local Postgres | ✅ `amphive-db-dev` | ✅ | ✅ `amphive-db` (same VM) |
| Secrets/Tapo env | ✅ via `${...}` | ❌ (DB+MQTT only) | ✅ via `${...}` |
| mqtt healthcheck | ❌ | ❌ | ✅ |
| db healthcheck | ❌ | ❌ | ✅ `pg_isready` |
| restart | `unless-stopped` | `unless-stopped` | `always` |
| `mosquitto.conf` mount path | `./deploy/config/...` | `../../mosquitto.conf` | `./mosquitto.conf` |

Root and dev are local stacks (include Postgres); prod targets the VM. The dev
file omits all secrets, so Direct Mode / Razorpay won't work there.

### Deploy script — `deploy/scripts/deploy.ps1`

1. Set `DATABASE_URL=postgresql+asyncpg://postgres:amphive_db_admin@db:5432/amphive` in
   local `.env` (the hostname `db` resolves within the Docker Compose network).
2. `tar` up `backend/` + `frontend/` (excluding node_modules/.venv/.git) and
   `gcloud compute scp` the tarball to `~/amphive/`.
3. SCP `mosquitto.conf` (via `/tmp/` + `sudo mv` to handle permissions),
   `docker-compose.prod.yml` (as `docker-compose.yml`), and `.env`.
4. SSH: extract and `sudo docker-compose up -d --build`.

**Total time:** ~1–2 minutes (no Cloud SQL polling wait).

### One-time VM bootstrap — `deploy/scripts/startup.sh`

`apt-get install docker.io docker-compose`, enable+start Docker. Nothing else
(no firewall, no WireGuard — those are manual / runbook steps).

### Helper `.bat` scripts (root)

| Script | Action |
|--------|--------|
| `start-vm.bat` | Start VM only (`gcloud compute instances start`). All 4 containers auto-start via `restart: always`. **No Cloud SQL, no IP rewrite needed.** |
| `stop-vm.bat` | Stop VM only (`gcloud compute instances stop`). All containers stop gracefully. DB data persists in Docker volume. |
| `start-remote-servers.bat` | SSH `docker-compose up -d` only (no rebuild). |
| `stop-remote-servers.bat` | SSH `docker-compose down`. |
| `restart-remote-servers.bat` | SSH `docker-compose restart`. |
| `logs-remote-backend.bat` | SSH `docker logs -f amphive-backend`. |

### Database Seeding

To populate the database with development/test data (sample tenants, CPOs, drivers, gateways, plugs, completed charging sessions, and ledger transaction logs), you can run the database seed script:

- **Run locally (requires dependencies and database running):**
  ```bash
  python backend/seed.py
  ```

- **Run in Docker Compose development environment:**
  ```bash
  docker exec -it amphive-backend-dev python seed.py
  ```

Once completed, the database will be populated with default test accounts (all using the password `password123`):
- **Admin**: `admin@amphive.com`
- **CPO 1 (VoltNetwork)**: `cpo@voltnetwork.com`
- **CPO 2 (GreenCharge)**: `cpo@greencharge.com`
- **Driver 1**: `driver1@gmail.com`
- **Driver 2**: `driver2@gmail.com`

---

## 2. MQTT broker config (`deploy/config/mosquitto.conf`)

```
listener 1883 0.0.0.0
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest stdout
```

Single listener on 1883, **anonymous, no TLS**. Compose/K8s also publish port
9001 (websockets) but the config declares **no 9001 listener**, so that port is
exposed but not served. Combined with the firewall opening 1883 to `0.0.0.0/0`,
the broker is effectively an open public endpoint — see [SECURITY.md](SECURITY.md).

## 3. Direct-Mode WireGuard tunnel

`deploy/config/amphive_tunnel.conf` is the Windows-PC client config:
`Address 10.10.0.2/24`, peer endpoint `<vm-ip>:51820`, `AllowedIPs 10.10.0.0/24`.
The VM is the WG server `10.10.0.1/24` on UDP/51820 (firewall rule
`allow-amphive-wireguard`). The backend reaches the home relay through this
tunnel. (This file **commits a WireGuard private key** — see [SECURITY.md](SECURITY.md).)

> Port note: `wireguard_tunnel_setup.md` documents the relay at `:80`, but the
> actual `tools/relay_server.py` and `phase2_walkthrough.md` use **`:8000`**.

## 4. `tools/` (Direct-Mode helpers, run on the home PC)

| Script | Purpose |
|--------|---------|
| `relay_server.py` | stdlib HTTP server on `0.0.0.0:8000`; routes `/health`, `/info`, `/on`, `/off`; uses the `tapo` lib to drive the real plug. This is what the backend's `TAPO_RELAY_URL` points at. |
| `local_tapo_test.py` | Connection self-test (info → on → 3 s → off). |
| `turn_on.py` / `turn_off.py` | Minimal manual on/off. |

⚠️ All four hard-code Tapo account credentials — see [SECURITY.md](SECURITY.md).

## 5. Kubernetes manifests (`deploy/k8s/`, not live)

Namespace `amphive`, all Deployments `replicas: 1`. Differs from prod reality:
- In-cluster **Postgres** (PVC) instead of Cloud SQL.
- Backend/frontend pull Docker Hub images `sarthak195/amphive-*:latest` instead
  of building from source — these may be stale/unpublished.
- A full **Headscale** Deployment + ConfigMap (`server_url:
  http://amphive.duckdns.org:8080`, embedded DERP region 999, MagicDNS
  `amphive.mesh`, prefix `100.64.0.0/10`).
- Backend Deployment omits `JWT_SECRET_KEY` / Razorpay / Tapo env, so
  auth/payments/direct-mode would be unconfigured on K8s.
- No resource limits/requests; STUN/DERP ports not exposed via a Service.

## 6. Submodules (`.gitmodules` → `context_repos/`)

Reference repos under the author's GitHub (read-only context, not build inputs):
`ChargeHub` (prototype), `headscale` (control-server fork), `ESP32-Tailscale-WoL`
(firmware reference, source of `wireguard_lwip`).

## 7. Runbooks (`deploy/docs/`)

| File | Covers |
|------|--------|
| `new_device_setup.md` | Setting up a new dev workstation |
| `deploy_guide.md` | Cloud hosting, DuckDNS, VPN networking |
| `deployment_checklist.md` | Step-by-step physical site deployment (contains stale "EC2" wording) |
| `gcp_migration_runbook.md` | Full AWS EC2 → GCP, then region migration log |
| `wireguard_tunnel_setup.md` | Direct-Mode WireGuard setup (pre-generated keys/configs) |
| `phase2_walkthrough.md` | Phase-2 work log |
</content>
