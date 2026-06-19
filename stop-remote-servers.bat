@echo off
echo Shutting down remote servers on GCP VM (amphive-vm-in)...
gcloud compute ssh amphive-vm-in --zone=asia-south1-a --command="docker-compose down"
echo Servers shut down successfully!
pause
