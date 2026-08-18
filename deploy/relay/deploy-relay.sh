#!/usr/bin/env bash
# =============================================================================
# AmpHive Relay Host Bootstrap
#
# Runs ON amphive-relay (GCP e2-micro, us-west1, 1 GB RAM, free tier) — NOT on
# the dev box, NOT on amphive-vm-in. Idempotent: safe to re-run after a
# partial failure or to pick up a refreshed docker-compose.relay.yml / .env.
#
# Typical invocation from the dev box (copy both files up together — this
# script looks for docker-compose.relay.yml next to itself):
#   gcloud compute scp --recurse deploy/relay amphive-relay:~/deploy-relay --zone=<zone>
#   gcloud compute ssh amphive-relay --zone=<zone> \
#     --command="bash ~/deploy-relay/deploy-relay.sh"
#
# See deploy/docs/relay_consolidation_runbook.md for the full cutover plan
# (backup, restore, DNS repoint, decommission) — this script only covers
# "get the relay-sized stack running on this box"; it does the OS prep (swap,
# Docker) and container lifecycle. It does NOT sync application source or
# restore data — see the OPERATOR ACTION REQUIRED blocks below for exactly
# what you still have to hand it.
#
# What this script does, in order:
#   1. Creates a 2 GB swapfile if none exists (e2-micro ships with none; 1 GB
#      RAM is not enough headroom for 5 containers + a `--build` step without
#      one — see the compose file's header for the RAM budget math).
#   2. Installs Docker + the `docker compose` (v2) plugin, if missing.
#   3. Stops + disables the box's existing nginx TCP relay (frees :80/:8883 —
#      once the local Mosquitto container here is reachable on :8883, nginx's
#      stream{} TCP-proxy role is fully superseded, per the runbook).
#   4. Stages docker-compose.relay.yml into $WORKDIR and validates every
#      operator-supplied prerequisite is present (.env, mosquitto.conf,
#      mqtt-certs/, backend/ + frontend/ source) — fails fast with exact scp
#      commands if anything is missing, rather than limping into a broken
#      `docker compose up`.
#   5. Generates mosquitto_passwd (backend account only, first run) and
#      mosquitto_acl (always, cheap to regenerate) and a starter Caddyfile
#      (first run only) if they don't already exist.
#   6. Runs `docker compose -f docker-compose.relay.yml up -d --build`.
#   7. Polls http://localhost:8000/api/health until it answers (or times out).
# =============================================================================
set -euo pipefail

WORKDIR="${AMPHIVE_RELAY_DIR:-$HOME/amphive}"
ZONE_HINT="us-west1-<zone>"   # cosmetic only — used in printed scp examples

log()  { printf '\n[deploy-relay] %s\n' "$1"; }
fail() { printf '\n[deploy-relay] ERROR: %s\n' "$1" >&2; exit 1; }

# -----------------------------------------------------------------------------
# 1. Swapfile (idempotent: skip if /swapfile exists OR any swap is already on)
# -----------------------------------------------------------------------------
log "Checking swap..."
if [ -f /swapfile ] || [ "$(swapon --show 2>/dev/null | wc -l)" -gt 0 ]; then
  log "Swap already present — skipping."
else
  log "No swap found. Creating a 2G swapfile (e2-micro ships with none; 1 GB"
  log "physical RAM is not enough for 5 containers + a build burst without it)."
  sudo fallocate -l 2G /swapfile 2>/dev/null || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab 2>/dev/null || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  # Prefer swap only under real pressure, not proactively (default is 60).
  sudo sysctl -w vm.swappiness=10 >/dev/null || true
  log "Swapfile active: $(swapon --show | tail -n1)"
fi

# -----------------------------------------------------------------------------
# 2. Docker + compose plugin (idempotent: skip install if both already work)
# -----------------------------------------------------------------------------
log "Checking Docker..."
if command -v docker >/dev/null 2>&1 && sudo docker compose version >/dev/null 2>&1; then
  log "Docker + compose plugin already installed: $(docker --version), $(sudo docker compose version --short 2>/dev/null || echo present)"
else
  log "Installing Docker (official convenience script installs docker-ce +"
  log "the compose plugin together; safe to re-run)..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo systemctl enable --now docker
  # Lets you drop 'sudo' after a fresh login; every command below still uses
  # sudo explicitly so this script works in the same shell that installed it.
  sudo usermod -aG docker "$(whoami)" 2>/dev/null || true
  command -v curl >/dev/null 2>&1 || { sudo apt-get update -y && sudo apt-get install -y curl; }
  sudo docker compose version >/dev/null 2>&1 || fail "Docker installed but 'docker compose' plugin still missing — check get.docker.com output above."
  log "Installed: $(docker --version)"
fi

# -----------------------------------------------------------------------------
# 3. Stop the existing nginx TCP relay (frees :80 and :8883 for our stack)
# -----------------------------------------------------------------------------
log "Checking for an existing nginx TCP relay..."
if systemctl is-active --quiet nginx 2>/dev/null; then
  log "nginx is active — stopping + disabling it (its stream{} TCP-proxy role"
  log "for MQTT is fully superseded once the local mosquitto container below"
  log "is reachable on :8883 — see relay_consolidation_runbook.md ground rules)."
  sudo systemctl stop nginx
  sudo systemctl disable nginx
else
  log "nginx not active — nothing to stop (already stopped, or never installed)."
fi

# -----------------------------------------------------------------------------
# 4. Stage the compose file + validate operator-supplied prerequisites
# -----------------------------------------------------------------------------
mkdir -p "$WORKDIR"
log "Working directory: $WORKDIR"

SCRIPT_SRC="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT_SRC" ] && [ -f "$SCRIPT_SRC" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SRC")" && pwd)"
else
  SCRIPT_DIR=""
fi

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/docker-compose.relay.yml" ]; then
  cp "$SCRIPT_DIR/docker-compose.relay.yml" "$WORKDIR/docker-compose.relay.yml"
  log "Staged docker-compose.relay.yml into $WORKDIR."
elif [ -f "$WORKDIR/docker-compose.relay.yml" ]; then
  log "docker-compose.relay.yml already present in $WORKDIR (script run without a"
  log "sibling copy, e.g. piped via 'bash -s <'); using the existing one as-is."
else
  fail "docker-compose.relay.yml not found next to this script, and not already
in $WORKDIR. Copy both deploy/relay files up together, e.g.:
  gcloud compute scp --recurse deploy/relay amphive-relay:~/deploy-relay --zone=$ZONE_HINT
  gcloud compute ssh amphive-relay --zone=$ZONE_HINT --command=\"bash ~/deploy-relay/deploy-relay.sh\""
fi

env_val() {   # env_val NAME FILE -> prints the value (empty if absent)
  grep -m1 "^$1=" "$2" 2>/dev/null | cut -d '=' -f2- | tr -d '\r'
}

# ---- OPERATOR ACTION REQUIRED: .env ----------------------------------------
# This script never fabricates one — a missing/placeholder secret in
# production is worse than refusing to start (same philosophy as
# deploy/scripts/deploy.ps1 for amphive-vm-in).
if [ ! -f "$WORKDIR/.env" ]; then
  fail ".env not found in $WORKDIR.
Copy deploy/config/.env.template, fill in NEW strong secrets (do not reuse
amphive-vm-in's values verbatim — JWT_SECRET_KEY / POSTGRES_PASSWORD /
MQTT_PASSWORD should all be freshly generated; CADDY_DOMAIN/RAZORPAY_* are the
values worth carrying over deliberately, per the runbook), then:
  gcloud compute scp .env amphive-relay:$WORKDIR/.env --zone=$ZONE_HINT"
fi

jwt_val="$(env_val JWT_SECRET_KEY "$WORKDIR/.env")"
if [ -z "$jwt_val" ] || [ "$jwt_val" = "change-me-in-production" ] || [ "${#jwt_val}" -lt 32 ]; then
  fail "JWT_SECRET_KEY in $WORKDIR/.env is missing, the template placeholder, or <32 chars.
Generate one with:  python3 -c \"import secrets; print(secrets.token_urlsafe(64))\""
fi

pg_val="$(env_val POSTGRES_PASSWORD "$WORKDIR/.env")"
case "$pg_val" in
  ""|"set-a-strong-db-password"|"amphive_db_admin")
    fail "POSTGRES_PASSWORD in $WORKDIR/.env is missing or a known placeholder." ;;
esac

mqtt_user="$(env_val MQTT_USERNAME "$WORKDIR/.env")"
mqtt_pw="$(env_val MQTT_PASSWORD "$WORKDIR/.env")"
case "$mqtt_pw" in
  ""|"set-a-strong-mqtt-password")
    fail "MQTT_PASSWORD in $WORKDIR/.env is missing or a known placeholder." ;;
esac
[ -n "$mqtt_user" ] || fail "MQTT_USERNAME in $WORKDIR/.env is empty."

log ".env present and passes the basic placeholder/strength gate."

# ---- mosquitto.conf: reuse the repo's config if it's sitting alongside this
# script (deploy/config/mosquitto.conf); otherwise it must already be staged.
if [ ! -f "$WORKDIR/mosquitto.conf" ]; then
  if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../config/mosquitto.conf" ]; then
    cp "$SCRIPT_DIR/../config/mosquitto.conf" "$WORKDIR/mosquitto.conf"
    log "Copied mosquitto.conf from deploy/config/."
  else
    fail "mosquitto.conf not found in $WORKDIR (and deploy/config/mosquitto.conf
isn't alongside this script either). Copy it up manually:
  gcloud compute scp deploy/config/mosquitto.conf amphive-relay:$WORKDIR/mosquitto.conf --zone=$ZONE_HINT"
  fi
fi

# ---- OPERATOR ACTION REQUIRED: MQTT TLS certs ------------------------------
# Never generated here — the CA must never be regenerated (it's baked into
# every fielded gateway's firmware); only the server cert gets re-issued with
# an extra SAN for this box's IP. See relay_consolidation_runbook.md §4a.
mkdir -p "$WORKDIR/mqtt-certs"
for f in ca.crt server.crt server.key; do
  if [ ! -f "$WORKDIR/mqtt-certs/$f" ]; then
    fail "$WORKDIR/mqtt-certs/$f not found.
Reissue the server cert with this relay's IP as an added SAN (keeping the
existing CA — never regenerate it):
  RESIGN_SERVER=1 MQTT_TLS_SAN_IPS=<existing-ips>,<this-relay-ip> \\
    MQTT_TLS_SAN_DNS=mqtt.amphive.app bash deploy/config/gen_mqtt_certs.sh
Then copy the three files up:
  gcloud compute scp deploy/config/mqtt-certs/ca.crt deploy/config/mqtt-certs/server.crt deploy/config/mqtt-certs/server.key amphive-relay:$WORKDIR/mqtt-certs/ --zone=$ZONE_HINT"
  fi
done
sudo chown -R 1883:1883 "$WORKDIR/mqtt-certs" 2>/dev/null || true
sudo chmod 644 "$WORKDIR/mqtt-certs/ca.crt" "$WORKDIR/mqtt-certs/server.crt" 2>/dev/null || true
sudo chmod 600 "$WORKDIR/mqtt-certs/server.key" 2>/dev/null || true

# ---- OPERATOR ACTION REQUIRED: application source --------------------------
# This script does not sync app code — only host prep + container lifecycle.
for d in backend frontend; do
  if [ ! -d "$WORKDIR/$d" ]; then
    fail "$WORKDIR/$d not found — application source must be synced separately.
  gcloud compute scp --recurse backend frontend amphive-relay:$WORKDIR/ --zone=$ZONE_HINT
(or restore it however this relay's checkout is normally kept up to date)."
  fi
done

log "All operator-supplied prerequisites found."

# -----------------------------------------------------------------------------
# 5. Generate broker identity files (passwd/acl) + a starter Caddyfile
# -----------------------------------------------------------------------------
if [ ! -f "$WORKDIR/mosquitto_passwd" ]; then
  log "Generating mosquitto_passwd (backend account only — per-gateway accounts"
  log "are restored from the prod config tarball or added later via"
  log "add_gateway_user.ps1; this file is never truncated once it exists)."
  sudo docker run --rm -v "$WORKDIR":/work eclipse-mosquitto:2.0 \
    sh -c "mosquitto_passwd -c -b /work/mosquitto_passwd '$mqtt_user' '$mqtt_pw'"
else
  log "mosquitto_passwd already exists — leaving per-gateway entries intact."
fi
sudo chown 1883:1883 "$WORKDIR/mosquitto_passwd"
sudo chmod 600 "$WORKDIR/mosquitto_passwd"

# ACL is derived purely from MQTT_USERNAME — safe to regenerate every run.
log "Writing mosquitto_acl..."
printf 'user %s\ntopic readwrite amphive/#\ntopic read $SYS/#\n\npattern readwrite amphive/gateways/%%u/#\n' "$mqtt_user" \
  | sudo tee "$WORKDIR/mosquitto_acl" >/dev/null
sudo chown 1883:1883 "$WORKDIR/mosquitto_acl"
sudo chmod 600 "$WORKDIR/mosquitto_acl"

# ============================================================================
# IMPORTANT — READ BEFORE ASSUMING THIS SHIPPED ANYTHING
#
# The block below only runs on the FIRST deploy, when no Caddyfile exists yet.
# An already-deployed relay keeps its hand-edited Caddyfile untouched, forever.
# That means EVERY edit made here — security headers, Permissions-Policy, the
# http:// redirect/serve split, trusted_proxies — reaches a FRESH relay only.
# On the live relay it changes NOTHING until an operator applies the same edits
# by hand to $WORKDIR/Caddyfile and reloads Caddy:
#
#   gcloud compute ssh amphive-relay --zone=<zone>
#   sudo nano ~/amphive/Caddyfile          # apply the same edits
#   sudo docker compose -f ~/amphive/docker-compose.relay.yml exec caddy \
#       caddy validate --config /etc/caddy/Caddyfile
#   sudo docker compose -f ~/amphive/docker-compose.relay.yml exec caddy \
#       caddy reload --config /etc/caddy/Caddyfile
#
# Do NOT "fix" this by deleting the live Caddyfile so it regenerates: the live
# one carries hand edits, and Caddy would restart ACME from a cold state.
# deploy/config/Caddyfile.example is the reference for what it should contain.
# ============================================================================
if [ ! -f "$WORKDIR/Caddyfile" ]; then
  log "Generating a starter Caddyfile from .env (CADDY_DOMAIN/CADDY_CPO_DOMAIN/"
  log "ACME_EMAIL) — edit in place afterwards; re-running this script will not"
  log "overwrite it once it exists."
  domain="$(env_val CADDY_DOMAIN "$WORKDIR/.env")"; domain="${domain:-amphive.app}"
  cpo_domain="$(env_val CADDY_CPO_DOMAIN "$WORKDIR/.env")"
  acme_email="$(env_val ACME_EMAIL "$WORKDIR/.env")"
  # Keep these two strings byte-identical with frontend/nginx.conf and
  # deploy/config/Caddyfile.example. object-src 'none' and frame-ancestors
  # 'none' are the modern companions to X-Frame-Options; geolocation is
  # (self) rather than () on purpose — /map calls navigator.geolocation and a
  # blanket deny silently breaks the map. If Razorpay Checkout ever needs the
  # W3C Payment Request API, relax payment=() to
  # payment=(self "https://api.razorpay.com") rather than dropping it.
  csp="default-src 'self'; script-src 'self' https://*.razorpay.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self' https: wss:; frame-src https://*.razorpay.com; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
  perms="geolocation=(self), camera=(), microphone=(), usb=(), payment=(), interest-cohort=()"
  {
    echo "# Generated once by deploy-relay.sh from .env — edits here are preserved"
    echo "# on future runs (only created when missing). Delete to regenerate."
    # Global options: always emitted (not only when an ACME email is set) so the
    # trusted_proxies setting below is present regardless.
    echo "{"
    [ -n "$acme_email" ] && echo "    email $acme_email"
    echo "    servers {"
    echo "        # M3: trust ONLY the compose private network as a proxy hop, so"
    echo "        # a direct internet client cannot spoof X-Forwarded-For to poison"
    echo "        # the backend's per-IP rate limiter (see backend rate_limit.py)."
    echo "        # Caddy is the edge here, so its immediate peer is the real"
    echo "        # client; these ranges make Caddy write a clean XFF = client IP."
    echo "        trusted_proxies static 172.16.0.0/12 192.168.0.0/16 10.0.0.0/8"
    echo "    }"
    echo "}"
    echo
    # Shared security-header block (P3). Caddy `header <field> <value>` SETS
    # (replaces) the field, so these override whatever the frontend nginx sent —
    # exactly one CSP per response.
    hdr() {
      echo "    header {"
      echo "        Strict-Transport-Security \"max-age=31536000; includeSubDomains\""
      echo "        X-Content-Type-Options \"nosniff\""
      echo "        X-Frame-Options \"DENY\""
      echo "        Referrer-Policy \"strict-origin-when-cross-origin\""
      echo "        Permissions-Policy \"$perms\""
      echo "        Content-Security-Policy \"$csp\""
      echo "    }"
    }
    site() {
      echo "$1 {"
      echo "    encode gzip"
      hdr
      echo "    reverse_proxy frontend:80"
      echo "}"
      echo
    }
    # Plain HTTP on a NAMED domain: redirect, never serve. This used to fall
    # through to the catch-all http:// site below, which SERVED the app in
    # cleartext for every Host — including the real domains. The comment that
    # justified that said to tighten once a paid/reliable domain existed;
    # amphive.app went live 2026-07-20, so the justification expired, and a
    # disaster-recovery rebuild would otherwise silently reopen a window where
    # http://<domain>/api/... answers 200 with a bearer JWT in the clear.
    # 308 keeps the method and body of a mis-addressed POST (and matches what
    # Caddy's own automatic HTTP->HTTPS redirect uses). Port 80 must stay open:
    # Caddy answers the ACME HTTP-01 challenge ahead of any site route, so
    # these redirects do not break certificate issuance or renewal.
    redir_site() {
      echo "http://$1 {"
      echo "    redir https://{host}{uri} 308"
      echo "}"
      echo
    }
    site "$domain"
    [ -n "$cpo_domain" ] && site "$cpo_domain"
    redir_site "$domain"
    [ -n "$cpo_domain" ] && redir_site "$cpo_domain"
    echo "# Bare-IP / unknown-Host over plain HTTP: still SERVED, deliberately —"
    echo "# this is the ONE site not converted to a redirect, because:"
    echo "#   1. Let's Encrypt does not issue certificates for bare IPs, so"
    echo "#      redirecting http://<ip>/ to https://<ip>/ produces a hard cert"
    echo "#      interstitial: unreachable, which is worse than cleartext."
    echo "#   2. Redirecting to the named domain instead defeats the point — the"
    echo "#      escape hatch exists for when DNS is broken (DuckDNS, 2026-07-11)."
    echo "#   3. frontend/src/utils/appHost.js treats a bare-IP host as UNSPLIT"
    echo "#      and App.jsx serves the combined driver+console+admin tree there,"
    echo "#      so a DNS outage does not lock operators out of the console."
    echo "# Accepted residual risk: this site is cleartext, /api and /socket.io"
    echo "# included, with a bearer JWT in play. Mitigated only by nothing linking"
    echo "# to it. HSTS learned from the https domain does not cover it (HSTS is"
    echo "# host-scoped and never applies to IP literals). If the DNS-outage risk"
    echo "# is retired, DELETE this block rather than turning it into a redirect."
    echo "# Same security headers as the named sites; HSTS is a harmless no-op"
    echo "# over plain http — browsers ignore it per spec."
    echo "http:// {"
    echo "    encode gzip"
    hdr
    echo "    reverse_proxy frontend:80"
    echo "}"
  } | sudo tee "$WORKDIR/Caddyfile" >/dev/null
else
  log "Caddyfile already present — leaving as-is."
  log "NOTE: security-header / Permissions-Policy / http-redirect / trusted_proxies"
  log "changes in this script DO NOT reach an existing Caddyfile. Edit"
  log "$WORKDIR/Caddyfile by hand to match deploy/config/Caddyfile.example, then"
  log "run 'caddy validate' + 'caddy reload' inside the caddy container. Until"
  log "you do, this relay runs the OLD headers and still SERVES the app over"
  log "plain http on the real domains."
fi

# -----------------------------------------------------------------------------
# 6. Bring the stack up
# -----------------------------------------------------------------------------
log "Running docker compose up (this also builds backend/frontend images —"
log "expect the first run to take a few minutes on an e2-micro)..."
( cd "$WORKDIR" && sudo docker compose -f docker-compose.relay.yml up -d --build )

# -----------------------------------------------------------------------------
# 6a. Install log rotation for the durable mosquitto.log file (TD#28 follow-up)
# -----------------------------------------------------------------------------
# `docker compose` (run from $WORKDIR above, with no COMPOSE_PROJECT_NAME set)
# derives its project name from $WORKDIR's basename — the same derivation the
# `mosquitto_log` named volume's actual on-disk name follows
# ("<project>_mosquitto_log"). Best-effort: the stack is already up at this
# point, so a missing/failed logrotate install must not fail the deploy —
# see deploy/docs/mosquitto_log_rotation.md for the manual fallback command.
log "Installing logrotate for the mosquitto_log volume..."
MOSQUITTO_LOG_VOL="$(basename "$WORKDIR" | tr 'A-Z' 'a-z')_mosquitto_log"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../scripts/setup_mosquitto_logrotate.sh" ]; then
  bash "$SCRIPT_DIR/../scripts/setup_mosquitto_logrotate.sh" "$MOSQUITTO_LOG_VOL" \
    || log "logrotate install failed (non-fatal, see output above) — the stack is still up."
else
  log "setup_mosquitto_logrotate.sh not found alongside this script (only"
  log "deploy/relay/ was staged, not deploy/scripts/) — install it manually:"
  log "  gcloud compute scp deploy/scripts/setup_mosquitto_logrotate.sh amphive-relay:~/ --zone=$ZONE_HINT"
  log "  gcloud compute ssh amphive-relay --zone=$ZONE_HINT --command=\"bash ~/setup_mosquitto_logrotate.sh $MOSQUITTO_LOG_VOL\""
fi

# -----------------------------------------------------------------------------
# 7. Poll backend health
# -----------------------------------------------------------------------------
command -v curl >/dev/null 2>&1 || { sudo apt-get update -y && sudo apt-get install -y curl; }

log "Polling http://localhost:8000/api/health ..."
elapsed=0
timeout=180
until curl -fs http://localhost:8000/api/health >/dev/null 2>&1; do
  if [ "$elapsed" -ge "$timeout" ]; then
    log "Backend did not become healthy within ${timeout}s."
    ( cd "$WORKDIR" && sudo docker compose -f docker-compose.relay.yml ps )
    fail "Timed out waiting for /api/health. Check: sudo docker compose -f $WORKDIR/docker-compose.relay.yml logs backend"
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done

log "Backend healthy after ~${elapsed}s."
( cd "$WORKDIR" && sudo docker compose -f docker-compose.relay.yml ps )
log "Done. Resource check: free -h && sudo docker stats --no-stream"
