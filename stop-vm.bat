@echo off

echo ===================================================
echo      AmpHive Shutdown Script (VM + Database)
echo ===================================================

echo [1/2] Stopping GCP VM Instance (amphive-vm-in)...
call gcloud compute instances stop amphive-vm-in --zone=asia-south1-a

echo [2/2] Stopping Cloud SQL Database (amphive-db-in)...
call gcloud sql instances patch amphive-db-in --activation-policy=NEVER

echo ===================================================
echo AmpHive Cloud Infrastructure is now offline (sleeping).
echo You are no longer incurring major compute costs.
echo ===================================================
pause
