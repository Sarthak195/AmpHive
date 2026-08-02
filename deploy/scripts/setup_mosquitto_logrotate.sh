#!/usr/bin/env bash
# =============================================================================
# Mosquitto log rotation (TD#28 follow-up)
#
# mosquitto.conf's `log_dest file /mosquitto/log/mosquitto.log` (on the
# `mosquitto_log` named volume — see deploy/config/mosquitto.conf) is durable
# across container recreation but has NO built-in rotation, so it grows
# unbounded on disk. The stdout stream is already bounded by the mqtt
# service's compose `logging:` json-file limits; this script bounds the file
# copy the same way an operator would on any Linux box: logrotate.
#
# Run this ON the host that runs the mosquitto container (amphive-relay /
# amphive-vm-in), as root or via sudo — not on the dev box (CLAUDE.md
# non-negotiable: don't run the app stack locally). Idempotent: re-running
# overwrites the same logrotate config with the same content and the final
# `logrotate -d` invocation is a dry run, so this is always safe to re-run
# (e.g. from deploy-relay.sh on every deploy).
#
# Usage: setup_mosquitto_logrotate.sh [docker-volume-name]
#   Defaults to amphive_mosquitto_log (docker compose's
#   "<project>_<volume>" naming for the `mosquitto_log` volume key when the
#   compose project name is "amphive" — i.e. compose was run from a
#   directory named `amphive`, which is deploy-relay.sh's default WORKDIR
#   basename). Pass the volume name explicitly if your compose project name
#   differs: `docker volume ls | grep mosquitto_log`.
# =============================================================================
set -euo pipefail
VOL="${1:-amphive_mosquitto_log}"
MOUNT="$(sudo docker volume inspect "$VOL" --format '{{ .Mountpoint }}')"

# logrotate config: weekly OR 20M, whichever comes first, keep 3 rotations,
# gzip the rotated copies. copytruncate (not create+signal) is deliberate:
# mosquitto has no SIGHUP/reload hook this script wants to depend on, and
# this file is a durable-but-secondary diagnostic copy (stdout via `docker
# logs` is still the operationally-primary stream — see mosquitto.conf's own
# comment) — the handful of bytes copytruncate can lose in the race between
# copying and truncating are an acceptable trade for not having to coordinate
# a container signal/restart on every rotation.
sudo tee /etc/logrotate.d/amphive-mosquitto >/dev/null <<EOF
$MOUNT/mosquitto.log {
    weekly
    rotate 3
    maxsize 20M
    compress
    missingok
    notifempty
    copytruncate
}
EOF
sudo logrotate -d /etc/logrotate.d/amphive-mosquitto
echo "logrotate installed for $MOUNT/mosquitto.log"
