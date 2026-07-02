@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo      AmpHive Startup Script (VM only)
echo ===================================================
echo NOTE: Database now runs as a Docker container on the VM.
echo       No Cloud SQL required.

echo [1/1] Starting GCP VM Instance (amphive-vm-in)...
call gcloud compute instances start amphive-vm-in --zone=asia-south1-a

echo ===================================================
echo AmpHive VM is starting up!
echo Docker containers (backend, frontend, mqtt, db) will
echo start automatically via docker-compose restart:always.
echo
echo Wait ~30 seconds, then access:
echo   Frontend : check IP with: gcloud compute instances list
echo   Backend  : http://<VM_IP>:8000/docs
echo
echo If using Direct Mode, ensure WireGuard tunnel is active.
echo ===================================================
pause
