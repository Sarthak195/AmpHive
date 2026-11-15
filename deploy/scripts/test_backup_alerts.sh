#!/usr/bin/env bash
# Local test for the backup failure alerts (no VM, no docker, no GCS):
#   bash deploy/scripts/test_backup_alerts.sh
# A stub sendmail writes its stdin to a file; stub sudo/docker/gsutil drive
# backup_db.sh down its success and failure paths. Asserts mail did / did not
# arrive. Exits non-zero on the first failed assertion.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
STUBS="$W/stubs"; mkdir -p "$STUBS"
MAIL="$W/mail.txt"

cat > "$STUBS/sendmail" <<EOF
#!/usr/bin/env bash
cat >> "$MAIL"
EOF
cat > "$STUBS/sudo" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = chown ] && exit 0
exec "$@"
EOF
cat > "$STUBS/docker" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  ps) [ -n "${STUB_NO_DB:-}" ] || echo amphive-db ;;
  exec) echo FAKE-DUMP ;;
esac
EOF
cat > "$STUBS/gsutil" <<'EOF'
#!/usr/bin/env bash
[ -z "${STUB_UPLOAD_FAIL:-}" ]
EOF
cat > "$STUBS/logger" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUBS"/*

fail=0
check() { # name, expect (mail|nomail), [grep pattern]
    local got=nomail; [ -s "$MAIL" ] && got=mail
    if [ "$got" = "$2" ] && { [ -z "${3:-}" ] || grep -q -- "$3" "$MAIL"; }; then
        echo "PASS $1"
    else
        echo "FAIL $1 (expected $2${3:+ containing '$3'}, got $got)"; [ -s "$MAIL" ] && cat "$MAIL"; fail=1
    fi
    rm -f "$MAIL"
}
run_backup() { # fresh BASE each time
    rm -rf "$W/base"; mkdir -p "$W/base"
    touch "$W/base/.env" "$W/base/Caddyfile" "$W/base/docker-compose.relay.yml"; mkdir "$W/base/config"
    PATH="$STUBS:$PATH" SENDMAIL="$STUBS/sendmail" BASE="$W/base" bash "$HERE/backup_db.sh" >/dev/null 2>&1
    echo $?
}

# backup_db.sh
rc=$(run_backup);                          [ "$rc" = 0 ] || { echo "FAIL ok-run rc=$rc"; fail=1; }
check "backup success sends no mail" nomail
export STUB_NO_DB=1; rc=$(run_backup); unset STUB_NO_DB
[ "$rc" = 1 ] || { echo "FAIL no-db rc=$rc"; fail=1; }
check "no DB container mails, names step 1" mail "step: 1 find DB"
export STUB_UPLOAD_FAIL=1; rc=$(run_backup); unset STUB_UPLOAD_FAIL
[ "$rc" = 2 ] || { echo "FAIL upload rc=$rc"; fail=1; }
check "upload failure mails, names step 3, has log tail" mail "upload failed"

# check_backup_fresh.sh
run_fresh() { SENDMAIL="$STUBS/sendmail" BACKUP_DIR="$W/b" bash "$HERE/check_backup_fresh.sh" >/dev/null 2>&1; }
mkdir -p "$W/b"; rm -f "$W/b/LAST_SUCCESS"
run_fresh; check "missing LAST_SUCCESS mails" mail "missing"
date -u +%FT%TZ > "$W/b/LAST_SUCCESS"
run_fresh; check "fresh LAST_SUCCESS sends no mail" nomail
touch -d '30 hours ago' "$W/b/LAST_SUCCESS"
run_fresh; check "stale LAST_SUCCESS (30h) mails" mail "older than 26h"
touch -d '25 hours ago' "$W/b/LAST_SUCCESS"
run_fresh; check "25h-old LAST_SUCCESS sends no mail" nomail

[ "$fail" = 0 ] && echo "ALL PASS" || echo "FAILED"
exit "$fail"
