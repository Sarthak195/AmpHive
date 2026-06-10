# AmpHive: New Workstation Development Setup Guide

This document provides a step-by-step guide for setting up a new development workstation (Windows/macOS/Linux) to continue coding, building, and deploying the **AmpHive** EV charging platform.

---

> [!WARNING]
> **DEVELOPMENT WORKSTATION ONLY**: 
> The local machine is for writing code, compiling firmware, and editing configurations. **Never** run `docker compose up`, database migrations, or any deployment commands locally. All integration testing and runtime executions must take place on the remote Google Cloud Platform VM (`amphive-vm-in`) via the deploy script or SSH.

---

## 1. Files to Migrate Manually (Gitignored)

Because certain files containing secrets and local variables are listed in `.gitignore`, they will not be fetched when cloning the repository. You must manually copy these from your old device:

### 1.1. Environment Variables Configuration (`.env`)
Copy the `.env` file to the root of the project (`AmpHive/.env`):
```ini
DATABASE_URL=postgresql+asyncpg://postgres:amphive_db_admin@8.231.89.63:5432/amphive
MQTT_BROKER_HOST=mqtt
MQTT_BROKER_PORT=1883
```
*Note: The database URL points directly to the live production Cloud SQL instance. Keep this secure.*

---

## 2. Setting Up the Local Codebase & Submodules

1. **Clone the repository** from GitHub.
2. **Initialize and update submodules**:
   The ESP32 firmware relies on external submodules for WireGuard and the Tailscale client (`microlink`). You must pull these submodules before compiling:
   ```bash
   git submodule update --init --recursive
   ```

---

## 3. Install Development Tooling

### 3.1. Google Cloud SDK (gcloud CLI)
The deployment script uses the `gcloud` CLI to check database states, SCP code, and SSH to the VM.
1. Download and install the [Google Cloud SDK](https://cloud.google.com/sdk/docs/install).
2. Authenticate the CLI with your Google account:
   ```bash
   gcloud auth login
   ```
3. Set your active project containing the AmpHive resources:
   ```bash
   # List your projects to find the active one
   gcloud projects list
   
   # Set the active project ID
   gcloud config set project <PROJECT_ID>
   ```
4. Verify you can list the instances:
   ```bash
   gcloud compute instances list
   ```
   You should see `amphive-vm-in` in zone `asia-south1-a` running.

### 3.2. Python Backend Environment
1. Ensure **Python 3.11** is installed.
2. Create and activate a virtual environment inside the `/backend` folder:
   ```powershell
   # Windows (PowerShell)
   cd backend
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   
   # Install dependencies
   pip install -r requirements.txt
   ```

### 3.3. React Frontend Environment
1. Ensure **Node.js (LTS)** is installed.
2. Install packages inside the `/frontend` folder:
   ```bash
   cd frontend
   npm install
   ```

### 3.4. ESP32-S3 Firmware Compiler (ESP-IDF)
The gateway firmware requires the Espressif IoT Development Framework (ESP-IDF) v5.x.
1. Install [ESP-IDF v5.x](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/index.html) (recommended: v5.3 matching the context repos).
2. Set up the terminal toolchain:
   - On Windows: Use the **ESP-IDF 5.x PowerShell** or Command Prompt shortcut created by the installer.
   - On Linux/macOS: Run `source ~/esp/esp-idf/export.sh`.
3. To compile the gateway code:
   ```bash
   cd firmware
   
   # Set target to ESP32-S3
   idf.py set-target esp32s3
   
   # Build the firmware
   idf.py build
   
   # Flash over USB (replace COM3/usbmodem with your actual port) and launch serial monitor
   idf.py -p COM3 flash monitor
   ```

---

## 4. Run Deployments from the New Device

Before running the deployment script in PowerShell on Windows, you must allow scripts to run:

1. Open PowerShell (as Administrator) and run:
   ```powershell
   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
   ```
2. You can now deploy updates directly to the live GCP server using:
   ```powershell
   .\deploy\scripts\deploy.ps1
   ```
   This will:
   - Verify Cloud SQL status.
   - Ensure the `amphive` database exists.
   - Retrieve the current Cloud SQL database IP.
   - Write the fresh database URL to your local `.env`.
   - Upload backend and frontend code to `amphive-vm-in`.
   - Restart and rebuild the containers on the VM.
