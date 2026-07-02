@echo off
echo Restarting remote servers on GCP VM (amphive-vm-in)...
gcloud compute ssh amphive-vm-in --zone=asia-south1-a --command="docker-compose restart"
echo Servers restarted successfully!
pause
