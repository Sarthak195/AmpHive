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
$REMOTE_DIR   = "~/amphive"

# ---- Step 1: Wait for Cloud SQL to become RUNNABLE ----
Write-Host "`n[1/5] Waiting for Cloud SQL instance to become RUNNABLE..." -ForegroundColor Cyan
while ($true) {
    $state = cmd.exe /c "gcloud sql instances describe amphive-db-in --format=""value(state)"""
    if ($state.Trim() -eq "RUNNABLE") {
        Write-Host "  -> Instance is RUNNABLE!" -ForegroundColor Green
        break
    }
    Write-Host "  -> Current state: $state — retrying in 15s..."
    Start-Sleep -Seconds 15
}

# ---- Step 2: Create the database (idempotent) ----
Write-Host "`n[2/5] Ensuring 'amphive' database exists..." -ForegroundColor Cyan
cmd.exe /c "gcloud sql databases create amphive --instance=amphive-db-in 2>nul"

# ---- Step 3: Fetch Cloud SQL IP and generate .env ----
Write-Host "`n[3/5] Fetching Cloud SQL IP and writing .env..." -ForegroundColor Cyan
$ip = cmd.exe /c "gcloud sql instances describe amphive-db-in --format=""value(ipAddresses[0].ipAddress)"""
$env_content = "DATABASE_URL=postgresql+asyncpg://postgres:amphive_db_admin@$($ip.Trim()):5432/amphive`nMQTT_BROKER_HOST=mqtt`nMQTT_BROKER_PORT=1883"
Set-Content -Path "$PROJECT_ROOT\.env" -Value $env_content

# ---- Step 4: Copy files to VM ----
# Files are organized under deploy/ locally but need a flat structure on the VM.
Write-Host "`n[4/5] Copying files to GCP VM ($VM_NAME)..." -ForegroundColor Cyan

# Application code
cmd.exe /c "gcloud compute scp --quiet --recurse $PROJECT_ROOT\backend $PROJECT_ROOT\frontend ${VM_NAME}:${REMOTE_DIR}/ --zone=$VM_ZONE"

# Deployment configs (flattened into ~/amphive/ on the VM)
cmd.exe /c "gcloud compute scp --quiet $PROJECT_ROOT\deploy\config\mosquitto.conf ${VM_NAME}:${REMOTE_DIR}/mosquitto.conf --zone=$VM_ZONE"
cmd.exe /c "gcloud compute scp --quiet $PROJECT_ROOT\deploy\docker\docker-compose.prod.yml ${VM_NAME}:${REMOTE_DIR}/docker-compose.yml --zone=$VM_ZONE"
cmd.exe /c "gcloud compute scp --quiet $PROJECT_ROOT\.env ${VM_NAME}:${REMOTE_DIR}/.env --zone=$VM_ZONE"

# ---- Step 5: Rebuild and restart containers ----
Write-Host "`n[5/5] Rebuilding and restarting Docker Compose on VM..." -ForegroundColor Cyan
cmd.exe /c "gcloud compute ssh --quiet $VM_NAME --zone=$VM_ZONE --command=""cd $REMOTE_DIR && sudo docker-compose up -d --build"""

Write-Host "`nDeployment complete!" -ForegroundColor Green
