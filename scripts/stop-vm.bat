@echo off

echo ===================================================
echo      AmpHive Shutdown Script (VM only)
echo ===================================================
echo NOTE: Database runs as a Docker container on the VM.
echo       Stopping the VM stops all containers including Postgres.
echo       Data persists in the Docker volume on the VM disk.

echo [1/1] Stopping GCP VM Instance (amphive-vm-in)...
call gcloud compute instances stop amphive-vm-in --zone=asia-south1-a

echo ===================================================
echo AmpHive VM is now stopped.
echo No major compute costs are being incurred.
echo (Cloud SQL has been decommissioned - no SQL instance cost either)
echo ===================================================
pause

