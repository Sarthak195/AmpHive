# Mosquitto log rotation (TD#28 follow-up)

`deploy/config/mosquitto.conf` mirrors broker logs to two places (see that
file's own "Logging" comment, ~lines 40-52):

- **stdout** — the operationally-primary stream (`docker logs` /
  `docker compose logs mqtt`), already bounded by the `mqtt` service's
  compose `logging:` json-file `max-size`/`max-file` limits.
- **`/mosquitto/log/mosquitto.log`** on the `mosquitto_log` named volume — a
  durable copy that survives container *recreation* (not just restarts), so
  it doesn't disappear on the next `deploy.ps1` / `deploy-relay.sh` run. This
  file has **no built-in rotation** — mosquitto never truncates or rolls it —
  so left alone it grows unbounded on disk.

## The fix

`deploy/scripts/setup_mosquitto_logrotate.sh [docker-volume-name]` installs a
standard `logrotate` config (`/etc/logrotate.d/amphive-mosquitto`) for that
file: weekly **or** 20 MB (whichever comes first), keep 3 rotations, gzip
compress. It uses **`copytruncate`**, deliberately: mosquitto has no
SIGHUP/reload hook this script wants to couple to, and this file is a
secondary diagnostic copy (stdout remains authoritative) — the few bytes
`copytruncate` can theoretically lose in the copy/truncate race are an
acceptable trade for not signaling or restarting the container on every
rotation.

The script is idempotent (safe to re-run): it overwrites the same config
file with the same content every time, and the final step is
`logrotate -d` (dry run only — it never rotates as a side effect of being
run, only when the real `logrotate` cron invokes the installed config).

`deploy/relay/deploy-relay.sh` calls it automatically after bringing the
compose stack up (step 6a), passing the volume name derived the same way
`docker compose` derives its project name (`$WORKDIR`'s basename — see that
script). If `deploy/scripts/` wasn't staged alongside `deploy/relay/` on the
target host, that step logs the manual fallback command instead of failing
the deploy.

## Manual invocation

Run directly on the host that runs the mosquitto container (not the dev
box — CLAUDE.md non-negotiable: don't run the app stack locally):

```sh
# Find the actual volume name if unsure:
docker volume ls | grep mosquitto_log

# Then, on amphive-relay (default project name "amphive"):
sudo bash deploy/scripts/setup_mosquitto_logrotate.sh amphive_mosquitto_log
```

No mosquitto.conf or compose changes are involved — this is host-level
logrotate configuration only.
