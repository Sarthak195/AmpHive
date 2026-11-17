#!/usr/bin/env bash
# =============================================================================
# AmpHive host health check: mail the operator when the VM is about to run out
# of memory or disk, or when the backend stops answering. Runs from the HOST
# cron, so it still alerts when the app container is dead or evicted (the
# 2026-11-16 failure mode); an in-app alerter could not report that.
#
# Install on the prod VM (normal user crontab, every 5 minutes):
#   */5 * * * * ~/amphive-relay/check_host_health.sh
# Details: deploy/docs/host_health_alerts.md
#
# Checks (each with its own cooldown so a standing fault mails once per
# COOLDOWN_MIN, not every run):
#   memory  MemAvailable below MIN_MEM_MB            (default 100)
#   disk    free space on / below MIN_DISK_FREE_PCT  (default 10)
#   health  curl of HEALTH_URL fails or is non-2xx   (default http://localhost/api/health)
# Env overrides: ALERT_TO, SENDMAIL, MIN_MEM_MB, MIN_DISK_FREE_PCT, HEALTH_URL,
#   COOLDOWN_MIN (default 360), STAMP_DIR (default ~/.amphive-health),
#   MEMINFO_FILE (default /proc/meminfo).
# Exit 0 all ok (or alerts suppressed by cooldown) | 1 at least one alert sent.
# =============================================================================
set -u

ALERT_TO="${ALERT_TO:-support@amphive.app}"
SENDMAIL="${SENDMAIL:-/usr/sbin/sendmail}"
MIN_MEM_MB="${MIN_MEM_MB:-100}"
MIN_DISK_FREE_PCT="${MIN_DISK_FREE_PCT:-10}"
HEALTH_URL="${HEALTH_URL:-http://localhost/api/health}"
COOLDOWN_MIN="${COOLDOWN_MIN:-360}"
STAMP_DIR="${STAMP_DIR:-$HOME/.amphive-health}"
MEMINFO_FILE="${MEMINFO_FILE:-/proc/meminfo}"
mkdir -p "$STAMP_DIR" 2>/dev/null

PROBLEMS=""
STAMPS=""

# cooled_down NAME: true when no stamp exists or it is older than COOLDOWN_MIN.
cooled_down() {
    local f="$STAMP_DIR/$1"
    [ ! -e "$f" ] || [ -n "$(find "$f" -mmin +"$COOLDOWN_MIN" 2>/dev/null)" ]
}
# problem NAME TEXT: record it unless a recent alert for NAME is still cooling.
problem() {
    cooled_down "$1" || return 0
    PROBLEMS="$PROBLEMS- $2"$'\n'
    STAMPS="$STAMPS $1"
}

avail_kb=$(awk '/^MemAvailable:/ {print $2}' "$MEMINFO_FILE" 2>/dev/null)
if [ -n "$avail_kb" ] && [ $((avail_kb / 1024)) -lt "$MIN_MEM_MB" ]; then
    problem memory "available memory $((avail_kb / 1024)) MB is below ${MIN_MEM_MB} MB"
fi

used_pct=$(df -Pk / 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
if [ -n "$used_pct" ] && [ $((100 - used_pct)) -lt "$MIN_DISK_FREE_PCT" ]; then
    problem disk "free disk on / is $((100 - used_pct))%, below ${MIN_DISK_FREE_PCT}%"
fi

if ! curl -fsS -m 10 -o /dev/null "$HEALTH_URL" 2>/dev/null; then
    problem health "$HEALTH_URL did not return 2xx within 10 s"
fi

[ -n "$PROBLEMS" ] || exit 0

{
    echo "To: $ALERT_TO"
    echo "Subject: [AmpHive] HOST HEALTH ALERT on $(hostname)"
    echo
    echo "check_host_health.sh found:"
    printf '%s' "$PROBLEMS"
    echo
    echo "Check: free -m; df -h /; sudo docker ps; sudo docker stats --no-stream"
    echo "Repeat alerts for the same check are suppressed for ${COOLDOWN_MIN} min."
} | "$SENDMAIL" -t >/dev/null 2>&1 || { echo "check_host_health.sh: sendmail failed; $PROBLEMS" >&2; exit 1; }

for n in $STAMPS; do touch "$STAMP_DIR/$n"; done
exit 1
