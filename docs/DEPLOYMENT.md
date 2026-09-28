# AmpHive — Deployment

*Verified against `deploy/`, `scripts/`, and `tools/` on 2026-07-20.*

The deployment model is **Docker Compose on a GCP Compute Engine VM**.
(The K3s manifests under `deploy/k8s/` were **retired 2026-07-07** — see
[§5](#5-kubernetes-manifests-deployk8s-retired) and `deploy/k8s/README.md`;
they are unmaintained reference material, not an alternative.)

---

## 1. Live deployment — GCP free-tier VM + Docker Compose

**Since the 2026-07-27 consolidation** the whole stack runs on ONE always-free
e2-micro (see `deploy/docs/relay_consolidation_runbook.md` for how it got
there; the previous paid `amphive-vm-in` / static-IP setup is deleted).

| Resource | Value |
|----------|-------|
| Compute VM | `amphive-relay`, zone `us-west1-a`, `e2-micro` (2 shared vCPU / 1 GB RAM + 2 GB swapfile), always-free tier — hosting cost $0/mo |
| Public IP | `136.117.94.209` — **reserved/static** (`gcloud compute addresses list` shows `amphive-relay-ip`, IN_USE). This doc previously said "ephemeral"; that was wrong, and it matters now that an **MX record** points here — a changing IP would silently break mail, not just the web. Verified across a stop/start during the 2026-08-18 resize: the address held. A records: `@`, `cpo`, `mqtt`, `mail`. |
| Database | PostgreSQL 15 as a Docker container. Data persists in the `amphive-relay_pgdata` named volume; nightly pg_dump → GCS + a recovery disk snapshot exist (see runbooks). |

The VM runs `deploy/relay/docker-compose.relay.yml` from `~/amphive-relay/`
(`docker-compose.relay.yml`, `.env`, `Caddyfile`, `config/` with the mosquitto
config + broker certs + passwd/ACL, and `backend/` + `frontend/` source staged
by the deploy script). Services are **image-pinned** (`amphive_backend:latest`,
`amphive_frontend:latest`) with per-container `mem_limit`s sized for 1 GB RAM —
there are no `build:` keys; the deploy script rebuilds the images explicitly.
The older `deploy/docker/docker-compose.tls.yml` / `docker-compose.prod.yml`
files describe the retired two-VM-era stack and are kept for reference.

### Containers (relay compose)

Compose-default names (`amphive-relay-<service>-1`) except the fake plug
(`fakeplug`); mosquitto logs to **stdout only** (bounded by the compose
json-file limits — no file mirror on this stack). Restart policy
`unless-stopped`.

| Container | Image/build | Port | Notes |
|-----------|-------------|------|---------|
| caddy | `caddy:2-alpine` | **80, 443** | The only public web entrypoint. Terminates HTTPS with an auto-renewed Let's Encrypt cert for `CADDY_DOMAIN` (HTTP-01; cert persists in the `caddy_data` volume); HTTP and bare-IP requests redirect to the canonical https origin. |
| frontend | `amphive_frontend:latest` | internal | Nginx serves the SPA + proxies `/api/` and `/socket.io/` → backend; reached only by Caddy as `frontend:80`. |
| backend | `amphive_backend:latest` | 8000 | env via `${...}` from `.env`; depends on `db` + `mqtt`. |
| db | `postgres:15-alpine` | internal | Postgres on the VM itself. `POSTGRES_PASSWORD` interpolated from `.env` (value rotated 2026-07-06). |
| mqtt | `eclipse-mosquitto:2.0` | **8883 public TLS** (1883 internal-only) | auth + topic ACLs + persistence volumes + authenticated healthcheck — see [§2](#2-mqtt-broker-config-deployconfigmosquittoconf) |



### Compose file differences

| | root `docker-compose.yml` | `deploy/docker/docker-compose.dev.yml` | `deploy/docker/docker-compose.prod.yml` |
|--|--|--|--|
| Local Postgres | ✅ `amphive-db-dev` | ✅ | ✅ `amphive-db` (same VM) |
| Secrets env | ✅ via `${...}` | ❌ (DB+MQTT only) | ✅ via `${...}` |
| mqtt healthcheck | ❌ | ❌ | ✅ |
| db healthcheck | ❌ | ❌ | ✅ `pg_isready` |
| restart | `unless-stopped` | `unless-stopped` | `always` |
| `mosquitto.conf` mount path | `./deploy/config/...` | `../../mosquitto.conf` | `./mosquitto.conf` |

Root and dev are local stacks (include Postgres); prod targets the VM. The dev
file omits all secrets, so Direct Mode / Razorpay won't work there.

### Deploy script — `deploy/scripts/deploy.ps1` (rewritten 2026-08-02 for the relay)

Ships the **committed git HEAD** (not the working tree) and rebuilds images
on-box:

1. **Preflight**: hard-gates the gcloud default project (other projects' work
   can silently switch it); resolves the repo's newest migration id (what
   `alembic_version` must equal afterwards); refuses to run if the VM is
   missing its operator-managed `.env`/compose file.
2. `git archive HEAD backend frontend` → tarball → `gcloud compute scp`.
3. **Stage via swap** (extract to `.deploy_stage`, then `rm -rf backend
   frontend && mv`): a corrupt tarball fails before the live tree is touched,
   and local deletions/renames propagate (the 2026-07-13 stale-overlay lesson).
4. Build `amphive_backend:latest` then `amphive_frontend:latest` sequentially
   under `nohup` (survives SSH drops; 1 GB RAM can't take parallel builds) and
   poll `~/build.log` for the result.
5. `docker compose -f docker-compose.relay.yml up -d` — recreates only the
   containers whose image changed; migrations auto-apply at backend startup.
6. **Verify**: `/api/health`, `alembic_version` == the expected head,
   backend restart count 0, and public `https://amphive.app/api/health` 200.

**What it deliberately does NOT ship:** the VM's `.env` (live secrets — DB,
MQTT, Razorpay, SMTP, Google OAuth), the `Caddyfile`, and the mosquitto
config/passwd/ACL under `config/` — those are operator-managed on the VM
(first-time bootstrap: `deploy/relay/deploy-relay.sh`; per-gateway broker
accounts: `add_gateway_user.ps1`). To add/rotate `.env` keys, append on the VM
and `docker compose up -d backend`.

**Total time:** ~10–15 min, dominated by the on-box image builds (mostly
cache-hits when only a few files changed). **DNS:** `amphive.app` (driver),
`cpo.amphive.app` (CPO portal), `mqtt.amphive.app` (direct-MQTT broker) and
`mail.amphive.app` (self-hosted mail) are records at the registrar (name.com)
pointing at the relay VM. DuckDNS is retired. **Since 2026-09-28** Cloudflare's
nameservers (`harlee`/`ridge.ns.cloudflare.com`) answer for the zone (see
below) — the records are unchanged; the domain is still registered at
name.com, whose old DNS records were left in place as the rollback.

### Cloudflare front door (`amphive.app`, `cpo.amphive.app`) — added 2026-09-28

`amphive.app` and `cpo.amphive.app` sit behind Cloudflare's free HTTP proxy
(orange-cloud) to cut latency for Indian visitors — the origin is a
`us-west1` `e2-micro`, ~240 ms RTT to India, and Cloudflare's edge terminates
the TLS handshake close to the visitor instead. `mqtt.amphive.app` and
`mail.amphive.app` stay **DNS-only** (grey-cloud): MQTT on 8883 and SMTP/IMAP
cannot pass through Cloudflare's HTTP-only proxy, so those two records keep
resolving straight to the relay VM's IP.

**Caddy side (live):** the global `trusted_proxies` block now lists
Cloudflare's published edge ranges instead of the old compose-private-network
ranges, plus `client_ip_headers CF-Connecting-IP`, plus
`header_up X-Forwarded-For {client_ip}` on every `reverse_proxy` block — see
`deploy/config/Caddyfile.example` for the annotated reference and
`deploy/relay/deploy-relay.sh` for the starter-Caddyfile generator (both
updated 2026-09-28; the live Caddyfile on the VM was hand-edited to match and
verified with `caddy validate` + reload). Net effect: from a request that
really came through Cloudflare, Caddy trusts `CF-Connecting-IP` only from
those ranges and collapses the upstream X-Forwarded-For to that single
vouched address (a spoofed XFF from the visitor is discarded); from a direct
hit on the origin IP (bypassing Cloudflare — still possible, see below),
`CF-Connecting-IP` is ignored and XFF falls back to the real peer. Either way
Caddy still appends exactly one XFF hop and frontend-nginx appends the other,
so `backend/services/rate_limit.py`'s `TRUSTED_PROXY_HOPS=2` needed **no
change** — only its topology comments were updated to spell out the
Cloudflare hop.

**Cloudflare dashboard settings (free plan):**

| Setting | Value | Why |
|---|---|---|
| SSL/TLS mode | Full (strict) | Origin already has valid Let's Encrypt certs via Caddy. |
| Always Use HTTPS | OFF | Caddy already 308-redirects http→https itself; leaving CF's edge redirect off keeps Let's Encrypt HTTP-01 renewal on :80 flowing through CF to Caddy untouched — Full mode connects to origin with the visitor's scheme, and TLS-ALPN-01 can't work through the proxy. |
| Rocket Loader | OFF | Rewrites HTML / injects scripts — conflicts with the strict CSP. |
| Email Address Obfuscation | OFF | Same reason — rewrites HTML against the strict CSP. |
| Bot Fight Mode | OFF | Can JS-challenge API/Socket.io clients. |
| WebSockets | ON | Required for Socket.io. |
| Caching | left at CF defaults | `/assets/*` are `public, max-age=31536000, immutable`; `index.html` is `no-cache`; every `/api` response already carries `Cache-Control: no-store` via `security_headers_middleware` in `backend/main.py`, so CSV exports (e.g. `/api/cpo/invoices.csv`) are never edge-cached despite `.csv` being a CF default-cached extension. |

Cloudflare's IP ranges change rarely; refresh `trusted_proxies` from
<https://www.cloudflare.com/ips-v4> and `/ips-v6` if they ever do.

**Deliberately not done:** the GCP firewall was **not** locked to Cloudflare's
ranges on 80/443 — the origin IP stays publicly reachable directly, because
`mail.amphive.app` is grey-cloud and its Let's Encrypt certificate issuance
needs :80 reachable directly (not routed through Cloudflare). Caddy's trust
model handles a direct hit correctly either way (see above), so this is a
deliberate trade rather than an oversight. See
[SECURITY.md](SECURITY.md#6-operational-notes) for the client-IP trust model
write-up.

**Status 2026-09-28:** LIVE. Caddy config applied on the VM and the
nameservers cut over to Cloudflare the same day; end-to-end verification
(edge TLS, caching, Socket.io, real client IP at nginx, mqtt/mail direct) is
recorded in [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md). On a fresh
zone the Universal SSL edge certificate lags activation by a few minutes —
HTTPS handshakes fail (TLS alert 40) until it lands.

### Backend dependency lockfile — `backend/requirements.lock.txt`

`backend/Dockerfile` installs `backend/requirements.lock.txt`, a fully-pinned
`pip freeze` snapshot — not `backend/requirements.txt` directly — so every
`--build` above resolves the exact same package versions instead of
re-resolving `>=` ranges at build time. `requirements.txt` stays the
human-edited source (loose pins + rationale comments); the lockfile is
regenerated, never hand-edited:

1. Update `backend/requirements.txt` as usual.
2. `.venv/Scripts/python -m pip install -r backend/requirements.txt`
3. `.venv/Scripts/python -m pip freeze` and copy the result into
   `backend/requirements.lock.txt`, dropping anything that isn't part of
   `requirements.txt`'s dependency closure — dev/test tooling (already
   pinned/declared separately in `backend/requirements-dev.txt`) and any
   stray local-only packages (browser automation, linters, etc.) that the
   backend doesn't import at runtime.

No version upgrades happen as a side effect of this regeneration — it only
mirrors what's already resolved from `requirements.txt`.

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

## 3. Direct-Mode WireGuard tunnel (RETIRED — removed 2026-08-02)

The pre-ESP32 "Direct Mode" (backend drives the Tapo plug over a WireGuard
tunnel to the home PC) was removed in the 2026-08-02 legacy purge along with
`backend/services/tapo_direct.py`, `backend/routers/direct.py`, and
`tools/relay_server.py`. `deploy/config/amphive_tunnel.conf` remains only as a
historical reference (its keys were rotated dead long ago — see
[SECURITY.md](SECURITY.md)); the retired runbooks live in `deploy/docs/`.

## 4. `tools/` (bench/dev helpers, run on the dev box)

| Script | Purpose |
|--------|---------|
| `fake_plug.py` | Hardware-free ESP32-gateway simulator (v2) speaking the full current MQTT contract — retained multi-plug roster (`.../config`), ON/OFF/SET_INTERVAL/SET_LIMITS, `/logs` WARN+ forwarding, configurable ramp/jitter load per plug (runs as the `fakeplug` container on the VM). |
| `p110_sim/` | P110 **network-protocol** emulator (KLAP v2, real crypto) — emulates N plugs so a **real** ESP32 gateway can be multi-plug bench-tested without owning that many P110s. Different layer than `fake_plug.py` (which fakes the whole gateway over MQTT and never touches real firmware); see `tools/p110_sim/README.md`. |
| `local_tapo_test.py` | LAN Tapo connection self-test (info → on → 3 s → off). |
| `turn_on.py` / `turn_off.py` | Minimal manual on/off against a LAN plug. |
| `klap_probe.py` / `read_serial.py` | Tapo KLAP protocol probe / ESP32 serial monitor. |
| `flasher/` | Standalone packaged tool (not a bench script) — a onefile Windows EXE that detects a plugged-in gateway board and flashes a prebuilt firmware image, for non-technical users. See [`tools/flasher/README.md`](../tools/flasher/README.md) and [FIRMWARE.md](FIRMWARE.md). Own `requirements.txt`, deliberately not part of the backend's dependency set. |

Tapo credentials come from `TAPO_EMAIL`/`TAPO_PASSWORD` env vars — never
hardcoded.

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

## 6. Runbooks (`deploy/docs/`)

| File | Covers |
|------|--------|
| `web_tls_rollout.md` | Caddy HTTPS front door — rollout, verification, rollback |
| `db_backup_restore.md` | Nightly DB/config backups + disk snapshots — setup, restore, restore test |
| `new_device_setup.md` | Setting up a new dev workstation |
| `gcp_migration_runbook.md` | Full AWS EC2 → GCP, then region migration log |
| `wireguard_tunnel_setup.md` | Direct-Mode WireGuard setup — **retired** (Path B is gone; kept as historical reference) |
| `phase2_walkthrough.md` | Phase-2 work log |
