#!/usr/bin/env bash
# =============================================================================
# AmpHive nightly backup — logical DB dump + ops-config tarball -> GCS.
#
# Runs on the prod VM `amphive-relay`, installed at ~/amphive-relay/backup_db.sh
# BY HAND (deploy.ps1 does not ship it; strip CRLF when copying it over).
# Cron: 0 21 * * * (21:00 UTC = 02:30 IST) in the NORMAL user's crontab; the
# script calls sudo itself where it needs root. NEVER run it with sudo (see
# the guard below). Setup, restore, the restore test and the 2026-10-20
# incident: deploy/docs/db_backup_restore.md.
# (Pre-2026-08-02 this targeted the deleted amphive-vm-in box; the cron did NOT
# survive the 2026-07-27 consolidation and was reinstated 2026-08-02.)
#
# LIVE COPY DRIFT: the copy on the VM also tars the mail store
# (/var/mail/vhosts, deploy/docs/mail_server.md gap 6) and /etc/dovecot/users,
# which this repo copy does not. Merge changes INTO the live copy (or pull it
# into the repo first); never overwrite it with this file.
#
# Exit codes: 0 ok | 1 no DB container / pg_dump failed | 2 upload failed
# (local copy kept) | 3 refused by the guard below (nothing done).
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
# The original gs://amphive-db-backups went away with the old GCP project in
# the 2026-09-04 account move; the live script uploads to the -2 bucket.
DEST=gs://amphive-db-backups-2
LOCAL="$BASE/backups"
LOG="$BASE/backup.log"
TS=$(date -u +%Y%m%d-%H%M%S)

# Guard (2026-10-20 incident): a manual `sudo ./backup_db.sh` on 2026-09-04
# left backup.log and backups/ owned by root. Every nightly cron run (as the
# normal user) then died in under a second, its only output was a cron mail
# that bounced (no local mailbox), and no backup was taken for 45 days. So:
# refuse to run as root, and fail LOUDLY when the log/backup paths are not
# writable, on stderr and in syslog (`journalctl -t amphive-backup`), because
# backup.log is exactly what may be unwritable.
guard_fail() {
    echo "backup_db.sh: ERROR: $1 NO BACKUP TAKEN." >&2
    logger -t amphive-backup -p user.err "ERROR: $1 NO BACKUP TAKEN." 2>/dev/null || true
    exit 3
}
ME=$(id -un)
[ "$(id -u)" -ne 0 ] || guard_fail "refusing to run as root - never run this script with sudo (it uses sudo itself where needed, and a root run leaves root-owned files that break the nightly cron)."
mkdir -p "$LOCAL" 2>/dev/null
{ [ -d "$LOCAL" ] && [ -w "$LOCAL" ]; } \
    || guard_fail "cannot write $LOCAL as $ME - owned by root? never run this script with sudo (fix: sudo chown -R $ME: $LOCAL $LOG)."
( : >> "$LOG" ) 2>/dev/null \
    || guard_fail "cannot write $LOG as $ME - owned by root? never run this script with sudo (fix: sudo chown $ME: $LOG)."
[ ! -e "$LOCAL/LAST_SUCCESS" ] || [ -w "$LOCAL/LAST_SUCCESS" ] \
    || guard_fail "cannot write $LOCAL/LAST_SUCCESS as $ME - owned by root? never run this script with sudo (fix: sudo chown -R $ME: $LOCAL)."

log() { echo "[$(date -u '+%F %T')] $*" >> "$LOG"; }

# 1) Logical dump out of the running container.
#
# The container name is RESOLVED, not hardcoded. It used to be a literal
# `amphive-relay-db-1` — the compose-project-default name, which is what the
# VM actually runs. But the repo's deploy/relay/docker-compose.relay.yml sets
# `container_name: amphive-db`, so a stack rebuilt from the repo file would
# name it differently and this backup would fail EVERY NIGHT while only
# writing to a log nobody reads. Match on the compose service label instead,
# which is stable under both naming schemes, and fall back to either literal.
DB_CONTAINER=$(sudo docker ps --filter "label=com.docker.compose.service=db" \
                              --format '{{.Names}}' | head -1)
if [ -z "$DB_CONTAINER" ]; then
    for candidate in amphive-relay-db-1 amphive-db; do
        if sudo docker ps --format '{{.Names}}' | grep -qx "$candidate"; then
            DB_CONTAINER="$candidate"; break
        fi
    done
fi
if [ -z "$DB_CONTAINER" ]; then
    log "ERROR: no running Postgres container found (looked for the compose 'db' service, amphive-relay-db-1, amphive-db) - NO BACKUP TAKEN"
    exit 1
fi
log "using db container: $DB_CONTAINER"

if sudo docker exec "$DB_CONTAINER" pg_dump -U postgres -Fc amphive > "$LOCAL/amphive-$TS.dump" 2>>"$LOG"; then
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
# Freshness marker. The cron only writes to backup.log, so a silently
# failing backup is invisible until the day someone needs a restore. This
# file's MTIME is the last KNOWN-GOOD upload, so a stale backup is one
# `ls -l` away instead of a log archaeology exercise:
#   ls -l ~/amphive-relay/backups/LAST_SUCCESS
# (Still not alerting - see docs/SECURITY.md. A cron that fails silently is
# a backup you do not have.)
date -u +%Y-%m-%dT%H:%M:%SZ > "$LOCAL/LAST_SUCCESS"
else
    log "backup complete WITH UPLOAD ERRORS"
    exit 2
fi
