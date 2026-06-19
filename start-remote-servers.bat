@echo off
echo Starting remote servers on GCP VM (amphive-vm-in)...
gcloud compute ssh amphive-vm-in --zone=asia-south1-a --command="docker-compose up -d"
echo Servers started successfully!
pause
