@echo off
echo Stopping the GCP VM Instance (amphive-vm-in) to save costs...
gcloud compute instances stop amphive-vm-in --zone=asia-south1-a
echo VM stopped successfully!
pause
