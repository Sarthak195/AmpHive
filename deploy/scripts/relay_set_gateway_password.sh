#!/usr/bin/env bash
# =============================================================================
# Create or reset ONE gateway's broker account on the amphive-relay VM.
#
# Runs ON the VM as the normal user (it calls sudo itself). Normally driven by
# deploy/scripts/add_gateway_user.ps1, which scp's this file to /tmp and runs
#   bash /tmp/relay_set_gateway_password.sh <gateway_id> [--force]
# over plain OpenSSH with the password on STDIN (first line). The password
# never appears on an ssh/sudo command line or in shell history. (It is
# briefly on the argv of mosquitto_passwd / mosquitto_sub inside the
# throwaway containers below, i.e. visible to root on the VM for about a
# second.)
#
# Username == gateway_id. The broker ACL line
#   pattern readwrite amphive/gateways/%u/#
# scopes every account to its own subtree, so no ACL edit is needed.
#
# RESETTING an existing account locks that gateway out until its own NVS
# holds the new password (see (c)). So in update mode this first asks the
# database and refuses (exit 9) while any of the gateway's plugs has an
# active charging session: the session would run on under the firmware's
# local watchdogs only, unbilled, until the backend's stale-session reaper
# ended it. --force skips that check (e.g. to lock out a compromised gateway
# at once).
#
# Generalised from the reset script used live on 2026-10-19 for 1cc3abb4fb54.
# Gotchas it encodes:
#  (a) Feed the password over PLAIN OpenSSH, never `gcloud compute ssh`: on
#      Windows gcloud goes through plink, which auto-accepts the host key by
#      writing a "y" line into the remote stdin, so the first line this
#      script reads would be "y" instead of the password.
#  (b) mosquitto_passwd rejects a file containing blank lines ("Corrupt
#      password file") although the broker itself ignores them, so it runs
#      on a copy with blank lines stripped, never on the live file. The
#      sanity check compares that copy with the LIVE file, so a failed or
#      truncated copy can never vouch for itself.
#  (c) The live file (~/amphive-relay/config/mosquitto_passwd, owner
#      1883:1883 mode 600) is a SINGLE-FILE bind mount (:ro) into the broker
#      container. Docker pins the inode at container start, so a replaced
#      file (mv, sed -i, mosquitto_passwd's own rewrite) is invisible to the
#      broker. Write back IN PLACE with `cat tmp > live` (keeps inode, owner
#      and mode), then SIGHUP the broker: it reloads passwd + ACL without
#      dropping the OTHER connected clients. The gateway whose password just
#      changed loses its login either way: mosquitto 2.x re-checks connected
#      clients' credentials on reload, so expect it to be dropped right
#      then (not tested here); at the latest its next reconnect fails.
#  (d) Verify by subscribing AS the gateway to its retained
#      amphive/gateways/<id>/config (the plug roster), from inside the
#      broker's network namespace (plaintext 1883 is not published on the
#      host).
#
# Exit codes:
#   0  done and verified
#   2  bad arguments / password     \
#   3  broker container or passwd    |
#      mount not found               |
#   9  active charging session on    |
#      the gateway's plugs, or the   |  live file NOT touched
#      check could not run           |
#   4  backup or work copy failed    |
#   5  mosquitto_passwd failed       |
#   7  sanity check failed          /
#   6  live file WAS updated, but the HUP or the verify failed (backup kept)
#   8  write-back failed: restore from the backup it names
# =============================================================================
set -uo pipefail

USAGE="usage: bash $0 <gateway_id> [--force]   (password on stdin)"
case $# in
  1) FORCE=0 ;;
  2) [ "$2" = --force ] || { echo "$USAGE"; exit 2; }; FORCE=1 ;;
  *) echo "$USAGE"; exit 2 ;;
esac
GW=$1
# Used in grep patterns and as the MQTT username (63 bytes max: the
# firmware's mqtt_user buffer is 64 including the NUL).
[[ $GW =~ ^[A-Za-z0-9_-]{1,63}$ ]] || { echo "refusing: bad gateway id '$GW'"; exit 2; }

IFS= read -r PW || true
exec </dev/null    # nothing below may read the rest of stdin
PW=${PW%$'\r'}     # a PowerShell pipe ends the line with CRLF
# 16-63 chars of [A-Za-z0-9_-]: unguessable, fits the firmware's 64-byte
# mqtt_pwd buffer, and safe to type into the setup portal or a KEY=VALUE file.
[[ $PW =~ ^[A-Za-z0-9_-]{16,63}$ ]] || {
  echo "refusing: password missing or not 16-63 chars of [A-Za-z0-9_-]"; exit 2; }

# A compose service's running container: by the compose service label, which
# holds under both the compose-default names on the VM (amphive-relay-mqtt-1)
# and the repo compose's container_name (amphive-mqtt), else by those literal
# names. backup_db.sh resolves db the same way.
find_container() {
  local svc=$1 name='' cand
  shift
  name=$(sudo docker ps --filter "label=com.docker.compose.service=$svc" --format '{{.Names}}' | head -1)
  if [ -z "$name" ]; then
    for cand in "$@"; do
      if sudo docker ps --format '{{.Names}}' | grep -qx "$cand"; then name=$cand; break; fi
    done
  fi
  printf '%s\n' "$name"
}

# Broker container; MQTT_CONTAINER overrides.
C=${MQTT_CONTAINER:-$(find_container mqtt amphive-relay-mqtt-1 amphive-mqtt)}
[ -n "$C" ] || { echo "no running broker container found (set MQTT_CONTAINER=<name>)"; exit 3; }
# Same image as the running broker, so the hash format matches and nothing
# has to be pulled.
IMG=$(sudo docker inspect "$C" --format '{{.Config.Image}}')
[ -n "$IMG" ] || IMG=eclipse-mosquitto:2.0
echo "broker container: $C ($IMG)"

SRC=$(sudo docker inspect "$C" --format '{{range .Mounts}}{{if eq .Destination "/mosquitto/config/passwd"}}{{.Source}}{{end}}{{end}}')
if [ -z "$SRC" ]; then
  echo "passwd mount not found on $C; its mounts are:"
  sudo docker inspect "$C" --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
  exit 3
fi
echo "passwd mount: $SRC -> /mosquitto/config/passwd"
echo "before: $(sudo stat -c 'inode=%i owner=%u:%g mode=%a size=%s' "$SRC")"
COUNT=$(sudo grep -c "^$GW:" "$SRC")
case $COUNT in
  0) MODE=create ;;
  1) MODE=update ;;
  *) echo "refusing: $COUNT entries for $GW in $SRC - fix the file by hand first"; exit 7 ;;
esac
echo "account $GW: $MODE"

# Never reset a gateway mid-session (see the header). Update mode only: a
# gateway with no account yet can't be connected.
if [ "$MODE" = update ] && [ "$FORCE" = 1 ]; then
  echo "--force: NOT checking $GW for an active charging session"
elif [ "$MODE" = update ]; then
  DBC=${DB_CONTAINER:-$(find_container db amphive-relay-db-1 amphive-db)}
  ACTIVE=''
  if [ -n "$DBC" ]; then
    # $GW matched ^[A-Za-z0-9_-]+$ above, so it is safe inside the quotes.
    ACTIVE=$(sudo docker exec "$DBC" psql -U postgres -d amphive -tAc \
      "SELECT count(*) FROM charging_sessions s JOIN plugs p ON p.id = s.plug_id WHERE p.gateway_id = '$GW' AND s.status = 'active'" 2>&1)
  fi
  case $ACTIVE in
    0) echo "no active charging session on $GW's plugs" ;;
    ''|*[!0-9]*)
      echo "refusing: could not check $GW for an active charging session (db container: ${DBC:-not found}; psql: ${ACTIVE:-no output})"
      echo "Check the CPO dashboard yourself, then re-run with --force."
      exit 9 ;;
    *)
      echo "refusing: $ACTIVE active charging session(s) on $GW's plugs - a reset now locks the gateway out mid-session."
      echo "Re-run once it has ended (or with --force)."
      exit 9 ;;
  esac
fi

TS=$(date -u +%Y%m%dT%H%M%SZ)
BAK="$SRC.bak-$TS"
sudo cp -p "$SRC" "$BAK" || { echo "backup FAILED - live file untouched"; exit 4; }
echo "backup: $BAK"
INODE_BEFORE=$(sudo stat -c %i "$SRC")

# (b) work on a blank-line-free copy. Everything the sanity check compares
# against is read from the LIVE file, never from this copy: a copy that died
# half-way (OOM on this 1 GB box, a full /tmp) must not be able to vouch for
# itself, or `cat tmp > live` would wipe every other account.
WORK=$(mktemp -d)
trap 'sudo rm -rf "$WORK"' EXIT
sudo sh -c "sed '/^[[:space:]]*\$/d' '$SRC' > '$WORK/passwd'" || {
  echo "work copy FAILED - live file untouched"; exit 4; }
LIVE_LINES=$(sudo grep -cv '^[[:space:]]*$' "$SRC")
COPY_LINES=$(sudo grep -c '' "$WORK/passwd")
if ! [[ $LIVE_LINES =~ ^[0-9]+$ ]] || [ "$COPY_LINES" != "$LIVE_LINES" ]; then
  echo "work copy incomplete (${COPY_LINES:-?} of ${LIVE_LINES:-?} lines) - live file untouched"; exit 4
fi
OLD_OTHERS=$(sudo grep -v -e '^[[:space:]]*$' -e "^$GW:" "$SRC")
OLD_GW_LINE=$(sudo grep "^$GW:" "$SRC" | sha256sum)
# -b adds the user if absent, replaces the hash if present.
printf '%s\n' "$PW" | sudo docker run -i --rm -v "$WORK":/work "$IMG" \
  sh -c 'read -r PW && mosquitto_passwd -b /work/passwd "$1" "$PW"' _ "$GW" \
  || { echo "mosquitto_passwd FAILED - live file untouched"; exit 5; }

# Sanity before touching the live file: every OTHER account's line is exactly
# as in the live file (byte for byte, same order), $GW has exactly one line,
# and that line really changed (a fresh salt guarantees it even for an
# unchanged password).
NEW_OTHERS=$(sudo grep -v "^$GW:" "$WORK/passwd")
NEW_GW_COUNT=$(sudo grep -c "^$GW:" "$WORK/passwd")
if [ "$NEW_OTHERS" != "$OLD_OTHERS" ]; then
  echo "sanity check FAILED: the other accounts differ from the live file - live file untouched"; exit 7
fi
if [ "$NEW_GW_COUNT" != 1 ] \
   || [ "$(sudo grep "^$GW:" "$WORK/passwd" | sha256sum)" = "$OLD_GW_LINE" ]; then
  echo "sanity check FAILED: $GW has ${NEW_GW_COUNT:-?} line(s), or its hash did not change - live file untouched"; exit 7
fi

# (c) write back IN PLACE.
sudo sh -c "cat '$WORK/passwd' > '$SRC'" || {
  echo "write-back FAILED - restore with: sudo sh -c \"cat '$BAK' > '$SRC'\""; exit 8; }
INODE_AFTER=$(sudo stat -c %i "$SRC")
echo "after:  $(sudo stat -c 'inode=%i owner=%u:%g mode=%a size=%s' "$SRC")"
echo "users: $(sudo cut -d: -f1 "$SRC" | tr '\n' ' ')"

if [ "$INODE_BEFORE" = "$INODE_AFTER" ]; then
  sudo docker kill -s HUP "$C" >/dev/null || {
    echo "HUP FAILED - the broker still has the old file loaded (sudo docker kill -s HUP $C)"; exit 6; }
  echo "same inode -> HUP sent to $C"
else
  echo "inode changed -> restarting $C so the single-file bind mount sees the new file"
  sudo docker restart "$C" >/dev/null || { echo "restart of $C FAILED"; exit 6; }
  echo "restarted $C"
fi
sleep 3

# (d) verify.
echo "verify: subscribe as $GW to amphive/gateways/$GW/config"
OUT=$(printf '%s\n' "$PW" | sudo docker run -i --rm --network "container:$C" "$IMG" \
  sh -c 'read -r PW && mosquitto_sub -h 127.0.0.1 -p 1883 -u "$1" -P "$PW" -t "amphive/gateways/$1/config" -C 1 -W 8 -v' _ "$GW" 2>&1)
RC=$?
if [ "$RC" -eq 0 ] && printf '%s\n' "$OUT" | grep -q "^amphive/gateways/$GW/config "; then
  echo "$OUT"
  echo "VERIFY OK"
elif [ "$RC" -eq 27 ]; then
  # 27 = MOSQ_ERR_TIMEOUT. mosquitto_sub's -W clock only starts once the
  # broker has ACCEPTED the connection, so the credentials work; there is
  # just no retained roster yet. Normal for a new gateway: the backend
  # publishes it on plug create/edit/delete and when the gateway connects.
  echo "VERIFY OK (authenticated as $GW; no retained roster on amphive/gateways/$GW/config yet)"
else
  echo "$OUT"
  echo "VERIFY FAILED (mosquitto_sub exit $RC; 4/5 = credentials rejected) - the live file WAS updated; backup: $BAK"
  exit 6
fi
