# Database & config backups — setup, restore, restore test

*Opened 2026-07-11. Two independent layers: nightly logical dumps (application-
consistent, testable, per-table restore) and daily GCP disk snapshots
(crash-consistent, whole-VM disaster recovery).*

## What runs

| Layer | Schedule | Retention | Where |
|-------|----------|-----------|-------|
| `pg_dump -Fc` of `amphive` + ops-config tarball | cron `0 21 * * *` (21:00 UTC = 02:30 IST) via `~/amphive/backup_db.sh` | GCS: 30-day lifecycle rule · local `~/amphive/backups/`: last 3 sets | `gs://amphive-db-backups/YYYY/MM/` (private, PAP enforced, asia-south1) |
| Disk snapshot of `amphive-vm-in` | daily 20:00 UTC (resource policy `amphive-daily-snapshot`) | 14 days, kept on disk delete | GCP snapshots, asia-south1 |

The config tarball holds `.env`, `Caddyfile`, `mosquitto.conf`,
`mosquitto_passwd`, `mosquitto_acl`, `mqtt-certs/` — the **per-gateway broker
password hashes exist only on the VM**, so losing them silently would mean
re-provisioning every fielded device. The tarball contains secrets; the bucket
is private and unlisted principals have no access.

The script always writes the local copy first and treats the GCS upload as
best-effort (exit 2 + `~/amphive/backup.log` entry on failure), so a broken
upload path degrades to local-only backups instead of none.

`deploy.ps1` re-ships `deploy/scripts/backup_db.sh` to the VM on every deploy
(CRLF stripped); the cron entry is one-time (already installed 2026-07-11).

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

```sh
# Latest local dump (or: gsutil cp gs://amphive-db-backups/YYYY/MM/<name>.dump .)
DUMP=$(ls -1t ~/amphive/backups/amphive-*.dump | head -1)

# Into a fresh/empty database:
sudo docker exec amphive-db createdb -U postgres amphive_new
sudo docker exec -i amphive-db pg_restore -U postgres -d amphive_new < "$DUMP"

# Over the live DB (DESTRUCTIVE — stop the backend first):
sudo docker stop amphive-backend
sudo docker exec -i amphive-db pg_restore -U postgres -d amphive --clean --if-exists < "$DUMP"
sudo docker start amphive-backend
```

Config files: `sudo tar -xzf config-<ts>.tar.gz -C /home/Sarthak/amphive` then
restart the affected containers (`mqtt` for passwd/ACL/certs, `caddy` for the
Caddyfile). Full-VM loss: create the instance, restore the newest **disk
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
