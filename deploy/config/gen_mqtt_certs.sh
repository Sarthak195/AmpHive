#!/usr/bin/env bash
# =============================================================================
# Generate the MQTT broker TLS material: a self-signed CA and a server cert.
#
# Run ONCE (or when the broker IP/SAN changes). Produces, under
# deploy/config/mqtt-certs/:
#   ca.crt      self-signed CA cert   — PUBLIC (tracked); firmware embeds it,
#                                       mosquitto uses it as `cafile`
#   ca.key      CA private key        — SECRET (gitignored); re-sign only
#   server.crt  broker cert           — PUBLIC (tracked); mosquitto `certfile`
#   server.key  broker private key    — SECRET (gitignored); mosquitto `keyfile`
# and copies ca.crt to firmware/main/certs/mqtt_ca.crt for the firmware build.
#
# The firmware validates the broker cert against this CA (chain + IP SAN);
# it does NOT check cert dates (CONFIG_MBEDTLS_HAVE_TIME_DATE is off, no clock
# needed), so validity is set long (10y) mainly for any date-checking client.
#
# Usage:  MQTT_TLS_SAN_IP=100.87.241.70 bash deploy/config/gen_mqtt_certs.sh
#
# Re-issuing the SERVER cert only (e.g. the broker gained a public IP): keep
# the CA (it is embedded in deployed firmware), regenerate server.key/crt:
#   RESIGN_SERVER=1 MQTT_TLS_SAN_IPS=100.87.241.70,8.231.81.12 \
#     bash deploy/config/gen_mqtt_certs.sh
# =============================================================================
set -euo pipefail

# Git-Bash/MSYS rewrites leading-slash args (openssl's -subj "/CN=...") into
# Windows paths. Disable that so the DN passes through verbatim. We therefore
# run openssl from inside the output dir with RELATIVE filenames (which need
# no conversion) — absolute /c/ paths would break the native openssl.exe.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

# One or more SAN IPs, comma-separated (every address a client may dial:
# overlay IP for transition clients, public IP for direct-MQTT clients).
SAN_IPS="${MQTT_TLS_SAN_IPS:-${MQTT_TLS_SAN_IP:-100.87.241.70}}"
RESIGN="${RESIGN_SERVER:-0}"
DAYS=3650

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SCRIPT_DIR/mqtt-certs"
FW_CERT_DIR="$SCRIPT_DIR/../../firmware/main/certs"
mkdir -p "$OUT" "$FW_CERT_DIR"

if [[ "$RESIGN" == "1" ]]; then
  # Re-issue the server cert under the EXISTING CA (deployed firmware embeds it).
  if [[ ! -f "$OUT/ca.key" || ! -f "$OUT/ca.crt" ]]; then
    echo "RESIGN_SERVER=1 needs the existing ca.key/ca.crt in $OUT." >&2
    exit 1
  fi
elif [[ -f "$OUT/ca.key" || -f "$OUT/server.key" ]]; then
  echo "Refusing to overwrite existing keys in $OUT (delete them first to regenerate," >&2
  echo "or use RESIGN_SERVER=1 to re-issue only the server cert under the same CA)." >&2
  exit 1
fi

cd "$OUT"

if [[ "$RESIGN" != "1" ]]; then
  echo "Generating CA..."
  openssl genrsa -out ca.key 4096
  # Explicit CA extensions: strict validators (e.g. Python 3.13's default
  # VERIFY_X509_STRICT — the AmpHive Agent) reject a CA without keyUsage.
  openssl req -x509 -new -nodes -key ca.key -sha256 -days "$DAYS" \
    -subj "/CN=AmpHive-MQTT-CA/O=AmpHive" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out ca.crt
fi

echo "Generating server cert for SAN IP(s) $SAN_IPS..."
openssl genrsa -out server.key 2048
openssl req -new -key server.key -subj "/CN=amphive-mqtt/O=AmpHive" -out server.csr

# SAN must carry the exact address the gateway dials (mbedTLS checks it).
SAN_LIST=""
IFS=',' read -ra ADDRS <<< "$SAN_IPS"
for ip in "${ADDRS[@]}"; do
  SAN_LIST+="IP:${ip// /}, "
done
printf 'subjectAltName = %sDNS:mqtt, DNS:localhost\nextendedKeyUsage = serverAuth\n' "$SAN_LIST" > server.ext

openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key \
  -CAcreateserial -days "$DAYS" -sha256 -extfile server.ext -out server.crt

rm -f server.csr server.ext ca.srl

cp ca.crt "$FW_CERT_DIR/mqtt_ca.crt"

echo "Verifying chain..."
openssl verify -CAfile ca.crt server.crt

echo
echo "Done. Public certs (ca.crt, server.crt) are tracked; keys are gitignored."
echo "  CA embedded for firmware at: firmware/main/certs/mqtt_ca.crt"
echo "  deploy.ps1 transfers server.crt + server.key to the VM."
