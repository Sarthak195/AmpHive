@echo off
echo Starting the GCP VM Instance (amphive-vm-in)...
gcloud compute instances start amphive-vm-in --zone=asia-south1-a
echo VM started successfully! Note: The DuckDNS IP will update automatically within 5 minutes.
pause
