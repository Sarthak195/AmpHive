# Host health alerts

`deploy/scripts/check_host_health.sh` runs from the host cron and mails
`ALERT_TO` (default `support@amphive.app`, via host Postfix) when:

- available memory drops below `MIN_MEM_MB` (default 100),
- free space on `/` drops below `MIN_DISK_FREE_PCT` (default 10),
- `curl http://localhost/api/health` fails or is non-2xx (`HEALTH_URL`).

It runs outside the app, so it still alerts when the backend container is
dead or evicted. Each check has its own stamp file in `~/.amphive-health`, so a
standing fault mails once per `COOLDOWN_MIN` (default 360), not every run.

## Install (operator step, normal user, not sudo)

```sh
scp deploy/scripts/check_host_health.sh amphive-relay:/tmp/
ssh amphive-relay 'sed -i "s/\r$//" /tmp/check_host_health.sh && install -m 755 /tmp/check_host_health.sh ~/amphive-relay/check_host_health.sh'
# crontab -e:
#   */5 * * * * ~/amphive-relay/check_host_health.sh
```

Verify: `MIN_MEM_MB=999999 STAMP_DIR=/tmp/hh-test ~/amphive-relay/check_host_health.sh`
should mail you and exit 1. Local test (stub sendmail/curl/df, no VM):
`bash deploy/scripts/test_host_health_alerts.sh`.

The admin console page `/admin/health` (GET `/api/admin/health`, admin only)
shows the same numbers live plus cgroup memory, backend RSS, database size and
connections, and the reaper and telemetry-flush last-run times.
