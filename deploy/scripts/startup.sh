#!/bin/bash
# =============================================================================
# AmpHive VM Bootstrap Script
# Run once on a fresh Ubuntu VM to install Docker and enable the daemon.
# Usage: gcloud compute ssh amphive-vm-in -- 'bash -s' < deploy/scripts/startup.sh
# =============================================================================

set -euo pipefail

echo "[1/3] Updating package index..."
sudo apt-get update

echo "[2/3] Installing Docker..."
sudo apt-get install -y docker.io docker-compose

echo "[3/3] Enabling Docker daemon..."
sudo systemctl enable docker
sudo systemctl start docker

echo "VM bootstrap complete. Docker is running."
