#!/usr/bin/env bash
# =============================================================================
# AmpHive nightly backup — logical DB dump + ops-config tarball -> GCS.
#
# Runs on the prod VM `amphive-relay` (installed at ~/amphive-relay/backup_db.sh,
# shipped with CRLF stripped). Cron: 0 21 * * * (21:00 UTC = 02:30 IST) — see
# deploy/docs/db_backup_restore.md for setup, restore, and the restore test.
# (Pre-2026-08-02 this targeted the deleted amphive-vm-in box; the cron did NOT
# survive the 2026-07-27 consolidation and was reinstated 2026-08-02.)
#
# Design:
#   - pg_dump -Fc (custom format): compressed, supports selective pg_restore.
#   - Config tarball: .env, Caddyfile, compose file, and config/ (mosquitto
#     passwd/ACL — the per-gateway password hashes exist ONLY on this VM — and
#     broker TLS certs). Contains secrets — the bucket is private (PAP
#     enforced, uniform ACL).
#   - Local copies always kept (last 3 sets) in ~/amphive-relay/backups, so
#     backups exist even if the GCS upload path is down; bucket retention is a
#     GCS lifecycle rule, not this script's job.
#   - Upload uses the VM's built-in identity (storage-rw scope + bucket IAM;
#     bucket denies deletes from the VM by design). Upload failure exits 2 but
#     keeps the dump.
# =============================================================================
set -uo pipefail

BASE=/home/Sarthak/amphive-relay
DEST=gs://amphive-db-backups
LOCAL="$BASE/backups"
LOG="$BASE/backup.log"
TS=$(date -u +%Y%m%d-%H%M%S)

mkdir -p "$LOCAL"
log() { echo "[$(date -u '+%F %T')] $*" >> "$LOG"; }

# 1) Logical dump out of the running container.
if sudo docker exec amphive-relay-db-1 pg_dump -U postgres -Fc amphive > "$LOCAL/amphive-$TS.dump" 2>>"$LOG"; then
    log "pg_dump OK: amphive-$TS.dump ($(du -h "$LOCAL/amphive-$TS.dump" | cut -f1))"
else
    log "ERROR: pg_dump failed"
    rm -f "$LOCAL/amphive-$TS.dump"
    exit 1
fi

# 2) Ops-config tarball (config/ holds root/1883-owned mosquitto files).
if sudo tar -czf "$LOCAL/config-$TS.tar.gz" -C "$BASE" .env Caddyfile docker-compose.relay.yml config 2>>"$LOG"; then
    sudo chown "$(id -u):$(id -g)" "$LOCAL/config-$TS.tar.gz"
    log "config tarball OK"
else
    log "WARN: config tarball failed (continuing — DB dump still uploads)"
fi

# 3) Upload both, organized by year/month.
UPLOAD_OK=1
for f in "$LOCAL/amphive-$TS.dump" "$LOCAL/config-$TS.tar.gz"; do
    [ -f "$f" ] || continue
    if gsutil -q cp "$f" "$DEST/$(date -u +%Y/%m)/" 2>>"$LOG"; then
        log "uploaded $(basename "$f")"
    else
        log "ERROR: upload failed for $(basename "$f") (local copy kept)"
        UPLOAD_OK=0
    fi
done

# 4) Local retention: keep the newest 3 of each.
ls -1t "$LOCAL"/amphive-*.dump 2>/dev/null | tail -n +4 | xargs -r rm -f
ls -1t "$LOCAL"/config-*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f

if [ "$UPLOAD_OK" = 1 ]; then
    log "backup complete"
else
    log "backup complete WITH UPLOAD ERRORS"
    exit 2
fi
