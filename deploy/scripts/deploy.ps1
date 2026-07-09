# =============================================================================
# AmpHive Production Deployment Script
# Deploys the backend, frontend, MQTT config, and docker-compose to the GCP VM.
#
# Database now runs as a Docker container on the VM itself (postgres:15-alpine).
# No Cloud SQL dependency - DATABASE_URL points to the 'db' service on the
# internal Docker Compose network, which resolves within the container stack.
#
# Usage:   .\deploy\scripts\deploy.ps1
# Target:  amphive-vm-in (asia-south1-a)
# =============================================================================

$PROJECT_ROOT = (Resolve-Path "$PSScriptRoot\..\..").Path
$VM_NAME      = "amphive-vm-in"
$VM_ZONE      = "asia-south1-a"
$REMOTE_DIR   = "/home/Sarthak/amphive"

# ---- Step 1: Validate .env and set DATABASE_URL to the 'db' container ----
# The hostname 'db' resolves inside the Docker Compose network automatically.
# No Cloud SQL required - the Postgres container runs on the VM itself.
Write-Host "`n[1/4] Validating .env and setting DATABASE_URL..." -ForegroundColor Cyan

$env_file = "$PROJECT_ROOT\.env"

# The deploy refuses to run without a real .env. It must NEVER fabricate one:
# the old behavior wrote the publicly-known dev JWT secret into production.
if (-not (Test-Path $env_file)) {
    Write-Host "ERROR: $env_file not found." -ForegroundColor Red
    Write-Host "Create it from deploy/config/.env.template and set real secrets first." -ForegroundColor Red
    Write-Host 'Generate a JWT secret with:  python -c "import secrets; print(secrets.token_urlsafe(64))"' -ForegroundColor Yellow
    exit 1
}

$content = Get-Content $env_file

# --- Gate: JWT_SECRET_KEY must exist and not be a known-insecure default ---
$jwt_line = $content | Where-Object { $_ -like "JWT_SECRET_KEY=*" } | Select-Object -First 1
if ($jwt_line) {
    $jwt_value = $jwt_line.Substring("JWT_SECRET_KEY=".Length).Trim()
} else {
    $jwt_value = ""
}
$insecure_jwt_values = @("", "amphive-dev-secret-change-in-production", "change-me-in-production")
if (($insecure_jwt_values -contains $jwt_value) -or ($jwt_value.Length -lt 32)) {
    Write-Host "ERROR: JWT_SECRET_KEY in .env is missing, a known default, or too short (<32 chars)." -ForegroundColor Red
    Write-Host "Refusing to deploy: with a guessable key every login token is forgeable." -ForegroundColor Red
    Write-Host 'Generate one with:  python -c "import secrets; print(secrets.token_urlsafe(64))"' -ForegroundColor Yellow
    exit 1
}

# --- DB password: read POSTGRES_PASSWORD from .env (compose interpolates it) ---
$pg_line = $content | Where-Object { $_ -like "POSTGRES_PASSWORD=*" } | Select-Object -First 1
if ($pg_line) {
    $pg_password = $pg_line.Substring("POSTGRES_PASSWORD=".Length).Trim()
} else {
    $pg_password = ""
}
$insecure_pg_values = @("", "amphive_db_admin", "set-a-strong-db-password")
if ($insecure_pg_values -contains $pg_password) {
    Write-Host "ERROR: POSTGRES_PASSWORD in .env is missing, too weak, or using the legacy compromised default." -ForegroundColor Red
    Write-Host "Refusing to deploy: set a strong POSTGRES_PASSWORD in your .env first." -ForegroundColor Red
    exit 1
}

# --- MQTT bind IP: which host IP the broker's public port binds to ---
# 0.0.0.0 preserves the old (internet-exposed) behavior; switch this to the
# VM's overlay/Tailscale IP (e.g. 100.87.241.70) so only overlay peers reach
# MQTT. The ESP32 connects over the overlay, so nothing else needs 1883.
$has_mqtt_bind = $false
foreach ($line in $content) {
    if ($line -like "MQTT_BIND_IP=*") {
        $has_mqtt_bind = $true
    }
}
if (-not $has_mqtt_bind) {
    $content += "MQTT_BIND_IP=0.0.0.0"
    Write-Host "  WARNING: MQTT_BIND_IP missing from .env - defaulted to 0.0.0.0 (public)." -ForegroundColor Yellow
    Write-Host "  Set MQTT_BIND_IP=<VM overlay IP> to take the broker off the public internet." -ForegroundColor Yellow
}

# --- MQTT credentials: hashed into the broker's passwd file in Step 3 ---
# The broker now mounts the passwd file, so a deploy without these would ship
# a broker that can't start. Alphanumeric/-/_ only: the values pass through a
# remote shell and compose interpolation unquoted.
$mqtt_vars = @{}
foreach ($name in @("MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_GW_USERNAME", "MQTT_GW_PASSWORD")) {
    $line = $content | Where-Object { $_ -like "$name=*" } | Select-Object -First 1
    $value = if ($line) { $line.Substring("$name=".Length).Trim() } else { "" }
    if ($value -eq "" -or $value -like "set-a-strong-*") {
        Write-Host "ERROR: $name in .env is missing or still the template placeholder." -ForegroundColor Red
        Write-Host 'Generate one with:  python -c "import secrets; print(secrets.token_urlsafe(24))"' -ForegroundColor Yellow
        exit 1
    }
    if ($value -notmatch '^[A-Za-z0-9_-]+$') {
        Write-Host "ERROR: $name may only contain letters, digits, '-' and '_' (it is shell-interpolated)." -ForegroundColor Red
        exit 1
    }
    $mqtt_vars[$name] = $value
}

$db_url = "postgresql+asyncpg://postgres:$pg_password@db:5432/amphive"
$new_content = @()
$has_db_url = $false
foreach ($line in $content) {
    if ($line -like "DATABASE_URL=*") {
        $new_content += "DATABASE_URL=$db_url"
        $has_db_url = $true
    } else {
        $new_content += $line
    }
}
if (-not $has_db_url) { $new_content += "DATABASE_URL=$db_url" }
Set-Content -Path $env_file -Value $new_content
Write-Host "  -> .env validated; DATABASE_URL points at the 'db' container" -ForegroundColor Green

# Ensure the remote directory exists on the VM
Write-Host "`nEnsuring remote directory $REMOTE_DIR exists on VM..." -ForegroundColor Cyan
gcloud compute ssh $VM_NAME --zone=$VM_ZONE --command="mkdir -p $REMOTE_DIR"

# ---- Step 2: Compress and copy application code to VM ----
Write-Host "`n[2/4] Compressing and copying files to GCP VM ($VM_NAME)..." -ForegroundColor Cyan

if (Test-Path "$PROJECT_ROOT\amphive_app.tar.gz") {
    Remove-Item "$PROJECT_ROOT\amphive_app.tar.gz"
}

Push-Location $PROJECT_ROOT
tar -czf amphive_app.tar.gz --exclude node_modules --exclude .venv --exclude .git backend frontend
Pop-Location

gcloud compute scp --quiet "$PROJECT_ROOT\amphive_app.tar.gz" "${VM_NAME}:${REMOTE_DIR}/amphive_app.tar.gz" --zone=$VM_ZONE
Remove-Item "$PROJECT_ROOT\amphive_app.tar.gz"

# ---- Step 3: Copy deployment configs ----
Write-Host "`n[3/4] Copying deployment configs..." -ForegroundColor Cyan

# Mosquitto config - upload to /tmp first then sudo-move to avoid permission errors on VM
gcloud compute scp --quiet "$PROJECT_ROOT\deploy\config\mosquitto.conf" "${VM_NAME}:/tmp/mosquitto.conf" --zone=$VM_ZONE
gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command="sudo mv /tmp/mosquitto.conf $REMOTE_DIR/mosquitto.conf"

# MQTT TLS certs (broker 8883 listener). The mosquitto.conf mounts these, so
# a deploy without them ships a broker that fails to start the TLS listener.
$certDir = "$PROJECT_ROOT\deploy\config\mqtt-certs"
foreach ($f in @("ca.crt", "server.crt", "server.key")) {
    if (-not (Test-Path "$certDir\$f")) {
        Write-Host "ERROR: $certDir\$f not found." -ForegroundColor Red
        Write-Host "Generate the broker TLS material first:" -ForegroundColor Yellow
        Write-Host "  MQTT_TLS_SAN_IP=$MQTT_BIND_IP bash deploy/config/gen_mqtt_certs.sh" -ForegroundColor Yellow
        exit 1
    }
}
Write-Host "  -> Transferring MQTT TLS certs..." -ForegroundColor Cyan
gcloud compute scp --quiet "$certDir\ca.crt" "$certDir\server.crt" "$certDir\server.key" "${VM_NAME}:/tmp/" --zone=$VM_ZONE
# server.key must be readable by the mosquitto user (uid 1883) and no one else.
$certs_cmd = "sudo mkdir -p $REMOTE_DIR/mqtt-certs && sudo mv /tmp/ca.crt /tmp/server.crt /tmp/server.key $REMOTE_DIR/mqtt-certs/ && sudo chown -R 1883:1883 $REMOTE_DIR/mqtt-certs && sudo chmod 644 $REMOTE_DIR/mqtt-certs/ca.crt $REMOTE_DIR/mqtt-certs/server.crt && sudo chmod 600 $REMOTE_DIR/mqtt-certs/server.key"
gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command=$certs_cmd

gcloud compute scp --quiet "$PROJECT_ROOT\deploy\docker\docker-compose.prod.yml" "${VM_NAME}:${REMOTE_DIR}/docker-compose.yml" --zone=$VM_ZONE
gcloud compute scp --quiet "$PROJECT_ROOT\.env" "${VM_NAME}:${REMOTE_DIR}/.env" --zone=$VM_ZONE

# Mosquitto passwd file: hash the .env credentials with the mosquitto image's
# own mosquitto_passwd (values are validated alphanumeric above, so plain
# interpolation into the remote command is safe). Only created with -c when the
# file doesn't exist yet — per-gateway users (add_gateway_user.ps1) must
# survive redeploys; -b updates the managed entries in place either way.
Write-Host "  -> Generating mosquitto passwd file on the VM..." -ForegroundColor Cyan
$mq_u  = $mqtt_vars["MQTT_USERNAME"];    $mq_p  = $mqtt_vars["MQTT_PASSWORD"]
$mqg_u = $mqtt_vars["MQTT_GW_USERNAME"]; $mqg_p = $mqtt_vars["MQTT_GW_PASSWORD"]
# chown 1883 (the image's mosquitto user) + 600: the broker reads the file
# after dropping privileges on reload, so root-owned would break SIGHUP.
$passwd_cmd = "sudo docker run --rm -v ${REMOTE_DIR}:/work eclipse-mosquitto:2.0 sh -c '[ -f /work/mosquitto_passwd ] || mosquitto_passwd -c -b /work/mosquitto_passwd $mq_u $mq_p; mosquitto_passwd -b /work/mosquitto_passwd $mq_u $mq_p && mosquitto_passwd -b /work/mosquitto_passwd $mqg_u $mqg_p && chown 1883:1883 /work/mosquitto_passwd && chmod 600 /work/mosquitto_passwd'"
gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command=$passwd_cmd

# Mosquitto ACL file: topic authorization, required now that 8883 is public.
# Backend gets the full namespace (+ $SYS read for the compose healthcheck);
# the legacy shared gateway account keeps the whole gateways subtree during the
# transition; per-gateway accounts (username == gateway_id, created by
# deploy/scripts/add_gateway_user.ps1) are scoped to their own subtree by the
# %u pattern. Anything not granted is denied.
Write-Host "  -> Generating mosquitto ACL file on the VM..." -ForegroundColor Cyan
$acl_cmd = "printf 'user $mq_u\ntopic readwrite amphive/#\ntopic read `$SYS/#\n\nuser $mqg_u\ntopic readwrite amphive/gateways/#\n\npattern readwrite amphive/gateways/%%u/#\n' | sudo tee $REMOTE_DIR/mosquitto_acl > /dev/null && sudo chown 1883:1883 $REMOTE_DIR/mosquitto_acl && sudo chmod 600 $REMOTE_DIR/mosquitto_acl"
gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command=$acl_cmd

# ---- Step 4: Rebuild and restart containers ----
Write-Host "`n[4/4] Extracting application and restarting Docker Compose on VM..." -ForegroundColor Cyan
gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command="cd $REMOTE_DIR && tar -xzf amphive_app.tar.gz && rm amphive_app.tar.gz && sudo docker-compose up -d --build"

$vm_ip = (gcloud compute instances describe $VM_NAME --zone=$VM_ZONE --format="value(networkInterfaces[0].accessConfigs[0].natIP)").Trim()
Write-Host "`nDeployment complete!" -ForegroundColor Green
Write-Host "  Frontend : http://$vm_ip" -ForegroundColor Yellow
Write-Host "  Backend  : http://${vm_ip}:8000/docs" -ForegroundColor Yellow
