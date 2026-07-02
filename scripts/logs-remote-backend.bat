@echo off
echo Streaming logs from the remote backend (Press Ctrl+C to stop)...
gcloud compute ssh amphive-vm-in --zone=asia-south1-a --command="docker logs -f amphive-backend"
pause
