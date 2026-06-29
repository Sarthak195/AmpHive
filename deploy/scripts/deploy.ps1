# =============================================================================
# AmpHive Production Deployment Script
# Deploys the backend, frontend, MQTT config, and docker-compose to the GCP VM.
#
# Database now runs as a Docker container on the VM itself (postgres:15-alpine).
# No Cloud SQL dependency — DATABASE_URL points to the 'db' service on the
# internal Docker Compose network, which resolves within the container stack.
#
# Usage:   .\deploy\scripts\deploy.ps1
# Target:  amphive-vm-in (asia-south1-a)
# =============================================================================

$PROJECT_ROOT = (Resolve-Path "$PSScriptRoot\..\..").Path
$VM_NAME      = "amphive-vm-in"
$VM_ZONE      = "asia-south1-a"
$REMOTE_DIR   = "/home/Sarthak/amphive"

# ---- Step 1: Set DATABASE_URL to local Docker Compose 'db' container ----
# The hostname 'db' resolves inside the Docker Compose network automatically.
# No Cloud SQL required — the Postgres container runs on the VM itself.
Write-Host "`n[1/4] Setting DATABASE_URL to local 'db' container..." -ForegroundColor Cyan

$db_url = "postgresql+asyncpg://postgres:amphive_db_admin@db:5432/amphive"
$env_file = "$PROJECT_ROOT\.env"

if (Test-Path $env_file) {
    $content = Get-Content $env_file
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
} else {
    $env_content = @(
        "DATABASE_URL=$db_url",
        "MQTT_BROKER_HOST=mqtt",
        "MQTT_BROKER_PORT=1883",
        "JWT_SECRET_KEY=amphive-dev-secret-change-in-production",
        "COINS_PER_RUPEE=1.0",
        "COINS_PER_KWH=5.0"
    )
    Set-Content -Path $env_file -Value $env_content
}
Write-Host "  -> DATABASE_URL set to: $db_url" -ForegroundColor Green

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

# Mosquitto config — upload to /tmp first then sudo-move to avoid permission errors on VM
gcloud compute scp --quiet "$PROJECT_ROOT\deploy\config\mosquitto.conf" "${VM_NAME}:/tmp/mosquitto.conf" --zone=$VM_ZONE
gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command="sudo mv /tmp/mosquitto.conf $REMOTE_DIR/mosquitto.conf"

gcloud compute scp --quiet "$PROJECT_ROOT\deploy\docker\docker-compose.prod.yml" "${VM_NAME}:${REMOTE_DIR}/docker-compose.yml" --zone=$VM_ZONE
gcloud compute scp --quiet "$PROJECT_ROOT\.env" "${VM_NAME}:${REMOTE_DIR}/.env" --zone=$VM_ZONE

# ---- Step 4: Rebuild and restart containers ----
Write-Host "`n[4/4] Extracting application and restarting Docker Compose on VM..." -ForegroundColor Cyan
gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command="cd $REMOTE_DIR && tar -xzf amphive_app.tar.gz && rm amphive_app.tar.gz && sudo docker-compose up -d --build"

$vm_ip = (gcloud compute instances describe $VM_NAME --zone=$VM_ZONE --format="value(networkInterfaces[0].accessConfigs[0].natIP)").Trim()
Write-Host "`nDeployment complete!" -ForegroundColor Green
Write-Host "  Frontend : http://$vm_ip" -ForegroundColor Yellow
Write-Host "  Backend  : http://${vm_ip}:8000/docs" -ForegroundColor Yellow
