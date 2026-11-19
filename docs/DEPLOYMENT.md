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
| Public IP | `8.235.4.66`, **reserved/static** (`gcloud compute addresses list` shows `amphive-relay-ip`, IN_USE). This doc previously said "ephemeral"; that was wrong, and it matters now that an **MX record** points here, a changing IP would silently break mail, not just the web. Verified across a stop/start during the 2026-08-18 resize: the address held. A records: `@`, `cpo`, `mqtt`, `mail`. |
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

Compose-default names (`amphive-relay-<service>-1`) except the optional fake
plug (`amphive-fake-plug`, its own compose project `fakeplug`, **kept stopped**
since 2026-10-20; start/stop in [TESTING.md](TESTING.md)). Every container's
stdout log is capped by the json-file driver: 3 files of 10 MB for db,
backend, frontend, caddy and the fake plug, 3 files of 5 MB for mqtt (the
`x-logging` / `x-logging-mqtt` anchors in the repo compose, 2026-10-20; the
live copy on the VM gets the same values by hand, since deploy.ps1 doesn't
ship the compose file). The same applies to `stop_grace_period: 60s` on the
`db` service (added 2026-11-15 so `docker stop` does not SIGKILL Postgres
mid-checkpoint): add that line to the live compose on the VM by hand and
recreate `db`. mosquitto logs to **stdout only** (no file mirror on
this stack). Restart policy `unless-stopped`.

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

**Rolling back across `0042_revoked_sessions` (per-device logout,
2026-11-15).** An image rollback keeps the table but pre-feature code ignores
it, which silently un-revokes every per-device logout whose token has not
expired. Before starting the old image, run once on the owner connection:

```sql
UPDATE users SET token_version = token_version + 1 WHERE id IN (SELECT DISTINCT user_id FROM revoked_sessions WHERE expires_at > now());
```

It converts each live per-device revocation into a global one for the
affected users (they are signed out on every device). The migration's
`downgrade()` runs the same statement before dropping the table. Details in
[SECURITY.md](SECURITY.md) §2.

**Total time:** ~10–15 min, dominated by the on-box image builds (mostly
cache-hits when only a few files changed). **DNS:** `amphive.app` (driver),
`cpo.amphive.app` (CPO portal), `mqtt.amphive.app` (direct-MQTT broker) and
`mail.amphive.app` (self-hosted mail) are records at the registrar (name.com)
pointing at the relay VM. DuckDNS is retired. **Since 2026-09-28** Cloudflare's
nameservers (`harlee`/`ridge.ns.cloudflare.com`) answer for the zone (see
below), the records are unchanged; the domain is still registered at
name.com, whose old DNS records were left in place as the rollback.

### Non-root containers (infra deploy runbook, written 2026-11-16, DEPLOYED 2026-11-16)

**Deployed 2026-11-16 at 15:12Z** on top of `ac00d46`, differently from step 4 below: no `deploy.ps1` (it
would rebuild the frontend with npm on the 1 GB VM) and no disk snapshot (hosting
stays free). Instead a thin layer on the VM: `FROM amphive_backend:pre-nonroot`
(the running batch image) plus the exact `RUN` block and `USER 10001:10001` from
`backend/Dockerfile`, built with `--network none`. The compose half and the
volume chown were done as written. Verified: `id` is uid 10001, CapEff 0,
NoNewPrivs 1, the firmware dir is writable, health 200, RestartCount 0, and the
gateway kept sending telemetry. Rollback: tag `amphive_backend:pre-nonroot` as
`:latest`, restore `docker-compose.relay.yml.pre-nonroot`, `up -d backend`, then
reload nginx in the frontend container.

**Rollback images and migrations (learned 2026-11-16).** `init_db` runs
`alembic upgrade head` at startup and does not catch a failure. An image built
before a migration therefore cannot boot on a database that already has it:
alembic cannot find the newer revision and the backend crash-loops. So a
`pre-<sha>` tag is NOT a usable rollback after a deploy that ran a migration,
and neither is the kit's `rollback_backend.sh`. Before such a deploy, build a
rollback image: the previous code plus only the new migration file(s), with a
tree proof (on 2026-11-16: `amphive_backend:rollback-ac00d46-m0043`). The
2026-11-15 rollback tags `pre-825f27e` / `pre-6a42265` had the same trap.

`backend/Dockerfile` now runs as the fixed uid/gid **10001** (`app`) and
`agent/Dockerfile` as 10001 (`amphive`); in the compose files the `backend`
service gets `cap_drop: [ALL]` + `security_opt: [no-new-privileges:true]`
(relay, `tls`, `prod`). This closes audit item P1 (security-audit-2026-08-04).
The 2026-08-04 attempt was reverted because `deploy.ps1` ships only source (it
cannot apply the compose half) and the live `firmware_images` volume is
root-owned. Both are handled below. **Not changed on purpose:** the frontend
(nginx master starts as root to bind and drop to `nginx`; its caps cannot be
validated without running it), `db`, `mqtt` and `caddy` (their stock images
manage their own users and capabilities).

What the app writes to disk: nothing outside `/app/data`. OTA uploads
(`POST /api/admin/firmware-releases/upload`) publish to the GCS bucket over the
VM's metadata-server identity and never touch the volume; the volume is only
read by the legacy `GET /api/firmware/images/{filename}` for old release rows.
Existing files are mode 644 in a 755 directory, so they stay readable as uid
10001 even before the chown below. The chown is still done so the directory
is writable by the app (the image creates `/app/data/firmware-images` owned by
10001, but an EXISTING named volume keeps its old root ownership: Docker only
copies image ownership into a brand-new empty volume).

**This is a full image rebuild, not a thin layer.** The Dockerfile itself
changed, so use the normal on-box build (`deploy/scripts/deploy.ps1`): the pip
layer is above the changed instructions and stays cached, only the last few
layers rebuild. Do NOT use the local-build "thin layer over the old image"
shortcut: it overlays source on the previous image and never re-runs the
Dockerfile, so the `USER` would not be applied. The frontend image is unchanged
(its build is a cache hit). Order does not matter between the image and the
compose half: the image alone is non-root and safe with the old compose; the
compose half only adds `cap_drop` / `no-new-privileges`.

Run from the dev box (`gcloud compute ssh amphive-relay --zone us-west1-a`
for the VM steps; confirm the gcloud default project first, see the
`deploy.ps1` preflight). Pick a quiet time: the backend restarts once (a few
seconds of 502s, the gateways reconnect on their own).

1. **Backup.** On the VM run `~/amphive-relay/backup_db.sh` as the normal user
   (never with sudo) and confirm it exits 0 and the object is in the backup
   bucket; also take a disk snapshot
   (`gcloud compute disks snapshot amphive-relay --zone us-west1-a --snapshot-names pre-nonroot-$(date +%Y%m%d)`).
   Copy the live compose file: `cp docker-compose.relay.yml docker-compose.relay.yml.pre-nonroot`.
2. **Rollback tags** (so the old image survives the rebuild overwriting
   `:latest`): `sudo docker tag amphive_backend:latest amphive_backend:pre-nonroot`
   and the same for `amphive_frontend`.
3. **Chown the firmware volume.** Find the name with
   `sudo docker volume ls | grep firmware` (compose-default:
   `amphive-relay_firmware_images`), then
   `sudo docker run --rm -v amphive-relay_firmware_images:/d alpine chown -R 10001:10001 /d`.
   Safe to do before the new image is live (the old root-run backend ignores
   ownership).
4. **Ship and build the image.** From the dev box on a clean, committed HEAD
   that contains this change: `pwsh deploy/scripts/deploy.ps1` (pwsh 7).
   It builds both images on-box, runs `compose up -d` and verifies health and
   the alembic head. The backend now starts as uid 10001.
5. **Apply the compose half by hand** (deploy.ps1 does not ship the compose
   file). In `~/amphive-relay/docker-compose.relay.yml` add to the `backend`
   service (same text as `deploy/relay/docker-compose.relay.yml`):
   ```yaml
       cap_drop:
         - ALL
       security_opt:
         - no-new-privileges:true
   ```
   then `sudo docker compose -f docker-compose.relay.yml up -d backend` (only
   the backend is recreated).
6. **Verify.**
   - `sudo docker exec <backend> id` prints `uid=10001(app) gid=10001(app)`
     (take the real container name from `docker ps`).
   - `sudo docker inspect -f '{{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}}' <backend>`
     prints `[ALL] [no-new-privileges:true]`.
   - Volume is writable by the app:
     `sudo docker exec <backend> sh -c 'touch /app/data/firmware-images/.w && rm /app/data/firmware-images/.w && echo writable'`.
   - Legacy reads still work: `sudo docker exec <backend> ls -ln /app/data/firmware-images`
     lists the files owned by 10001, and (if any release row still points at a
     self-hosted URL) `curl -sI https://amphive.app/api/firmware/images/<file>` is 200.
   - Upload/OTA path: on the next real firmware release, upload the `.bin`
     through the admin console and confirm the release registers with the
     `storage.googleapis.com/amphive-fw/...` URL (this exercises the GCS
     publish from the non-root container) and the CPO OTA picker lists it.
   - `curl -s http://localhost:8000/api/health`, public
     `https://amphive.app/api/health` 200, backend restart count 0,
     `docker logs <backend> --since 5m` has no `PermissionError`, and a gateway
     shows online with fresh telemetry (the MQTT path works under `cap_drop`).
7. **Rollback** (any failed check): `sudo docker tag amphive_backend:pre-nonroot amphive_backend:latest`,
   restore the compose copy
   (`cp docker-compose.relay.yml.pre-nonroot docker-compose.relay.yml`), then
   `sudo docker compose -f docker-compose.relay.yml up -d backend`. The chown is
   harmless to leave in place. No migration is involved, so no database
   rollback is needed.

The fake plug (`deploy/docker/docker-compose.fakeplug.yml`) reuses the backend
image, so it also runs as 10001 once restarted: its bind-mounted
`fake_plug.py` must be world-readable (mode 644). The agent image (not run on
the VM) needs an existing `/state` volume chowned once:
`docker run --rm -v <vol>:/state alpine chown -R 10001:10001 /state`.

### Cloudflare front door (`amphive.app`, `cpo.amphive.app`) (added 2026-09-28)

`amphive.app` and `cpo.amphive.app` sit behind Cloudflare's free HTTP proxy
(orange-cloud) to cut latency for Indian visitors, the origin is a
`us-west1` `e2-micro`, ~240 ms RTT to India, and Cloudflare's edge terminates
the TLS handshake close to the visitor instead. `mqtt.amphive.app` and
`mail.amphive.app` stay **DNS-only** (grey-cloud): MQTT on 8883 and SMTP/IMAP
cannot pass through Cloudflare's HTTP-only proxy, so those two records keep
resolving straight to the relay VM's IP.

**Caddy side (live):** the global `trusted_proxies` block now lists
Cloudflare's published edge ranges instead of the old compose-private-network
ranges, plus `client_ip_headers CF-Connecting-IP`, plus
`header_up X-Forwarded-For {client_ip}` on every `reverse_proxy` block, see
`deploy/config/Caddyfile.example` for the annotated reference and
`deploy/relay/deploy-relay.sh` for the starter-Caddyfile generator (both
updated 2026-09-28; the live Caddyfile on the VM was hand-edited to match and
verified with `caddy validate` + reload). Net effect: from a request that
really came through Cloudflare, Caddy trusts `CF-Connecting-IP` only from
those ranges and collapses the upstream X-Forwarded-For to that single
vouched address (a spoofed XFF from the visitor is discarded); from a direct
hit on the origin IP (bypassing Cloudflare, still possible, see below),
`CF-Connecting-IP` is ignored and XFF falls back to the real peer. Either way
Caddy still appends exactly one XFF hop and frontend-nginx appends the other,
so `backend/services/rate_limit.py`'s `TRUSTED_PROXY_HOPS=2` needed **no
change**, only its topology comments were updated to spell out the
Cloudflare hop.

**Cloudflare dashboard settings (free plan):**

| Setting | Value | Why |
|---|---|---|
| SSL/TLS mode | Full (strict) | Origin already has valid Let's Encrypt certs via Caddy. |
| Always Use HTTPS | OFF | Caddy already 308-redirects http→https itself; leaving CF's edge redirect off keeps Let's Encrypt HTTP-01 renewal on :80 flowing through CF to Caddy untouched, Full mode connects to origin with the visitor's scheme, and TLS-ALPN-01 can't work through the proxy. |
| Rocket Loader | OFF | Rewrites HTML / injects scripts, conflicts with the strict CSP. |
| Email Address Obfuscation | OFF | Same reason, rewrites HTML against the strict CSP. |
| Bot Fight Mode | OFF | Can JS-challenge API/Socket.io clients. |
| WebSockets | ON | Required for Socket.io. |
| Caching | left at CF defaults | `/assets/*` are `public, max-age=31536000, immutable`; `index.html` is `no-cache`; every `/api` response already carries `Cache-Control: no-store` via `security_headers_middleware` in `backend/main.py`, so CSV exports (e.g. `/api/cpo/invoices.csv`) are never edge-cached despite `.csv` being a CF default-cached extension. |

Cloudflare's IP ranges change rarely; refresh `trusted_proxies` from
<https://www.cloudflare.com/ips-v4> and `/ips-v6` if they ever do.

**Deliberately not done:** the GCP firewall was **not** locked to Cloudflare's
ranges on 80/443, the origin IP stays publicly reachable directly, because
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
zone the Universal SSL edge certificate lags activation by a few minutes:
HTTPS handshakes fail (TLS alert 40) until it lands.

### Service worker and instant open (added 2026-11-16)

On the driver host (`amphive.app`, HTTPS, production build only) every visitor
gets the service worker `frontend/public/sw.js`, registered by
`frontend/src/utils/swControl.js` with `updateViaCache: 'none'`. It keeps the
app shell (`index.html`) and the hashed `/assets/*` in Cache Storage, so a
repeat open paints at once; the app then shows the user's last saved
dashboard and session data from localStorage (`src/utils/snapshot.js`),
dimmed and labelled "last saved" until each refresh succeeds. The CPO host
never registers it, and a worker that finds itself on a `cpo.` host has no
fetch handler. `/api/`, `/socket.io/`, `/auth/*`, `/reset-password`,
`/verify-email`, `/login`, `/signup`, `/cpo`, `/admin`, legal pages, unknown
paths and root files are never answered from the cache.

**Update flow after a deploy.** No `sw.js` change is needed for an app
deploy. Each open serves the cached shell and revalidates it in the
background: the worker fetches the new `index.html` and its entry assets, and
swaps the shell only once all of them are cached. A phone therefore normally
runs the previous bundle for one open; this is not a guarantee (the browser
can stop the worker before the swap finishes), so it is bounded instead by
the 7-day rule (a shell older than 7 days is fetched network-first, with a
3 s timeout, before falling back to the cache) and by the CACHE bump below. If the
old bundle asks for a lazy chunk the deploy removed, the page drops the
cached shell and reloads once onto the current app. Old assets are pruned one
swap later, not in the same open. Rule this implies: **API changes must stay
backward compatible for one release** (already true for open tabs).
`frontend/nginx.conf` is unchanged by this feature, so a dist-only thin layer
still deploys it.

**Operator switches** (each one is a frontend redeploy; `sw.test.js` fails if
the duplicated constants disagree):

| Switch | Edit | Effect on everyone | Effect on push users |
|---|---|---|---|
| Force everyone onto the new shell (a breaking API change) | Bump `CACHE` in `sw.js` and `SHELL_CACHE_NAME` in `swControl.js` together (`amphive-shell-v2`) | Old shell cache dropped on the worker update; open pages reload once onto the new app | None |
| Level 1, soft off | `SHELL_CACHE = false` in `sw.js` | Caches dropped, no fetch handler, worker stays registered; app opens as it did before this feature | None, push keeps working |
| Level 2, kill | `KILL = true` in `sw.js` **and** `SW_KILL = true` in `swControl.js` | Caches dropped, no fetch handler, open pages reload once (sessionStorage-guarded, no loop), pages stop registering and unregister the worker wherever it carries no push subscription | They keep a registered, cache-less, fetch-less worker (the same as the push-only worker before this feature), so notifications still arrive. A hard unregister for push users is not provided. |

Both levels work even if the shell logic is broken: the browser's worker
update check fetches `/sw.js` outside the fetch handler, and
`updateViaCache: 'none'` keeps it off the HTTP cache. Kill-switch reach is
"next visit", not instant. To revert, flip the flags back and redeploy.

**Post-deploy check.** `curl -sI https://amphive.app/sw.js`: expect
`cf-cache-status` other than a stale `HIT` and an `ETag` that matches the
origin. Cloudflare's Browser Cache TTL rewrites the browser-facing
`Cache-Control` to `max-age=14400` (measured 2026-11-16), so do not expect
`no-cache` in that header; the worker update check ignores it anyway because
of `updateViaCache: 'none'` (set "Respect Existing Headers" in Cloudflare if
you want the header to read correctly). `index.html` must stay
`cf-cache-status: DYNAMIC`: never add a Cloudflare "Cache Everything" rule.

**Known limits.**
- iOS Safari wipes script-written storage (Cache Storage, localStorage)
  after 7 days without use, and keeps service workers alive only briefly;
  the page posts a `revalidate` message after load so the swap does not rely
  only on the navigation event. A wiped phone just opens the slow way once.
- Personal snapshots are uid-tagged, schema-versioned, expire after 7 days
  and are cleared on logout, logout everywhere and any 401 (a per-device
  logout from another device reaches this one as a 401). Cache Storage holds
  only public shell and asset bytes.
- Logout does not unsubscribe Web Push (pre-existing), so on a shared phone
  the previous user's push notifications keep arriving until they turn push
  off in Account or the subscription expires.

### Memory and disk on the 1 GB VM (measured 2026-11-16)

Warm, the API answers in 25 to 200 ms on the VM. The likely cause of the slow
phone opens is the app's pages (code and data, mostly file cache rather than
swap) being evicted and read back from the 30 GB `pd-standard` disk on the
next request. One phone open at 08:38Z took about 21 s from page to dashboard
data; in that minute and the next the VM did 2,500 to 4,300 disk reads per
minute (baseline 35 to 500, GCE `instance/disk/read_ops_count`) and spent 23%
of CPU time in iowait. Two periodic jobs churn the page cache:

- **Hourly telemetry prune** (`services/telemetry_persistence.py
  _prune_old`): the retention DELETE filtered on `recorded_at` only, which has
  no index, so every run was a full scan of `telemetry_readings` (288 MB, 1 M
  rows) to remove about 360 rows. Fixed by also filtering on `plug_id`, which
  lets Postgres use `idx_telemetry_plug_recorded`.
- **Google OS Config agent** (`google-osconfig-agent`): every 10 minutes it
  rebuilds the apt package cache (`/var/cache/apt/pkgcache.bin`, about 80 MB
  written each time). It only enforces the `goog-ops-agent-policy` install
  policy and VM Manager inventory, neither of which AmpHive needs. Host fix,
  not in the repo: `sudo systemctl disable --now google-osconfig-agent`
  (rollback `sudo systemctl enable --now google-osconfig-agent`).

Host tuning, also not in the repo: `vm.swappiness=10` in
`/etc/sysctl.d/99-amphive-swappiness.conf` is a precaution: it makes the kernel
drop file cache before swapping the app's heap (real swap traffic in the
measured windows was small, 0 to 14 MB per few minutes). Rollback:
delete the file and run `sudo sysctl -w vm.swappiness=60`. The Ops Agent's
`agent.googleapis.com/swap/io` metric does not match the kernel's swap
counters (it showed about 330 MB at a 10-minute spike while `pswpout` barely
moved; its spikes line up with the apt cache writes). Use `/proc/vmstat`
`pswpin`/`pswpout`/`pgpgin` and per-process `/proc/<pid>/io` instead. Status 2026-11-16: all three are live. The
prune fix shipped in `e7bbcae` (PR #146, deployed 10:26Z), and the
swappiness file and the disabled OS Config agent were applied on the VM at
10:22Z with the owner's approval. Check: `pg_stat_user_tables.seq_scan` for
`telemetry_readings` (918 after the last old-style prune) should stop rising
hourly, and `pkgcache.bin` should stop being rewritten every 10 minutes.

Housekeeping 2026-11-17 (owner approved): removed the 10 finished deploy
payload folders under `/tmp/amphive-*` (including `amphive-maint` from the
2026-10-20 logging install) and untagged 11 alias tags:
`amphive_backend:thin-<sha>` and `amphive_frontend:thin-<sha>` for 2dc5c64,
825f27e, 6a42265, e7bbcae and ac00d46, plus `amphive_backend:nonroot-ac00d46`.
Each was a second name for an image that keeps another tag, so the 19 image
IDs and the disk use (28% of 30 GB) are unchanged. All `pre-*`, `pre-nonroot`
and `rollback-ac00d46-m0043` tags are kept. To restore a tag:
`sudo docker tag <kept tag> <removed tag>`. Of the backend tags, only
`rollback-ac00d46-m0043` and `pre-nonroot` can boot on a database at alembic
0043 (see "Non-root containers" above); the older `pre-<sha>` tags cannot.

### Features wave 1 (2026-11-17, built on `release/2026-11-17-w1`, not deployed)

No migration, nginx, compose, Dockerfile or dependency change: the thin
backend layer then the thin frontend layer carry it. All new env vars are
optional with working defaults; see the env table in
[API_REFERENCE.md](API_REFERENCE.md) "Environment variables the backend reads":

- F8 insights estimates: `GRID_EMISSION_FACTOR_KG_PER_KWH=0.71`,
  `PETROL_CO2_KG_PER_L=2.31`, `PETROL_PRICE_PER_L=100`, `ICE_KM_PER_L=15`,
  `EV_KM_PER_KWH=6`. To tune them, add them to the backend env and restart
  the backend.
- F3 operator alerts: `OPERATOR_ALERTS` (default on, `off` disables),
  `OPERATOR_ALERT_EMAIL` (default unset, no mail; email needs the `SMTP_*`
  vars, already set in prod), `GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC` (10800),
  `OPERATOR_ALERT_COOLDOWN_SEC` (3600). Cooldowns, the "offline notice was
  sent" memory and the cooldown-expiry "still offline" re-check are in process
  memory; a backend restart loses them and can re-alert once. The alerts
  cannot fire while the backend itself is down or evicted, which is what the
  host cron below covers.
- F11 public status: `STATUS_RATE_LIMIT` (default `20/60`). Cloudflare does
  not cache JSON by default, so `max-age=60` only helps browsers; every
  request reaches the origin, where the 60 s in-process cache keeps it cheap.
  Optional: a free Cloudflare Cache Rule for `/api/status` (Eligible for
  cache, edge TTL 60 s).

**Host health alerts (F5, not installed yet).** `deploy/scripts/check_host_health.sh`
mails `support@amphive.app` when available memory or free disk drops below a
floor or `/api/health` fails, with a cooldown (see
`deploy/docs/host_health_alerts.md`). Install as the normal user, not sudo:

1. `scp deploy/scripts/check_host_health.sh amphive-relay:/tmp/`, then
   `ssh amphive-relay 'sed -i "s/\r$//" /tmp/check_host_health.sh && install -m 755 /tmp/check_host_health.sh ~/amphive-relay/check_host_health.sh'`.
2. `crontab -e` and add `*/5 * * * * ~/amphive-relay/check_host_health.sh`.
3. Verify: `MIN_MEM_MB=999999 STAMP_DIR=/tmp/hh-test ~/amphive-relay/check_host_health.sh`
   should send a mail and exit 1.

Optional overrides: `ALERT_TO`, `MIN_MEM_MB`, `MIN_DISK_FREE_PCT`,
`HEALTH_URL`, `COOLDOWN_MIN`, `STAMP_DIR`. The script curls
`http://localhost/api/health` without `-L`; if port 80 only redirects to
HTTPS on the VM, set `HEALTH_URL` in the crontab line to a local URL that
answers 200.

### Features wave 2 (2026-11-17, built on `release/2026-11-17-w2`, not deployed)

`release/2026-11-17-w2` contains wave 1 as well (branched from
`release/2026-11-17-w1`), so a deploy of it ships both waves. Unlike wave 1
this is a **migration deploy**: the backend boot runs `alembic upgrade head`
from `0043_email_ci_unique` through `0044_session_auto_stop_full`,
`0045_cpo_viewer_role`, `0046_api_tokens` and `0047_promo_codes` in one
transaction. All four are additive and idempotent (IF NOT EXISTS), with no
backfill and no new index on a hot table.

0. **Path.** The deploy kit's thin-layer script refuses
   `backend/migrations/versions/*`, so ship it on the migration path the
   2026-11-16 `0043` deploy used (IMPLEMENTATION_STATUS.md §0c): take a DB
   backup, tag the rollback image (step 1), build the backend layer with the
   merged source including `0044` to `0047`, `up -d backend` (`init_db` runs
   the upgrade at boot), verify `/api/health`, `alembic_version` ==
   `0047_promo_codes` and RestartCount 0, then ship the frontend as the usual
   dist-only thin layer. `deploy.ps1` also works but rebuilds the frontend with
   npm on the 1 GB VM.
1. **Rollback image first.** Same rule as "Rollback images and migrations"
   above: before deploying, build and tag a rollback image from the code now in
   prod plus only the four new migration files, named after the sha being
   deployed like the `rollback-ac00d46-m0043` precedent
   (`amphive_backend:rollback-<new sha>-m0047`), with a tree proof, and also
   tag it `amphive_backend:pre-<new sha>`: the kit's `rollback_backend.sh`
   only accepts `pre-<sha>` tags, so its standard `rollback_backend.sh <new
   sha>` then lands on an image that can boot. Any other `pre-<sha>` tag
   cannot boot on a database at 0047. `0045_cpo_viewer_role`
   downgrade is a no-op (an enum value cannot be dropped), so `alembic
   downgrade` is not a full rollback. If a `cpo_viewer` login exists, set its
   role back to `driver` (owner connection) before rolling back: older code
   does not know the enum value, and disabling the account leaves the value on
   the row (untested either way).
2. **Backend and frontend ship together** (F10 viewer UI, F1 opt-in toggle,
   F7 token panel, F12 promo card and wallet redeem). An old frontend with the
   new backend is harmless (the server still refuses viewer writes) but hides
   the new features.
3. **Env vars, all optional with code defaults**, read from the relay `.env`
   through `env_file` (a line in the VM's `.env`, no compose edit; empty
   values fall back to the defaults): `CHARGE_FULL_ACTIVE_W` (200),
   `CHARGE_FULL_IDLE_W` (30), `CHARGE_FULL_GRACE_MIN` (5),
   `CHARGE_FULL_MINUTES` (10), `API_TOKEN_RATE_LIMIT` (`60/60`),
   `PROMO_REDEEM_RATE_LIMIT` (`20/3600`), `PROMO_REDEEM_ACCOUNT_RATE_LIMIT`
   (`10/3600`).
4. **No new service, port, dependency, nginx, Caddy, compose or Dockerfile
   change.** New routes live under `/api/me/*`, `/api/wallet/*` and
   `/api/cpo/*`, already proxied. F1 state is one small object per plug.
5. **No manual VM step is required.** Optional, owner only, after the deploy:
   create the demo viewer with
   `docker exec -it <backend container> python -m backend.seed_cpo_viewer --tenant-id <owner tenant id> --email <demo address>`
   (prints the password once; re-running resets it and signs out old
   sessions; never run so far). Also optional: the wave 1 F5 host cron above.
6. **Using F12:** redeem from a DRIVER-role account; the issuing tenant needs
   unsettled earnings in its pool.
7. **OCPP bridge:** nothing to deploy on the VM. It runs on a laptop with
   `python -m tools.ocpp_bridge` from the repo root, needs
   `AMPHIVE_METER_TOKEN` (an F7 token, so F7 must be live first) and
   `OCPP_CSMS_URL`; optional `AMPHIVE_API_URL` (default
   `https://amphive.app`), `OCPP_CP_PREFIX`, `OCPP_POLL_SECONDS`,
   `OCPP_ID_TAG`, `OCPP_BASIC_USER`, `OCPP_BASIC_PASSWORD`,
   `OCPP_STATE_FILE`. It needs `websockets` (in the lock) and `httpx`
   (`backend/requirements-dev.txt`). See [OCPP_BRIDGE.md](OCPP_BRIDGE.md).
8. **Local demo of F1** (operator only, not run): `tools/fake_plug.py
   --run-only ... --drop-to-zero-after 600` (or `--taper-after 600`), then
   wait the 5 min grace plus `CHARGE_FULL_MINUTES` after the drop.

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
| `fake_plug.py` | Hardware-free ESP32-gateway simulator (v2) speaking the full current MQTT contract — retained multi-plug roster (`.../config`), ON/OFF/SET_INTERVAL/SET_LIMITS, `/logs` WARN+ forwarding, configurable ramp/jitter load per plug. Also deployable as the `amphive-fake-plug` container on the VM, which is **kept stopped** and started only for a test (see [TESTING.md](TESTING.md)). |
| `gateway_provision/` | USB alternative to the setup portal: `provision_nvs.py` builds a gateway's NVS settings (Wi-Fi, Tapo account, broker password, setup code) with ESP-IDF's tools, verifies them, optionally writes them with esptool, and never prints a secret. **Writing replaces the whole NVS partition and reboots the gateway, so never use it during a charging session.** See [`tools/gateway_provision/README.md`](../tools/gateway_provision/README.md) and `deploy/docs/gateway_provisioning.md`. |
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
| `host_health_alerts.md` | Host cron alerter for memory, disk and `/api/health` (F5, 2026-11-17, not installed yet) |
| `new_device_setup.md` | Setting up a new dev workstation |
| `gcp_migration_runbook.md` | Full AWS EC2 → GCP, then region migration log |
| `wireguard_tunnel_setup.md` | Direct-Mode WireGuard setup — **retired** (Path B is gone; kept as historical reference) |
| `phase2_walkthrough.md` | Phase-2 work log |
