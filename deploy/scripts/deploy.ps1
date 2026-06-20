# =============================================================================
# AmpHive Production Deployment Script
# Deploys the backend, frontend, MQTT config, and docker-compose to the GCP VM.
# 
# Usage:   .\deploy\scripts\deploy.ps1
# Target:  amphive-vm-in (asia-south1-a)
# =============================================================================

$PROJECT_ROOT = (Resolve-Path "$PSScriptRoot\..\..").Path
$VM_NAME      = "amphive-vm-in"
$VM_ZONE      = "asia-south1-a"
$REMOTE_DIR   = "/home/Sarthak/amphive"

# ---- Step 1: Wait for Cloud SQL to become RUNNABLE ----
Write-Host "`n[1/5] Waiting for Cloud SQL instance to become RUNNABLE..." -ForegroundColor Cyan
while ($true) {
    $state = (gcloud sql instances describe amphive-db-in --format="value(state)").Trim()
    if ($state -eq "RUNNABLE") {
        Write-Host "  -> Instance is RUNNABLE!" -ForegroundColor Green
        break
    }
    Write-Host "  -> Current state: $state - retrying in 15s..."
    Start-Sleep -Seconds 15
}

# ---- Step 2: Create the database (idempotent) ----
Write-Host "`n[2/5] Ensuring 'amphive' database exists..." -ForegroundColor Cyan
# In PowerShell, redirecting output & error to null is clean and cross-platform
gcloud sql databases create amphive --instance=amphive-db-in 2>$null

# ---- Step 3: Fetch Cloud SQL IP and generate/update .env ----
Write-Host "`n[3/5] Fetching Cloud SQL IP and updating .env..." -ForegroundColor Cyan
$ip = (gcloud sql instances describe amphive-db-in --format="value(ipAddresses[0].ipAddress)").Trim()
$db_url = "postgresql+asyncpg://postgres:amphive_db_admin@$ip:5432/amphive"

$env_file = "$PROJECT_ROOT\.env"
if (Test-Path $env_file) {
    $content = Get-Content $env_file
    $new_content = @()
    $has_db_url = $false
    $has_mqtt_host = $false
    $has_mqtt_port = $false
    foreach ($line in $content) {
        if ($line -like "DATABASE_URL=*") {
            $new_content += "DATABASE_URL=$db_url"
            $has_db_url = $true
        } elseif ($line -like "MQTT_BROKER_HOST=*") {
            $new_content += "MQTT_BROKER_HOST=mqtt"
            $has_mqtt_host = $true
        } elseif ($line -like "MQTT_BROKER_PORT=*") {
            $new_content += "MQTT_BROKER_PORT=1883"
            $has_mqtt_port = $true
        } else {
            $new_content += $line
        }
    }
    if (-not $has_db_url) { $new_content += "DATABASE_URL=$db_url" }
    if (-not $has_mqtt_host) { $new_content += "MQTT_BROKER_HOST=mqtt" }
    if (-not $has_mqtt_port) { $new_content += "MQTT_BROKER_PORT=1883" }
    Set-Content -Path $env_file -Value $new_content
} else {
    $env_content = @(
        "DATABASE_URL=$db_url",
        "MQTT_BROKER_HOST=mqtt",
        "MQTT_BROKER_PORT=1883",
        "JWT_SECRET_KEY=amphive-dev-secret-change-in-production",
        "COINS_PER_RUPEE=1.0"
    )
    Set-Content -Path $env_file -Value $env_content
}

# Ensure the remote directory exists on the VM first
Write-Host "`nEnsuring remote directory $REMOTE_DIR exists on VM..." -ForegroundColor Cyan
gcloud compute ssh $VM_NAME --zone=$VM_ZONE --command="mkdir -p $REMOTE_DIR"

# ---- Step 4: Compress and copy files to VM ----
Write-Host "`n[4/5] Compressing and copying files to GCP VM ($VM_NAME)..." -ForegroundColor Cyan

# Remove old local tarball if exists
if (Test-Path "$PROJECT_ROOT\amphive_app.tar.gz") {
    Remove-Item "$PROJECT_ROOT\amphive_app.tar.gz"
}

# Create compressed tarball excluding dependencies
Push-Location $PROJECT_ROOT
tar -czf amphive_app.tar.gz --exclude node_modules --exclude .venv --exclude .git backend frontend
Pop-Location

# Upload tarball
gcloud compute scp --quiet "$PROJECT_ROOT\amphive_app.tar.gz" "${VM_NAME}:${REMOTE_DIR}/amphive_app.tar.gz" --zone=$VM_ZONE

# Clean up local tarball
Remove-Item "$PROJECT_ROOT\amphive_app.tar.gz"

# Deployment configs (flattened into /home/Sarthak/amphive/ on the VM)
gcloud compute scp --quiet "$PROJECT_ROOT\deploy\config\mosquitto.conf" "${VM_NAME}:${REMOTE_DIR}/mosquitto.conf" --zone=$VM_ZONE
gcloud compute scp --quiet "$PROJECT_ROOT\deploy\docker\docker-compose.prod.yml" "${VM_NAME}:${REMOTE_DIR}/docker-compose.yml" --zone=$VM_ZONE
gcloud compute scp --quiet "$PROJECT_ROOT\.env" "${VM_NAME}:${REMOTE_DIR}/.env" --zone=$VM_ZONE

# ---- Step 5: Rebuild and restart containers ----
Write-Host "`n[5/5] Extracting application and restarting Docker Compose on VM..." -ForegroundColor Cyan
gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command="cd $REMOTE_DIR && tar -xzf amphive_app.tar.gz && rm amphive_app.tar.gz && sudo docker-compose up -d --build"

Write-Host "`nDeployment complete!" -ForegroundColor Green
