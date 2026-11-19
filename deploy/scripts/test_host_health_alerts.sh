#!/usr/bin/env bash
# Local test for check_host_health.sh (no VM, no real mail, no network):
#   bash deploy/scripts/test_host_health_alerts.sh
# Stub sendmail/curl/df on PATH; MEMINFO_FILE points at a fixture.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
STUBS="$W/stubs"; mkdir -p "$STUBS"
MAIL="$W/mail.txt"
cat > "$STUBS/sendmail" <<EOF2
#!/usr/bin/env bash
cat >> "$MAIL"
EOF2
cat > "$STUBS/curl" <<'EOF2'
#!/usr/bin/env bash
[ -z "${STUB_CURL_FAIL:-}" ]
EOF2
cat > "$STUBS/df" <<'EOF2'
#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/root 10000000 5000000 5000000 ${STUB_DISK_USED:-50}% /"
EOF2
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
run() { # fresh STAMP_DIR unless KEEP_STAMPS=1
    [ -n "${KEEP_STAMPS:-}" ] || rm -rf "$W/stamps"
    PATH="$STUBS:$PATH" SENDMAIL="$STUBS/sendmail" STAMP_DIR="$W/stamps" MEMINFO_FILE="$W/meminfo" \
        bash "$HERE/check_host_health.sh" >/dev/null 2>&1
}
meminfo() { printf 'MemTotal: 1013000 kB\nMemAvailable: %s kB\n' "$1" > "$W/meminfo"; }

meminfo 400000
run;                                   check "healthy host sends no mail" nomail
meminfo 51200
run;                                   check "low memory (50 MB) mails" mail "available memory 50 MB"
meminfo 400000; STUB_DISK_USED=95 run; check "low disk (5% free) mails" mail "free disk"
STUB_CURL_FAIL=1 run;                  check "failed health curl mails" mail "did not return 2xx"

meminfo 51200
run;                                   rm -f "$MAIL"
KEEP_STAMPS=1 run;                     check "second run inside cooldown sends no mail" nomail
touch -d '7 hours ago' "$W/stamps/memory"
KEEP_STAMPS=1 run;                     check "after cooldown expires mails again" mail "available memory"

[ "$fail" = 0 ] && echo "ALL PASS" || echo "FAILED"
exit "$fail"
