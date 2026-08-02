# Relay consolidation runbook — full stack onto `amphive-relay` (zero data loss)

*Opened 2026-07-27. Goal: fold the whole AmpHive stack (Postgres, backend,
Mosquitto, Caddy/frontend) onto the free-tier GCP e2-micro `amphive-relay`
(`us-west1`, external IP `136.117.94.209`, 1 GB RAM, ~600 MB free today —
currently just a bare nginx TCP relay for MQTT, stood up during the 2026-07-20
home migration), for portfolio hosting, then decommission the paid
`amphive-vm-in` (`asia-south1-a`, `8.231.81.12`, 2 vCPU / 8 GB, holds the real
Postgres data) with **zero data loss**.*

**Read first:** [db_backup_restore.md](db_backup_restore.md) (backup/restore
commands cited below) and [gcp_migration_runbook.md](gcp_migration_runbook.md)
(prior-migration pattern this follows — provision new, restore data, verify,
delete old only after verification).

**Companion scripts** (authored alongside this runbook, referenced but not
duplicated here): `deploy/relay/deploy-relay.sh` (relay host prep: swap file +
Docker install) and `deploy/relay/docker-compose.relay.yml` (the relay-sized
compose stack). If either has drifted from what's described here, the script
is the source of truth for exact commands — update this doc to match, don't
silently follow stale steps. `deploy-relay.sh` also installs logrotate for the
durable `mosquitto.log` file on every run — see
[mosquitto_log_rotation.md](mosquitto_log_rotation.md).

## Ground rules

- **1 GB RAM is a hard ceiling.** This is a demo/low-traffic/portfolio
  deployment, not a scale target. Expect to run Postgres + backend + Mosquitto
  + Caddy/frontend concurrently in ~600 MB of headroom — no room for a second
  copy of anything (no blue/green on-box). If a step needs temporary double
  storage (e.g. holding both an old dump and a live DB), watch disk, not just
  RAM.
- **The relay's current nginx TCP-relay role is retired by this migration**,
  not layered under it: once the local Mosquitto container on `amphive-relay`
  is up and reachable on `:8883`, gateways connect straight to it and the
  nginx `stream {}` TCP-proxy config (whatever forwarded MQTT traffic
  elsewhere) is no longer needed. Confirm nothing else depends on that nginx
  process before removing it (step 2).
- **Every step below that touches `amphive-vm-in` (prod), DNS, or deletes
  anything is marked OPERATOR/PROD and requires explicit operator
  authorization before running.** Steps that only prepare the relay box or
  restore into it (nothing yet pointed at by DNS or by fielded gateways) are
  lower-risk but still confirm the target host (`amphive-relay`, not
  `amphive-vm-in`) before running any `gcloud compute ssh` / `scp`.
- Do not run the app stack or a database on this dev box (CLAUDE.md
  non-negotiable) — every command below targets the relay or prod VM via
  `gcloud compute ssh`/`scp`, never `localhost`.

---

## 0. PREREQ — fresh prod backup **[OPERATOR/PROD]**

Take a backup no older than the cutover window. This does not require full
authorization to *run* (it's the existing nightly job, invoked ad hoc,
non-destructive, read-only against prod) but confirm with the operator before
touching `amphive-vm-in` at all:

```sh
# On amphive-vm-in (via gcloud compute ssh amphive-vm-in --zone=asia-south1-a):
bash ~/amphive/backup_db.sh
```

This produces, per [db_backup_restore.md](db_backup_restore.md):
- `~/amphive/backups/amphive-<TS>.dump` — `pg_dump -Fc` logical dump of the
  `amphive` database.
- `~/amphive/backups/config-<TS>.tar.gz` — `.env`, `Caddyfile`,
  `mosquitto.conf`, `mosquitto_passwd`, `mosquitto_acl`, `mqtt-certs/`. **The
  per-gateway broker password hashes exist only in this tarball / on the VM**
  — losing them means re-provisioning every fielded gateway, so do not skip
  this even though it feels redundant with the nightly cron.

Pull both artifacts down to the dev box (or straight to the relay — either
works, but staging on the dev box first means one known-good copy survives
even if a transfer to the relay fails):

```powershell
gcloud compute scp --zone=asia-south1-a `
  amphive-vm-in:~/amphive/backups/amphive-<TS>.dump ./amphive-<TS>.dump
gcloud compute scp --zone=asia-south1-a `
  amphive-vm-in:~/amphive/backups/config-<TS>.tar.gz ./config-<TS>.tar.gz
```

Also grab the current MQTT CA + broker keys if they aren't already in the
config tarball's `mqtt-certs/` (they should be — verify):

```sh
tar -tzf config-<TS>.tar.gz | grep mqtt-certs
```

**Do not proceed past this point without a dump + config tarball you've
verified are non-empty and recent.**

---

## 1. Prep the relay host

Every command below uses `--zone=us-west1-<zone>` as a placeholder — confirm
the relay's actual zone once and substitute it everywhere:

```sh
gcloud compute instances list --filter="name=amphive-relay" --format="value(zone)"
```

Run the companion script from the dev box against `amphive-relay` (NOT
`amphive-vm-in` — double-check the target before running):

```powershell
gcloud compute ssh amphive-relay --zone=us-west1-<zone> --command="bash -s" < deploy/relay/deploy-relay.sh
# or, if the script is copied up first:
gcloud compute scp deploy/relay/deploy-relay.sh amphive-relay:~/deploy-relay.sh --zone=us-west1-<zone>
gcloud compute ssh amphive-relay --zone=us-west1-<zone> --command="bash ~/deploy-relay.sh"
```

Expected effect (see the script itself for the authoritative command list):
- A swap file sized to give Postgres + the JVM-free Node/Python stack enough
  headroom under 1 GB physical RAM (e2-micro has no swap by default — without
  it, the OOM killer will take out Postgres or the backend under any burst).
- Docker + Docker Compose installed (matching the version behavior
  `docker-compose.prod.yml`/`docker-compose.tls.yml` already assume —
  `version: '3.7'`, simple-array `depends_on`, no `${VAR:-default}`
  interpolation, same as the current prod VM's docker-compose v1).
- `~/amphive/` created as the remote deploy directory (mirrors
  `REMOTE_DIR` in `deploy/scripts/deploy.ps1` — keeps the restore commands
  below copy-pasteable from `db_backup_restore.md` without path edits).

**Before this step, note whatever currently runs the nginx TCP relay** (unit
name, compose file, or bare process) so it can be stopped cleanly in step 2
rather than left as an orphaned listener competing for the box's limited RAM.

Verify before continuing:

```sh
gcloud compute ssh amphive-relay --zone=us-west1-<zone> --command="free -h && docker --version && docker-compose --version"
```

---

## 2. Bring the stack up with a real `.env`

Build a production `.env` for the relay the same way `deploy.ps1` validates
one for `amphive-vm-in` — copy `deploy/config/.env.template`, fill every
secret with a **new, strong value** (do not reuse prod's secrets verbatim;
generate fresh `JWT_SECRET_KEY` / `POSTGRES_PASSWORD` / `MQTT_PASSWORD` per the
template's own instructions) *except* where continuity with the restored data
matters:

- `MQTT_USERNAME`/`MQTT_PASSWORD` (the backend's own broker account) can be
  freshly generated — only the **per-gateway** accounts in the restored
  `mosquitto_passwd` need to survive unchanged (that's why step 3 restores the
  config tarball wholesale rather than regenerating it).
- `CADDY_DOMAIN=amphive.app`, `CADDY_CPO_DOMAIN=cpo.amphive.app` (unchanged —
  these hostnames move with the DNS repoint in step 4, not with the VM).
- `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`/`RAZORPAY_WEBHOOK_SECRET` — reuse
  prod's live values if this relay is meant to keep taking real payments
  post-cutover, or blank/test values if the portfolio deployment is meant to
  be demo-only from here on. **Confirm intent with the operator** — this is a
  business decision, not a technical one.
- `DATABASE_URL` — leave as the template's `db:5432` form; `deploy-relay.sh`/
  the relay compose file is expected to run Postgres as a container exactly
  like `docker-compose.prod.yml` does today (named volume `postgres_data`,
  same as prod), so no external DB host to point at.

Copy `.env` up and stand up the stack once (accepting that no restore has
happened yet — this just proves images pull and containers start on 1 GB):

```powershell
gcloud compute scp .env amphive-relay:~/amphive/.env --zone=us-west1-<zone>
gcloud compute scp deploy/relay/docker-compose.relay.yml amphive-relay:~/amphive/docker-compose.yml --zone=us-west1-<zone>
gcloud compute ssh amphive-relay --zone=us-west1-<zone> --command="cd ~/amphive && sudo docker-compose up -d"
gcloud compute ssh amphive-relay --zone=us-west1-<zone> --command="cd ~/amphive && sudo docker-compose ps && free -h"
```

If this OOMs or a container restart-loops, that is the 1 GB ceiling talking —
check `docker stats` before adding anything else, and consider whether Caddy
+ frontend need to co-locate here at all vs. a lighter static-file server (a
sizing call for whoever runs this, not scripted here).

Do **not** treat this as done — the DB is empty and Mosquitto has no
gateway credentials yet. That's steps 3.

---

## 3. Restore the prod dump + config tarball into the relay stack

Copy the artifacts pulled in step 0 up to the relay:

```powershell
gcloud compute scp amphive-<TS>.dump amphive-relay:~/amphive/amphive-<TS>.dump --zone=us-west1-<zone>
gcloud compute scp config-<TS>.tar.gz amphive-relay:~/amphive/config-<TS>.tar.gz --zone=us-west1-<zone>
```

**Config tarball first** (mosquitto passwd/ACL/certs, so the broker that
comes up already knows every fielded gateway — restoring this before the DB
means a gateway reconnecting mid-migration still authenticates, even before
the historical data restore finishes):

```sh
# On amphive-relay, ~/amphive:
sudo tar -xzf config-<TS>.tar.gz -C /home/<user>/amphive mosquitto_passwd mosquitto_acl mqtt-certs
# Do NOT extract the prod .env/Caddyfile over the ones built in step 2 —
# extract only the broker-identity files, or extract to a scratch dir and
# copy selectively:
#   sudo tar -xzf config-<TS>.tar.gz -C /tmp/prod-config
#   sudo cp -r /tmp/prod-config/mosquitto_passwd /tmp/prod-config/mosquitto_acl /tmp/prod-config/mqtt-certs ~/amphive/
sudo chown -R 1883:1883 ~/amphive/mosquitto_passwd ~/amphive/mosquitto_acl ~/amphive/mqtt-certs
sudo chmod 600 ~/amphive/mosquitto_passwd ~/amphive/mosquitto_acl
sudo docker restart amphive-mqtt
```

**Logical DB restore** — per [db_backup_restore.md §Restore](db_backup_restore.md),
restore into a fresh database first, verify row counts, then swap:

```sh
# Into a fresh/empty DB (safe, non-destructive — do this first regardless):
sudo docker exec amphive-db createdb -U postgres amphive_restore_test
sudo docker exec -i amphive-db pg_restore -U postgres -d amphive_restore_test < ~/amphive/amphive-<TS>.dump
sudo docker exec amphive-db psql -U postgres -d amphive_restore_test -tAc \
  "SELECT count(*) FROM users; SELECT count(*) FROM charging_sessions; SELECT count(*) FROM gateways; SELECT count(*) FROM plugs; SELECT count(*) FROM ledger_transactions;"
# Compare against the same query run on amphive-vm-in's live DB (step 0's
# source) — expect an exact match except telemetry_readings, which may be a
# handful of rows behind if prod kept ingesting after the dump was taken.

# Once counts check out, restore OVER the live (empty, freshly-created) 'amphive' DB:
sudo docker stop amphive-backend
sudo docker exec -i amphive-db pg_restore -U postgres -d amphive --clean --if-exists < ~/amphive/amphive-<TS>.dump
sudo docker start amphive-backend
sudo docker exec amphive-db dropdb -U postgres amphive_restore_test
```

Confirm the backend comes up clean against the restored data:

```sh
sudo docker logs amphive-backend --tail 50
curl -s http://localhost:8000/api/config
```

At this point the relay stack has real prod data, real gateway credentials,
but **DNS still points at `amphive-vm-in`** — nothing public has moved yet.
This is the natural pause point to sanity-check before touching DNS.

---

## 4. Repoint DNS + reissue the MQTT server cert **[OPERATOR/PROD — DNS change, public-facing]**

This step redirects live traffic. Confirm with the operator before running
any of it — once the TTL expires, drivers/gateways/CPOs start hitting the
relay, whether or not it's ready.

**4a. Reissue the MQTT server cert** to add the relay's IP as a SAN, keeping
the existing CA (per [mqtt_dns_rollout.md](mqtt_dns_rollout.md) — **never
regenerate the CA**, it's embedded in every deployed gateway):

```bash
RESIGN_SERVER=1 \
  MQTT_TLS_SAN_IPS=100.87.241.70,8.231.81.12,136.117.94.209 \
  MQTT_TLS_SAN_DNS=mqtt.amphive.app \
  bash deploy/config/gen_mqtt_certs.sh
```

This keeps the legacy IP SANs (harmless once `amphive-vm-in` is gone — they
just stay unused) and adds `136.117.94.209` alongside the `mqtt.amphive.app`
DNS SAN that fw ≥ 2.3.0 actually validates against. Copy the reissued
`server.crt`/`server.key` up to the relay and restart Mosquitto there (same
ownership/perms as `deploy.ps1`'s cert step: `chown 1883:1883`, `server.key`
`600`):

```powershell
gcloud compute scp deploy/config/mqtt-certs/ca.crt deploy/config/mqtt-certs/server.crt deploy/config/mqtt-certs/server.key amphive-relay:/tmp/ --zone=us-west1-<zone>
```
```sh
sudo mkdir -p ~/amphive/mqtt-certs
sudo mv /tmp/ca.crt /tmp/server.crt /tmp/server.key ~/amphive/mqtt-certs/
sudo chown -R 1883:1883 ~/amphive/mqtt-certs
sudo chmod 644 ~/amphive/mqtt-certs/ca.crt ~/amphive/mqtt-certs/server.crt
sudo chmod 600 ~/amphive/mqtt-certs/server.key
sudo docker restart amphive-mqtt
```

**4b. Repoint the A records** at the domain registrar:

| Record | Old target | New target |
|---|---|---|
| `amphive.app` | `8.231.81.12` | `136.117.94.209` |
| `cpo.amphive.app` | `8.231.81.12` | `136.117.94.209` |
| `mqtt.amphive.app` | `8.231.81.12` | `136.117.94.209` |

Verify propagation before assuming any device or browser sees it:

```sh
nslookup amphive.app 1.1.1.1
nslookup cpo.amphive.app 1.1.1.1
nslookup mqtt.amphive.app 1.1.1.1
```

Caddy on the relay obtains its own Let's Encrypt certs for `amphive.app`/
`cpo.amphive.app` via HTTP-01 the same way it does on `amphive-vm-in` today
(`deploy/docs/web_tls_rollout.md`) — no manual cert step needed for the web
tier, just make sure `tcp:80`/`tcp:443` are open in the relay's firewall
rules before DNS switches over, or issuance will hang.

**Until DNS has propagated and been verified, keep `amphive-vm-in` running
and untouched** — it's still the live system for any client that hasn't
picked up the new A record yet.

---

## 5. Verify

Run all of these against the **new** hostnames (which now resolve to the
relay) before touching prod:

- **Web**: `curl -sI https://amphive.app` → 200 with a valid LE cert;
  `curl -sI https://cpo.amphive.app` → 200. Log in on both as a real user,
  confirm the SPA loads and `/api/config` responds.
- **MQTT**: watch a real (or the fake-plug simulator's) gateway reconnect to
  `mqtts://mqtt.amphive.app:8883` — check `docker logs amphive-mqtt` for a
  successful CONNECT from the gateway's existing per-device username, and the
  CPO gateways page for a fresh `last_seen_at`. No firmware change or
  reprovisioning should be required — this is exactly the DNS-only broker
  move `mqtt_dns_rollout.md` was designed to make invisible to the fleet.
- **One billed session**: start and complete one real charging session
  end-to-end against the relay (driver app start → telemetry flowing → stop →
  ledger entry recorded), confirming coins are debited and a session row
  lands in the restored+live Postgres. This is the load-bearing proof that
  DB + broker + backend are all correctly wired together on the new box, not
  just individually reachable.
- **Resource check**: `free -h` and `docker stats --no-stream` on the relay
  under this real traffic — confirm no OOM, no container restart-looping, and
  note actual headroom for the writeup (the whole point of the 1 GB
  constraint note below).

Do not proceed to step 6 until all of the above are green **and** the
operator has explicitly signed off that the relay is the accepted new
production system.

---

## 6. Decommission prod **[OPERATOR/PROD — destructive, irreversible]**

Only after step 5 is fully verified and explicitly authorized. Every command
here is destructive or spends money in reverse (i.e., not reversible without
recreating from scratch) — do not run any of it speculatively.

**6a. Final disk snapshot** (belt-and-suspenders on top of the logical dump
already restored — covers anything the logical dump wouldn't, e.g. if a
restore gap is discovered later):

```sh
gcloud compute disks snapshot amphive-vm-in \
  --zone=asia-south1-a \
  --snapshot-names=amphive-vm-in-final-$(date -u +%Y%m%d) \
  --description="Final snapshot before relay-consolidation decommission"
```

Confirm it completed and appears in `gcloud compute snapshots list` before
continuing.

**6b. Confirm no live traffic is still hitting prod** — check
`amphive-vm-in`'s nginx/Caddy access logs and Mosquitto connection logs for
recent activity; DNS TTL stragglers and cached resolver entries can keep a
trickle of traffic arriving after the A-record switch.

**6c. Delete the VM:**

```sh
gcloud compute instances delete amphive-vm-in --zone=asia-south1-a
```

**6d. Release the static IP** (only after confirming nothing — including any
firewall rule, monitoring check, or hardcoded client config — still
references `8.231.81.12`):

```sh
gcloud compute addresses delete amphive-static-ip --region=asia-south1
```

**6e. Clean up now-orphaned prod-only resources** as applicable: the
`amphive-daily-snapshot` disk-snapshot resource policy (asia-south1), the
`allow-amphive-ports`/`allow-amphive-https` firewall rules if they're scoped
to `amphive-vm-in` alone, and the `gs://amphive-db-backups` nightly cron
(retarget it at the relay per §PREREQ pattern above, or retire it if backups
now run from the relay under a different mechanism — don't just let it start
failing silently).

Update `docs/DEPLOYMENT.md`, `docs/SECURITY.md`, and any other doc that
states `amphive-vm-in` / `8.231.81.12` / `asia-south1` as the production
target, once the relay is confirmed to be the sole production system — see
CLAUDE.md's non-negotiable to keep `docs/` (not scattered copies) current.

---

## Summary: what changes vs. today

| Aspect | Before | After |
|---|---|---|
| Compute | `amphive-vm-in`, e2-standard-2, 8 GB, asia-south1-a, paid | `amphive-relay`, e2-micro, 1 GB, us-west1, free tier |
| Postgres | container on `amphive-vm-in`, `postgres_data` volume | container on `amphive-relay`, restored `postgres_data` volume (same schema/data) |
| MQTT broker | Mosquitto container on `amphive-vm-in`, public `:8883` | Mosquitto container on `amphive-relay`, public `:8883` — **the relay's existing nginx TCP-relay role is retired**, superseded by this local broker |
| Web | Caddy + frontend on `amphive-vm-in` | Caddy + frontend on `amphive-relay` |
| DNS | `amphive.app`/`cpo.amphive.app`/`mqtt.amphive.app` → `8.231.81.12` | same names → `136.117.94.209` |
| MQTT cert | CA + SANs incl. `8.231.81.12` | same CA, SANs now also include `136.117.94.209` |
| Traffic ceiling | headroom for real growth | **hard 1 GB RAM ceiling — demo/low-traffic/portfolio use only**, not a scale target |
