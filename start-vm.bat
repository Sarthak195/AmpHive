@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo      AmpHive Startup Script (VM + Database)
echo ===================================================

echo [1/4] Starting Cloud SQL Database (amphive-db-in)...
call gcloud sql instances patch amphive-db-in --activation-policy=ALWAYS

echo [2/4] Starting GCP VM Instance (amphive-vm-in)...
call gcloud compute instances start amphive-vm-in --zone=asia-south1-a

echo [3/4] Fetching new Cloud SQL IP address...
for /f "tokens=*" %%i in ('gcloud sql instances describe amphive-db-in --format="value(ipAddresses[0].ipAddress)"') do set DB_IP=%%i
set DB_IP=%DB_IP: =%

if "%DB_IP%"=="" (
    echo ERROR: Failed to fetch Cloud SQL IP.
    pause
    exit /b 1
)

echo        Database IP is now: %DB_IP%

echo [4/4] Updating backend configuration on VM and restarting containers...
set NEW_DB_URL=postgresql+asyncpg://postgres:amphive_db_admin@%DB_IP%:5432/amphive
call gcloud compute ssh amphive-vm-in --zone=asia-south1-a --command="sed -i '/^DATABASE_URL=/d' ~/amphive/.env && echo DATABASE_URL=%NEW_DB_URL% >> ~/amphive/.env && cd ~/amphive && sudo docker-compose down && sudo docker-compose up -d"

echo ===================================================
echo AmpHive Backend is completely online!
echo NOTE: DuckDNS IP will update automatically within 5 minutes.
echo Ensure WireGuard tunnel is active on your PC if using Direct Mode.
echo ===================================================
pause
