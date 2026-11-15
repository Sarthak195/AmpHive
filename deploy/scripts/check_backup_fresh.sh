#!/usr/bin/env bash
# =============================================================================
# AmpHive backup freshness check: mail the operator when the nightly backup did
# not run or did not succeed. backup_db.sh mails on its own failures; this is
# the second net for "cron never ran / VM was down / script was removed".
#
# Runs on the prod VM from the NORMAL user's crontab, a few hours after the
# backup (backup is 21:00 UTC):  0 1 * * * ~/amphive-relay/check_backup_fresh.sh
# Install steps: deploy/docs/db_backup_restore.md ("Backup failure alerts").
#
# Alerts when backups/LAST_SUCCESS is missing or older than MAX_AGE_HOURS
# (default 26). Env overrides: BACKUP_DIR, ALERT_TO, SENDMAIL, MAX_AGE_HOURS.
# Exit 0 fresh | 1 stale or missing (alert sent) .
# =============================================================================
set -u

BACKUP_DIR="${BACKUP_DIR:-/home/Sarthak/amphive-relay/backups}"
ALERT_TO="${ALERT_TO:-support@amphive.app}"
SENDMAIL="${SENDMAIL:-/usr/sbin/sendmail}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-26}"
MARKER="$BACKUP_DIR/LAST_SUCCESS"

if [ ! -e "$MARKER" ]; then
    WHY="$MARKER is missing (no backup has ever succeeded here, or it was deleted)"
elif [ -n "$(find "$MARKER" -mmin +$((MAX_AGE_HOURS * 60)) 2>/dev/null)" ]; then
    WHY="$MARKER is older than ${MAX_AGE_HOURS}h (last good backup: $(cat "$MARKER" 2>/dev/null || echo unknown))"
else
    exit 0
fi

{
    echo "To: $ALERT_TO"
    echo "Subject: [AmpHive] BACKUP MISSING or STALE (> ${MAX_AGE_HOURS}h)"
    echo
    echo "check_backup_fresh.sh on $(hostname): $WHY"
    echo
    echo "Check: crontab -l; tail -n 30 ~/amphive-relay/backup.log; ls -l $BACKUP_DIR"
    echo "Runbook: deploy/docs/db_backup_restore.md"
} | "$SENDMAIL" -t >/dev/null 2>&1 || echo "check_backup_fresh.sh: sendmail failed; $WHY" >&2
exit 1
