# Database & config backups — setup, restore, restore test

*Opened 2026-07-11. Two independent layers: nightly logical dumps (application-
consistent, testable, per-table restore) and daily GCP disk snapshots
(crash-consistent, whole-VM disaster recovery).*

## What runs

| Layer | Schedule | Retention | Where |
|-------|----------|-----------|-------|
| `pg_dump -Fc` of `amphive` + ops-config tarball (+ mail-store tarball, live copy only, see below) | cron `0 21 * * *` (21:00 UTC = 02:30 IST) in the **normal user's** crontab, running `~/amphive-relay/backup_db.sh` | local `~/amphive-relay/backups/`: last 3 sets (log: `~/amphive-relay/backup.log`) · GCS: 30-day lifecycle rule on the original bucket (not re-verified on `-2`) | `gs://amphive-db-backups-2/YYYY/MM/` (private, PAP enforced, asia-south1) since the 2026-09-04 GCP account move; the original `gs://amphive-db-backups` went with the old project |
| Disk snapshot | daily 20:00 UTC (resource policy `amphive-daily-snapshot`) | 14 days, kept on disk delete | GCP snapshots. Set up for `amphive-vm-in`, re-created for `amphive-relay` on 2026-08-02 in the old project. **Not verified since the move to the new project**: check with `gcloud compute resource-policies list` |

(The setup history and restore test below are dated and keep the names of
their day: `amphive-vm-in`, `~/amphive`, the `amphive-db` container.)

The config tarball holds `.env`, `Caddyfile`, `mosquitto.conf`,
`mosquitto_passwd`, `mosquitto_acl`, `mqtt-certs/` — the **per-gateway broker
password hashes exist only on the VM**, so losing them silently would mean
re-provisioning every fielded device. The tarball contains secrets; the bucket
is private and unlisted principals have no access.

The script always writes the local copy first and treats the GCS upload as
best-effort (exit 2 + `~/amphive-relay/backup.log` entry on failure), so a
broken upload path degrades to local-only backups instead of none.

`deploy.ps1` does **not** ship `backup_db.sh`: since its 2026-08-02 relay
rewrite it ships only `backend/` + `frontend/`. The VM copy is installed by
hand, and it has drifted from `deploy/scripts/backup_db.sh`: it also tars the
mail store and `/etc/dovecot/users` (mail_server.md gap 6), which the repo copy
lacks. To change it, merge the change into the live
`~/amphive-relay/backup_db.sh` (or first copy the live file into the repo),
strip CRLF, and keep it executable. Never overwrite the live copy with the
repo file as-is.

## Incident 2026-10-20: 45 days without a backup

**What happened.** During the 2026-09-04 GCP account move the script was
test-run by hand as `sudo ./backup_db.sh`. That run worked, but it created
`~/amphive-relay/backup.log` and `~/amphive-relay/backups/` owned by
**root**. The nightly cron runs as the normal user, so from then on every run
died in under a second: it could neither append to its log nor write a dump
into `backups/`. Cron mailed the error output to the user, and that mail
bounced because the VM has no local mailbox. Nothing else was watching, so
**no backup was taken for 45 days**.

**Fix (2026-10-20).** `chown` of `backup.log` and `backups/` back to the
normal user, then one manual run as that user (no sudo), which uploaded to
`gs://amphive-db-backups-2/2026/10/`.

**So it can't fail silently again**, `backup_db.sh` now opens with a guard:

- It refuses to run as root (exit 3). It already uses `sudo` itself for the
  steps that need root, so there is never a reason to run the whole script
  with sudo.
- If `backups/`, `backup.log` or `backups/LAST_SUCCESS` is not writable, it
  exits 3 with `ERROR: cannot write … - owned by root? never run this script
  with sudo (fix: sudo chown …)`. The message goes to **stderr** and to
  **syslog** (`journalctl -t amphive-backup`), so it no longer depends on the
  very log file that is broken.

Rule: run it by hand only as the normal user, i.e. `~/amphive-relay/backup_db.sh`.

### Is the backup actually running?

```sh
ls -l ~/amphive-relay/backups/LAST_SUCCESS    # mtime = last good upload; older than a day = broken
tail -5 ~/amphive-relay/backup.log
journalctl -t amphive-backup --since "2 days ago"   # guard refusals land here
ls -ld ~/amphive-relay/backup.log ~/amphive-relay/backups   # owner must be the normal user, not root
```

There is still no alerting, so a failing backup only shows up if someone
looks (see docs/SECURITY.md).

## One-time setup (DONE 2026-07-11, owner-approved)

The VM's OAuth scope was `devstorage.read_only` — uploads failed with
`403 Provided scope(s) are not authorized` until it was raised. Scopes are
immutable on a running instance, so this cost **~48 s of downtime**
(02:34:45–02:35:33 UTC; containers auto-started, both gateways auto-reconnected
within ~2.5 min, the LE cert persisted in the `caddy_data` volume):

```powershell
gcloud compute instances stop amphive-vm-in --zone=asia-south1-a
gcloud compute instances set-service-account amphive-vm-in --zone=asia-south1-a `
  --service-account=930756667383-compute@developer.gserviceaccount.com `
  --scopes=https://www.googleapis.com/auth/devstorage.read_write,https://www.googleapis.com/auth/logging.write,https://www.googleapis.com/auth/monitoring.write,https://www.googleapis.com/auth/pubsub,https://www.googleapis.com/auth/service.management.readonly,https://www.googleapis.com/auth/servicecontrol,https://www.googleapis.com/auth/trace.append
gcloud compute instances start amphive-vm-in --zone=asia-south1-a

# Bucket-scoped IAM for the VM's identity (create + read, no delete/overwrite):
gcloud storage buckets add-iam-policy-binding gs://amphive-db-backups --member="serviceAccount:930756667383-compute@developer.gserviceaccount.com" --role=roles/storage.objectCreator
gcloud storage buckets add-iam-policy-binding gs://amphive-db-backups --member="serviceAccount:930756667383-compute@developer.gserviceaccount.com" --role=roles/storage.objectViewer
```

**Gotcha found doing this:** gsutil caches the metadata-server token in
`~/.gsutil/gcecredcache` / `credstore2` — after a scope change it keeps using
the old read-only token (same 403) until the cache is removed:
`rm -f ~/.gsutil/credstore2* ~/.gsutil/gcecredcache*`.

**Verified 2026-07-11:** `backup_db.sh` exit 0; dump + config tarball listed in
`gs://amphive-db-backups/2026/07/`; a `gsutil rm` from the VM is **denied**
(`storage.objects.delete` — no role grants it), so a compromised VM cannot
destroy the backups.

(The `amphive-backup` service account was a key-file fallback that was never
used — deleted 2026-07-11.)

## Restore — logical dump

On the VM, from `~/amphive-relay` (compose service names work whatever the
containers happen to be called):

```sh
cd ~/amphive-relay
# Latest local dump (or: gcloud storage cp gs://amphive-db-backups-2/YYYY/MM/<name>.dump .)
DUMP=$(ls -1t ~/amphive-relay/backups/amphive-*.dump | head -1)

# Into a fresh/empty database:
sudo docker compose -f docker-compose.relay.yml exec -T db createdb -U postgres amphive_new
sudo docker compose -f docker-compose.relay.yml exec -T db pg_restore -U postgres -d amphive_new < "$DUMP"

# Over the live DB (DESTRUCTIVE — stop the backend first):
sudo docker compose -f docker-compose.relay.yml stop backend
sudo docker compose -f docker-compose.relay.yml exec -T db pg_restore -U postgres -d amphive --clean --if-exists < "$DUMP"
sudo docker compose -f docker-compose.relay.yml start backend
```

Config files: `sudo tar -xzf config-<ts>.tar.gz -C /home/Sarthak/amphive-relay`
then restart the affected containers (`mqtt` for passwd/ACL/certs, `caddy` for
the Caddyfile). Full-VM loss: create the instance, restore the newest **disk
snapshot** first, then apply the newest logical dump on top if it is fresher.

## Restore test (do this quarterly)

Performed 2026-07-11 against the first real dump (523 KB):

```sh
sudo docker exec amphive-db createdb -U postgres amphive_restore_test
sudo docker exec -i amphive-db pg_restore -U postgres -d amphive_restore_test < "$DUMP"   # exit 0
# row counts live | restored — matched exactly (telemetry_readings differed by
# 6 rows: live ingest continued after the dump; expected):
# users 4|4 · charging_sessions 19|19 · gateways 3|3 · plugs 3|3 ·
# telemetry_readings 44204|44198 · gateway_events 4|4
sudo docker exec amphive-db psql -U postgres -d amphive_restore_test -tAc "SELECT count(*) FROM ledger_transactions"
sudo docker exec amphive-db dropdb -U postgres amphive_restore_test
```
