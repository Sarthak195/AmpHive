@echo off
REM ---------------------------------------------------------------------
REM Retired 2026-08-18. This helper targeted `amphive-vm-in` in
REM `asia-south1-a` -- a VM DELETED in the 2026-07-27 consolidation -- and
REM drove it with docker-compose v1, which the relay stack does not use.
REM Running it did nothing useful and misled anyone who tried.
REM
REM The live stack is `amphive-relay` in `us-west1-a`, at ~/amphive-relay,
REM using `docker compose -f docker-compose.relay.yml` (v2 plugin).
REM See docs/DEPLOYMENT.md and deploy/scripts/deploy.ps1.
REM ---------------------------------------------------------------------
echo Streaming backend logs from amphive-relay (Ctrl+C to stop)...
gcloud compute ssh amphive-relay --zone=us-west1-a --command="sudo docker logs -f $(sudo docker ps --filter label=com.docker.compose.service=backend --format {{.Names}} | head -1)"
pause
