# AmpHive — Deployment

*Verified against `deploy/`, `scripts/`, and `tools/` on 2026-07-20.*

The deployment model is **Docker Compose on a GCP Compute Engine VM**.
(The K3s manifests under `deploy/k8s/` were **retired 2026-07-07** — see
[§5](#5-kubernetes-manifests-deployk8s-retired) and `deploy/k8s/README.md`;
they are unmaintained reference material, not an alternative.)

---

## 1. Live deployment — GCP VM + Docker Compose

| Resource | Value |
|----------|-------|
| Compute VM | `amphive-vm-in`, zone `asia-south1-a` (Mumbai), `e2-standard-2` (2 vCPU / 8GB RAM), Debian 11, 50 GB disk |
| Static IP | `8.231.81.12` (reserved as `amphive-static-ip` — **does not change on restart**) |
| Database | PostgreSQL 15 running as a **Docker container** (`amphive-db`) on the VM. Data persists in the `postgres_data` named Docker volume. |
| ~~Cloud SQL~~ | ~~`amphive-db-in`~~ — **Decommissioned and deleted** on 2026-06-29. |

The VM runs the **TLS compose** (`deploy/docker/docker-compose.tls.yml`, shipped
as `docker-compose.yml` — the default since 2026-07-11) from a flat `~/amphive/`
directory (`docker-compose.yml`, `Caddyfile`, `mosquitto.conf`,
`mosquitto_passwd`, `mosquitto_acl`, `mqtt-certs/`, `.env`, `backend/`,
`frontend/`). `docker-compose.prod.yml` is the plain-HTTP variant, kept in
lockstep as the `-NoTls` rollback target.

### Containers (TLS compose)

| Container | Image/build | Port | Notes |
|-----------|-------------|------|---------|
| `amphive-caddy` | `caddy:2-alpine` | **80, 443** | The only public web entrypoint. Terminates HTTPS with an auto-renewed Let's Encrypt cert for `CADDY_DOMAIN` (HTTP-01; cert persists in the `caddy_data` volume); HTTP and bare-IP requests redirect to the canonical https origin. |
| `amphive-frontend` | build `./frontend` | internal | Nginx serves the SPA + proxies `/api/` and `/socket.io/` → backend; reached only by Caddy as `frontend:80`. |
| `amphive-backend` | build `./backend` | 8000 | env via `${...}` from `.env`; depends on `db` + `mqtt`. |
| `amphive-db` | `postgres:15-alpine` | internal | Postgres on the VM itself. `POSTGRES_PASSWORD` interpolated from `.env` (value rotated 2026-07-06). |
| `amphive-mqtt` | `eclipse-mosquitto:2.0` | 1883 (overlay-bound), **8883 public TLS** | auth + topic ACLs + persistence volumes + authenticated healthcheck — see [§2](#2-mqtt-broker-config-deployconfigmosquittoconf) |

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

### Deploy script — `deploy/scripts/deploy.ps1` (default: TLS stack; `-NoTls` for plain HTTP)

1. Validate `.env` (refuses missing/weak `JWT_SECRET_KEY` / `POSTGRES_PASSWORD` /
   MQTT credentials) and set
   `DATABASE_URL=postgresql+asyncpg://postgres:<POSTGRES_PASSWORD>@db:5432/amphive`
   (the hostname `db` resolves within the Docker Compose network). Reads
   `CADDY_DOMAIN` (defaults to `amphive.app` if missing) and optional
   `ACME_EMAIL` for the TLS front door.
2. `tar` up `backend/` + `frontend/` (excluding node_modules/.venv/.git) and
   `gcloud compute scp` the tarball to `~/amphive/`.
3. SCP `mosquitto.conf` + the broker TLS certs (via `/tmp/` + `sudo mv` to
   handle permissions), `docker-compose.tls.yml` (as `docker-compose.yml`;
   `-NoTls` ships `docker-compose.prod.yml` instead), and `.env`; generate the
   mosquitto passwd + ACL files and the **Caddyfile** (from
   `CADDY_DOMAIN`/`ACME_EMAIL`) on the VM.
4. SSH: extract and `sudo docker-compose up -d --build`.

**Total time:** ~1–2 minutes (no Cloud SQL polling wait). First TLS deploy also
needs ~a minute for Caddy's initial Let's Encrypt issuance (requires tcp:80 +
tcp:443 open — firewall rules `allow-amphive-ports` / `allow-amphive-https` —
and `CADDY_DOMAIN` resolving to the VM's static IP). **DNS (as of 2026-07-20):**
`amphive.app` (driver) and `cpo.amphive.app` (CPO operator portal) are real,
statically-configured A records at the domain registrar pointing at the VM's
static IP; there's also an `mqtt.amphive.app` A record for the direct-MQTT
broker endpoint (§2). DuckDNS is **retired** — no dynamic-DNS updater cron
runs on the VM anymore, and `scripts/setup_duckdns.sh` is unused/retired
(kept only as reference; see [SECURITY.md](SECURITY.md)).

### One-time VM bootstrap — `deploy/scripts/startup.sh`

`apt-get install docker.io docker-compose`, enable+start Docker. Nothing else
(no firewall, no WireGuard — those are manual / runbook steps).

### Helper scripts (`scripts/`)

Windows helper scripts live in `scripts/` (moved there from the repo root on
2026-07-02). Each is self-contained and invokes `gcloud` directly, so they can be
run from anywhere.

| Script | Action |
|--------|--------|
| `scripts/start-vm.bat` | Start VM only (`gcloud compute instances start`). All 4 containers auto-start via `restart: always`. **No Cloud SQL, no IP rewrite needed.** |
| `scripts/stop-vm.bat` | Stop VM only (`gcloud compute instances stop`). All containers stop gracefully. DB data persists in Docker volume. |
| `scripts/start-remote-servers.bat` | SSH `docker-compose up -d` only (no rebuild). |
| `scripts/stop-remote-servers.bat` | SSH `docker-compose down`. |
| `scripts/restart-remote-servers.bat` | SSH `docker-compose restart`. |
| `scripts/logs-remote-backend.bat` | SSH `docker logs -f amphive-backend`. |
| `scripts/setup_duckdns.sh` | **Retired 2026-07-20** — DuckDNS dynamic-DNS updater, superseded by the real `amphive.app` domain (⚠ commits a live token — see [SECURITY.md](SECURITY.md)). |

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

Two listeners, both authenticated (`allow_anonymous false` + passwd file) and
topic-ACL'd (per-gateway accounts are scoped to `amphive/gateways/<id>/#`):

- **8883 (TLS, public)** — the primary **direct-MQTT** path: gateways/agents dial
  outbound `mqtts://mqtt.amphive.app:8883` (DNS name, un-pinned from the raw
  VM IP as of fw 2.3.0) and validate the broker cert against the embedded
  AmpHive CA (the broker cert carries an `mqtt.amphive.app` DNS SAN). Certs
  live in `~/amphive/mqtt-certs` (shipped by `deploy.ps1`).
- **1883 (plaintext)** — backend over the internal Docker network only; **not
  host-published** as of 2026-07-20 (no overlay/VPN network exists to bind it
  to either — see [SECURITY.md](SECURITY.md)).

The passwd and ACL files are generated on the VM by `deploy.ps1`; per-gateway
accounts are added with `deploy/scripts/add_gateway_user.ps1`. Full history and
verification in [SECURITY.md §3](SECURITY.md).

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

## 5. Kubernetes manifests (`deploy/k8s/`, RETIRED)

**Retired 2026-07-07 (TD#15)** — kept as unmaintained reference only;
`deploy/k8s/README.md` carries the banner. Known divergence from prod at
retirement time (namespace `amphive`, all Deployments `replicas: 1`):
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
| `web_tls_rollout.md` | Caddy HTTPS front door — rollout, verification, rollback |
| `db_backup_restore.md` | Nightly DB/config backups + disk snapshots — setup, restore, restore test |
| `new_device_setup.md` | Setting up a new dev workstation |
| `deploy_guide.md` | Cloud hosting, DuckDNS, VPN networking |
| `deployment_checklist.md` | Step-by-step physical site deployment |
| `gcp_migration_runbook.md` | Full AWS EC2 → GCP, then region migration log |
| `wireguard_tunnel_setup.md` | Direct-Mode WireGuard setup — **retired** (Path B is gone; kept as historical reference) |
| `phase2_walkthrough.md` | Phase-2 work log |
