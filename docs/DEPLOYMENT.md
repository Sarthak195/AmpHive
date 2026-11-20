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

### Features wave 1 (2026-11-17, built on `release/2026-11-17-w1`, deployed 2026-11-19)

*Executed 2026-11-19 (PR #151, merge `63f41b7`), thin kit: backend 07:24Z, frontend 07:32Z, no migration. DB backup `amphive-20261119-072134.dump`.*

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

**Host health alerts (F5, installed 2026-11-19).** `deploy/scripts/check_host_health.sh`
mails `support@amphive.app` when available memory or free disk drops below a
floor or `/api/health` fails, with a cooldown (see
`deploy/docs/host_health_alerts.md`). Install as the normal user, not sudo:

1. `scp deploy/scripts/check_host_health.sh amphive-relay:/tmp/`, then
   `ssh amphive-relay 'sed -i "s/\r$//" /tmp/check_host_health.sh && install -m 755 /tmp/check_host_health.sh ~/amphive-relay/check_host_health.sh'`.
2. `crontab -e` and add `*/5 * * * * HEALTH_URL=http://127.0.0.1:8000/api/health /home/Sarthak/amphive-relay/check_host_health.sh`.
3. Verify: `MIN_MEM_MB=999999 STAMP_DIR=/tmp/hh-test ~/amphive-relay/check_host_health.sh`
   should send a mail and exit 1.

Optional overrides: `ALERT_TO`, `MIN_MEM_MB`, `MIN_DISK_FREE_PCT`,
`HEALTH_URL`, `COOLDOWN_MIN`, `STAMP_DIR`. The script curls
`http://localhost/api/health` without `-L`; if port 80 only redirects to
HTTPS on the VM, set `HEALTH_URL` in the crontab line to a local URL that
answers 200.

*Executed 2026-11-19:* installed with exactly the crontab line in step 2. `HEALTH_URL` points at the backend's host loopback port because port 80 answers 308 to HTTPS and `https://localhost` fails without SNI. Alerts go to `support@amphive.app`; a forced test alert was delivered.

### Features wave 2 (2026-11-17, built on `release/2026-11-17-w2`, deployed 2026-11-19)

*Executed 2026-11-19 (PR #152, merge `3e7d26c`, includes the two `cpo_viewer` masking rounds) by the manual migration path: backend 08:48Z (alembic `0043_email_ci_unique` to `0047_promo_codes` at boot, healthy in about 15 s, 0 tracebacks), frontend 08:52Z (thin kit). DB backup `amphive-20261119-084706.dump`. Prod now: backend and frontend `3e7d26c`, alembic `0047_promo_codes`. The deploy kit now defaults `EXPECTED_ALEMBIC` to `0047_promo_codes`. The demo viewer is deployed but not seeded.*

*Rollback:* backend `./rollback_backend.sh 3e7d26c` (tag `pre-3e7d26c`, the same image as `rollback-3e7d26c-m0047`: the `63f41b7` code plus the four 0044-0047 migration files, so it boots on 0047). The tags `live-63f41b7`, `pre-63f41b7` and every older `pre-*` image can NOT boot on 0047. If a `cpo_viewer` user exists, first `UPDATE users SET role='driver' WHERE role='cpo_viewer'` (the `63f41b7` code cannot load that enum value). Frontend: `./rollback_frontend.sh 3e7d26c` (the `63f41b7` dist).

*Ops gotchas:* an `ssh host 'bash -s' <<EOF` script stops at the first `docker compose exec -T` because exec reads the rest of the script from stdin; copy the script to the VM and run it with `ssh -n`, or give exec `</dev/null`. The backend container has no Docker healthcheck, so check `{{.State.Status}}` and `{{.RestartCount}}`, not `.State.Health`.

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
   sessions; run once on 2026-11-19, see "Features wave 3"). Also optional: the wave 1 F5 host cron above.
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

### Features wave 3 (2026-11-19, built on `release/2026-11-17-w3`, deployed 2026-11-19)

*Executed 2026-11-19 (PR #153, merge `0047a2b`) by the manual migration path: backend 10:46Z (alembic `0047_promo_codes` to `0050_charge_routines` at boot, healthy in about 12 s; `charge_routines` table and the 5 new columns present; a 150 s soak covering the 60 s routine reaper showed 0 tracebacks, errors or warnings), frontend 10:53Z (thin kit). DB backup before it: `amphive-20261119-104547.dump`. Production now: backend and frontend both `0047a2b`, alembic `0050_charge_routines`.*

*Rollback:* backend `./rollback_backend.sh 0047a2b` (tag `pre-0047a2b`, the same image as `rollback-0047a2b-m0050`: the `3e7d26c` code plus the three 0048-0050 migration files, so it boots on 0050). `live-3e7d26c`, `pre-3e7d26c` and every older `pre-*` image can NOT boot on 0050. Frontend `./rollback_frontend.sh 0047a2b` (`3e7d26c` dist). The deploy kit (outside the repo) now defaults `EXPECTED_ALEMBIC` to `0050_charge_routines`. Logged-in read-only smoke passed in production: notification prefs, routines, an estimate on plug 1, and the commissioning checklist (all six checks pass on the real gateway). The demo viewer was seeded afterwards (one `cpo_viewer` on tenant 1, via `python -m backend.seed_cpo_viewer` in the backend container).

*Plan as written before the deploy:*

Production runs `3e7d26c` (backend and frontend, waves 1 and 2, deployed
2026-11-19) at alembic `0047_promo_codes`. `release/2026-11-17-w3` contains
waves 1 and 2 as well (branched from `release/2026-11-17-w2` at `590293e`,
then merged with main `6c1234f`), so the code delta against prod is wave 3
only. It is a **migration deploy**: the backend boot runs `alembic upgrade
head` through `0048_notification_prefs` (`users.notification_prefs` JSONB),
`0049_plug_hours` (four nullable `plugs` columns) and `0050_charge_routines`
(one new table plus `ix_charge_routines_user_id`) in one transaction. All
three are additive, reversible and idempotent (IF NOT EXISTS), with no
backfill, no GRANT and no index on a hot table.

0. **Path.** Same manual migration path as wave 2 (the kit's thin-layer script
   refuses `backend/migrations/versions/*`). In order:
   - DB backup.
   - Tag the running image `amphive_backend:live-3e7d26c`.
   - Build the rollback image FROM `live-3e7d26c` plus ONLY the three new
     migration files (`0048_notification_prefs`, `0049_plug_hours`,
     `0050_charge_routines`) as `amphive_backend:rollback-<new sha>-m0050`,
     and also tag it `amphive_backend:pre-<new sha>` (the kit's
     `rollback_backend.sh` only accepts `pre-<hex>` tags).
   - Build `amphive_backend:thin-<new sha>` FROM `live-3e7d26c` plus the added
     and modified backend files of the release (git diff of `backend/`
     against `3e7d26c`; at the time of writing 16 added including the three
     migrations, 21 modified; recount at deploy time).
   - Proofs on both images: alembic head reports `['0050_charge_routines']`;
     file counts (rollback = live + 3, thin = live + the added-file count);
     the container user is uid 10001.
   - Switch with `docker compose up -d --no-deps backend` and an nginx reload
     (`init_db` runs the upgrade at boot).
   - Verify: `alembic_version` == `0050_charge_routines`, the new column,
     columns and table exist, `{{.State.Status}}` is `running` with
     `{{.RestartCount}}` 0 (the backend has no Docker healthcheck, so do not
     read `.State.Health`), and 0 tracebacks in the backend log.
   - Then ship the frontend with the thin kit, `BASE_REF` set to the full
     `3e7d26c` sha.
   - Afterwards bump the kit's `EXPECTED_ALEMBIC` default from
     `0047_promo_codes` to `0050_charge_routines`.
   `deploy.ps1` also works but rebuilds the frontend with npm on the 1 GB VM.
1. **Rollback.** Use only `rollback-<new sha>-m0050` (also tagged
   `pre-<new sha>`, so `rollback_backend.sh <new sha>` lands on it). NEVER use
   `live-3e7d26c`, `pre-3e7d26c` or any older `pre-*` tag as a rollback after
   the switch: those images cannot boot on a database at `0050`. An `alembic
   downgrade` to `0047` drops the new column, the four plug columns and the
   `charge_routines` table (data in them is lost); the wave-2
   `0045_cpo_viewer_role` caveat in "Features wave 2" does not apply when
   rolling back only to `0047`.
2. **Backend and frontend ship together** (C10 checklist in the CPO gateway
   drawer, C3 estimate line, C7 Push preferences panel, C4 hours editor and
   driver badges, C5 Charge routines card). An old frontend with the new
   backend is harmless but hides the new features.
3. **Env vars: none new.** `services/routines.py` also reads the existing
   `MAX_QUEUED_CHARGES_PER_USER` (default 2). C7 push gating only matters
   when `VAPID_PRIVATE_KEY` is set, as before. Quiet hours use the IANA tz
   database already used by pricing (the prod image resolves Asia/Kolkata).
4. **No new service, port, dependency, nginx, Caddy, compose or Dockerfile
   change, no firmware change.** New routes live under `/api/me/*`,
   `/api/plugs/*` and `/api/cpo/*`, already proxied. The reaper gains two
   cheap passes per 60 s tick (one indexed scan of enabled routines, one scan
   of routines with a pending row). The C10 selftest is a pure read and adds
   no MQTT traffic.
5. **No manual VM step is required.** To use C5 on the real plug, queued or
   scheduled charging must be enabled for it (tenant or plug override), as for
   one-off scheduled starts; otherwise routines stay inert and the driver gets
   one "paused" notice.

### Wave 4 R2 (2026-11-19, built on `release/2026-11-19-r2`, deployed 2026-11-20)

*Executed 2026-11-20 (PR #156 wave 4 R1, merge `254a542`; PR #158 wave 4 R2, merge `7fd62bb`; PR #157 fw 2.10.0-direct live Wi-Fi signal, merge `d921ccd`; main == `d921ccd0bccaf4d5c2ffe9ea76ab8c2e753a7c33`) by the manual migration path, the same as wave 3. Backend 17:06:10Z (alembic `0050_charge_routines` to `0056_ocpp_charge_points` at boot, healthy in about 18 s). A model-vs-DB check found all 57 model columns that were missing before now present (tables `group_invites`, `ocpp_charge_points`, `ocpp_id_tags` and `ocpp_transactions` created; enum labels `tx_type.idle_fee`, `tx_type.reservation_no_show_fee`, `reservation_status.pending` and `reservation_status.declined` present). A 150 s soak showed 0 tracebacks, errors or warnings and 0 restarts, and `OCPP_CSMS_ENABLED` is unset in the container (OCPP off). Image `thin-d921ccd` (271 backend files = live 249 + 22 added). DB backup right before: `amphive-20261120-170536.dump`. Ops note: the switch script now gates on a pre-switch model-vs-DB column diff with the expected count pinned (57); the first run refused because the pinned count was mistyped as 56, and production was untouched (the gate failed closed). Frontend 17:11Z with the thin kit (dist-only, 231 files); the served `index.html` is identical to the build. Production now: backend and frontend both `d921ccd`, alembic `0056_ocpp_charge_points`. Read-only smoke in production passed (all 200): driver notification prefs, routines, vehicle (defaults), tariff-preview on plug 1 (`idle_fee` null), estimate, ready-by plan and my reservations; CPO selftest, reservations and groups. Behaviour is unchanged until someone opts in (idle fee 0, instant reservations, no member price, OCPP off). Firmware `2.10.0-direct` was built from `d921ccd`, published and OTA'd to the field gateway `1cc3abb4fb54` the same day (17:15Z, release id 7; see [FIRMWARE.md](FIRMWARE.md) and [deploy/docs/ota_image_publishing.md](../deploy/docs/ota_image_publishing.md)). PR #159 (a test-only flaky-test fix) is open and was not part of the deploy.*

*Rollback:* backend `./rollback_backend.sh d921ccd` lands on tag `pre-d921ccd`, the same image as `rollback-d921ccd-m0056` (the `0047a2b` code plus only the six `0051` to `0056` migration files, so it boots on 0056). `live-0047a2b`, `pre-0047a2b`, `rollback-0047a2b-m0050` and every older `pre-*` tag can NOT boot on 0056. Roll back only before any idle fee is settled, any no-show fee is charged or any `pending` or `declined` reservation exists (older code cannot map the new enum labels). Frontend `./rollback_frontend.sh d921ccd` (the `0047a2b` dist). The deploy kit (outside the repo) now defaults `EXPECTED_ALEMBIC` to `0056_ocpp_charge_points`.

*Plan as written before the deploy:*

Before the deploy, production ran `0047a2b` (backend and frontend, waves 1 to 3) at alembic `0050_charge_routines`. `release/2026-11-19-r2` (at `4fd4a70` when this was written) is stacked on `release/2026-11-19-r1` (`8402c41`, PR #156, no migration), so the code delta against production is R1 plus R2. Features and cross-lane wiring: [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) section 0h. Re-read the final sha, the DB-gated CI result and the migration line before deploying; the numbers below are those of the build.

It is a **migration deploy**: the backend boot runs `alembic upgrade head` through six revisions in one transaction (`0051_durable_notice_state` five nullable timestamptz columns, `0052_idle_fee` three tariff columns, six session columns and a `tx_type` value, `0053_vehicle_profile` four `users` columns, `0054_member_price_invites` two columns, table `group_invites` and three indexes, `0055_reservation_approval` three enum values, five columns and one partial index, `0056_ocpp_charge_points` three tables). All are additive, reversible (the enum labels stay on a downgrade), idempotent (`IF NOT EXISTS`), with no backfill, no GRANT and no index on a hot table. Metadata-only ALTERs on PG 15 are fast on the e2-micro. The `ALTER TYPE ... ADD VALUE` statements (`idle_fee`, `pending`, `declined`, `reservation_no_show_fee`) share the one upgrade transaction, which PG 12+ allows because no later revision uses a new value.

0. **Path.** The same manual migration path as wave 3 (the deploy kit's thin-layer script refuses `backend/migrations/versions/*`). In order:
   - DB backup first (`pg_dump`, as before), and confirm CI is green on the release sha (the DB-gated tests and the migration-vs-model check run only in CI).
   - Tag the running image `amphive_backend:live-0047a2b`.
   - Build the rollback image FROM `live-0047a2b` plus ONLY the six new migration files (`0051` to `0056`) as `amphive_backend:rollback-<new sha>-m0056`, and also tag it `amphive_backend:pre-<new sha>` (the kit's `rollback_backend.sh` accepts only `pre-<hex>` tags).
   - Build `amphive_backend:thin-<new sha>` FROM `live-0047a2b` plus the added and modified backend files of the release (git diff of `backend/` against `0047a2b`; recount at deploy time, it includes R1).
   - Proofs on both images: alembic head reports `['0056_ocpp_charge_points']`; file counts (rollback = live + 6, thin = live + the added-file count); the container user is uid 10001.
   - Switch with `docker compose up -d --no-deps backend` and an nginx reload (`init_db` runs the upgrade at boot). Backend first: an old backend answers 422 to a notification-prefs PUT that carries the R1 toggle keys.
   - Verify: `alembic_version` is `0056_ocpp_charge_points`; the new columns and the four new tables (`group_invites`, `ocpp_charge_points`, `ocpp_id_tags`, `ocpp_transactions`) exist; `{{.State.Status}}` is `running` with `{{.RestartCount}}` 0 (the backend has no Docker healthcheck); 0 tracebacks in the backend log through a soak that covers at least two 60 s reaper ticks (the no-show sweep and the startup `rearm_offline_rechecks` are new).
   - Then ship the frontend with the thin kit, `BASE_REF` set to the full `0047a2b` sha.
   - Afterwards bump the kit's `EXPECTED_ALEMBIC` default from `0050_charge_routines` to `0056_ocpp_charge_points`.
   `deploy.ps1` also works but rebuilds the frontend with npm on the 1 GB VM.
1. **Rollback.** Use only `rollback-<new sha>-m0056` (also tagged `pre-<new sha>`, so `rollback_backend.sh <new sha>` lands on it): the live code plus the new migration files, so it boots on 0056. NEVER use `live-0047a2b`, `pre-0047a2b`, `rollback-0047a2b-m0050` or any older `pre-*` tag after the switch: those images do not carry `0051` to `0056` and cannot boot on a database at `0056`. The rolled-back code does not know the four new enum labels, so roll back before any idle fee has been settled, any no-show fee charged or any `pending` or `declined` reservation created; otherwise the old code fails mapping those rows. An `alembic downgrade` to `0050` drops the new columns and the four tables (their data is lost) and leaves the enum labels. Frontend: `./rollback_frontend.sh 0047a2b` (the previous dist).
2. **Backend and frontend ship together.** The frontend changes are the Account Vehicle and insights panel (`VehicleSettings`), the dashboard insights and receipt CO2 line, ChargeSetupModal (Ready by, idle terms), the weekly routine form, SessionMonitor, the CPO Pricing idle fields, CPO Chargers reservation settings, CPO Groups member price and invites, the Groups invite landing (with the Login and Google callback return path), CPO Reservations approve and decline, ReserveModal, and the Terms, Refunds and Charging Credit Terms copy (idle fee and no-show fee, including `/refunds` section 4 on how to contest a no-show fee; `frontend/src/utils/legal.js` `LAST_UPDATED` was not bumped), plus the "Idle fee (included)" line on the receipt and the Activity session detail. An old frontend with the new backend is harmless but hides the new features.
3. **New env vars: all optional, all default safely, none is in the compose files.** The prod compose passes an explicit variable list, so these run at their defaults unless a pass-through is added (not part of this release; compose is not touched). Unset or empty values fall back to the default (`or "x"` guard).

   | Var | Default | Lane | Meaning |
   |---|---|---|---|
   | `INSIGHTS_EMAIL_EVERY_N_TICKS` | `15` | F8V | Run the monthly insights email pass every N reaper ticks. Needs `SMTP_HOST` (already set in prod for receipts) and uses `FRONTEND_ORIGIN` for its links; a silent no-op when SMTP is unset. Not silenced by `BILLING_EMAILS` (a separate opt-in) |
   | `GROUP_INVITES_PER_DAY` | `20` | C9 | Invite emails per CPO user per 24 h |
   | `GROUP_INVITES_GLOBAL_PER_DAY` | `50` | C9 | Invite emails platform-wide per 24 h. Invites reuse the existing `SMTP_*` and `FRONTEND_ORIGIN`; not silenced by `BILLING_EMAILS` |
   | `RESERVATION_NO_SHOW_FEE_MAX_COINS` | `100` | C8 | Cap on a plug's no-show fee |
   | `RESERVATION_NO_SHOW_SWEEP_MAX_LAG_MIN` | `10` | C8 | The no-show sweep charges only windows whose grace lapsed this recently |
   | `OCPP_CSMS_ENABLED` and the `OCPP_*` tuning vars | off | C2 | See "OCPP stays off" below |

   The existing knobs keep their meaning: `OPERATOR_ALERTS`, `GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC`, `OPERATOR_ALERT_COOLDOWN_SEC`, `GATEWAY_OFFLINE_NOTIFY_DELAY_SEC` (now also the floor for a re-armed re-check), `CHARGE_FULL_IDLE_W`, `PLUG_POWER_STALE_SEC`, `RESERVATION_NO_SHOW_GRACE_MIN`, `SCHEDULE_START_GRACE_MIN`.
4. **OCPP stays off.** Leave `OCPP_CSMS_ENABLED` unset in `.env`: the migration only creates three empty tables, and with the flag off neither `routers/ocpp.py` nor `services/ocpp_adapter.py` is imported, so `/ocpp/*` does not exist. No Caddyfile, nginx or compose change is part of this release. Enabling it anywhere would need all of: set `OCPP_CSMS_ENABLED=1`; a reverse-proxy route that forwards WebSocket upgrades for `/ocpp/*` to the backend over TLS (Basic credentials travel on every handshake; Caddy, and the frontend nginx if it stays in the path); uvicorn `--ws-max-size` close to `OCPP_MAX_MESSAGE_BYTES`; a measured backend RSS with the flag on and a simulator connected before any use on the 1 GB e2-micro (see "Memory and disk on the 1 GB VM"); and a charger `MeterValueSampleInterval` under the 300 s stale-session reaper timeout. With the flag on, each connected charge point causes at most one keep-alive UPDATE per 30 s on `charging_sessions`, so it is not reaped by `SESSION_STALE_TIMEOUT_SEC`; `OCPP_MAX_CONNECTOR_KW` (default 50) bounds metered energy; the backing gateway of each charge point counts as an offline gateway on the status page and in admin health. See [OCPP_CSMS.md](OCPP_CSMS.md).
5. **Behaviour is unchanged until someone opts in.** Idle fee 0 on every tariff, every plug in `instant` reservation mode with a fee of 0, no member price, no invite, the monthly email off for every account, OCPP off. The DUR change (durable notice and offline state) is active at once and is the only behavioural change on first boot: an offline alert outstanding at the 0051 deploy gets no "back online" follow-up (the new columns start NULL).
6. **No new service, port, dependency, nginx, Caddy, compose or Dockerfile change, no firmware change, no manual VM step.** New routes live under `/api/me/*`, `/api/plugs/*`, `/api/sessions/*`, `/api/groups/*`, `/api/cpo/*` and `/api/ocpp/*` (the last only with the flag), already proxied. The reaper gains three cheap passes: the no-show sweep (one bounded indexed query plus at most 50 rows per 60 s tick), the monthly insights email pass (every 15th tick) and, once at startup, `operator_alerts.rearm_offline_rechecks()` (reads the small `gateways` table). The ready-by planner scans at most about 1440 minute-steps in a worker thread (about 75 ms measured on a worst-case tariff) and writes no telemetry frames.
7. **Read-only smoke after the switch.** `GET /api/plugs/{id}/tariff-preview` carries `idle_fee: null`; `GET /api/me/vehicle` returns the defaults; `GET /api/plugs/{id}/ready-by` answers a display-only plan; `OCPP_CSMS_ENABLED` is unset in the running backend container's environment (do not probe `/ocpp/*` over HTTP: the frontend's SPA fallback can answer it with the app shell); `/admin/health` and the status page show no new offline gateway. Do not enable an idle fee, an approval mode or a no-show fee on the real plug without the operator's say-so.

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
| `host_health_alerts.md` | Host cron alerter for memory, disk and `/api/health` (F5, 2026-11-17, installed 2026-11-19) |
| `new_device_setup.md` | Setting up a new dev workstation |
| `gcp_migration_runbook.md` | Full AWS EC2 → GCP, then region migration log |
| `wireguard_tunnel_setup.md` | Direct-Mode WireGuard setup — **retired** (Path B is gone; kept as historical reference) |
| `phase2_walkthrough.md` | Phase-2 work log |
