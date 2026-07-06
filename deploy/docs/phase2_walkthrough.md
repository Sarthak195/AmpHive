# AmpHive Backend Direct Mode & Infrastructure Walkthrough

This document serves as a complete log of everything we accomplished to establish cloud-to-local communication without ESP32 hardware, fix deployment bugs, and optimize cloud costs.

## 1. Cloud-to-Local Direct Mode Relay

To bypass the lack of an ESP32 gateway and control the local Tapo P110 smart plug directly from the Google Cloud VM, we implemented a custom HTTP Relay architecture over a WireGuard VPN tunnel.

### Why not use simple Port Forwarding?
Because of ISP Carrier-Grade NAT (CGNAT), local firewalls, and Windows NAT routing restrictions, we could not easily port forward traffic from the GCP VM directly to the smart plug.

### The Solution: HTTP Relay Server
1. **Python Relay (`tools/relay_server.py`)**: We created a lightweight Python `http.server` running on port 8000 on the local Windows PC. This server acts as a proxy, translating simple HTTP requests from the cloud into complex local `tapo` library commands.
2. **Backend Integration (`backend/services/tapo_direct.py`)**: We modified the backend's `TapoDirectDriver` to detect the presence of `TAPO_RELAY_URL`. If set, instead of using the raw `tapo` library on the VM, it forwards the commands to the relay server via the WireGuard tunnel (`http://10.10.0.2:8000`).
3. **Firewall Configurations**: We executed a PowerShell command to allow inbound TCP traffic on port 8000 through the Windows Defender Firewall:
   ```powershell
   New-NetFirewallRule -DisplayName "AmpHive Relay Server" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
   ```

## 2. Database Initialization Bug Fix

During testing, we discovered that the PostgreSQL database tables were completely wiped and not being created automatically when the backend containers were deployed.

### The Fix:
1. Added a robust `init_db()` function in `backend/database/db.py` to instruct SQLAlchemy to create all tables.
2. Hooked the `init_db()` function into the FastAPI `lifespan` in `backend/main.py` so that it runs synchronously during the container boot sequence before traffic is accepted.
   ```python
   @asynccontextmanager
   async def lifespan(app: FastAPI):
       await init_db()
       # ... MQTT connection and Tapo setup
   ```
3. Created an emergency `tools/init_db.py` standalone script as a fallback mechanism.

## 3. Persistent Wake-from-Sleep Connections

Cloud SQL dynamically re-assigns its public IP address whenever it is stopped and restarted (waking from sleep). Because the `amphive-backend` container reads the database URL from `.env` on startup, it was connecting to a stale, incorrect IP, resulting in `500 Internal Server Errors`.

### The Fix: Dynamic Startup/Shutdown Scripts
We completely rewrote the local Windows batch scripts to orchestrate the entire GCP environment sequentially.

#### `start-vm.bat`
This script now does the following sequentially:
1. Boots the Cloud SQL Database (`amphive-db-in`).
2. Boots the GCP VM (`amphive-vm-in`).
3. Dynamically fetches the fresh, newly assigned Cloud SQL IP address.
4. Connects to the VM via SSH, updates the `.env` file with the new database IP, and reboots the Docker containers.

```bat
call gcloud sql instances patch amphive-db-in --activation-policy=ALWAYS
call gcloud compute instances start amphive-vm-in --zone=asia-south1-a
for /f "tokens=*" %%i in ('gcloud sql instances describe amphive-db-in --format="value(ipAddresses[0].ipAddress)"') do set DB_IP=%%i
call gcloud compute ssh amphive-vm-in --zone=asia-south1-a --command="sed -i '/^DATABASE_URL=/d' ~/amphive/.env && echo DATABASE_URL=postgresql+asyncpg://postgres:<DB_PASSWORD>@%DB_IP%:5432/amphive >> ~/amphive/.env && cd ~/amphive && sudo docker-compose down && sudo docker-compose up -d"
```

#### `stop-vm.bat`
Gracefully halts both the VM and the Cloud SQL database to ensure no major compute costs are incurred while the environment is sleeping.

```bat
call gcloud compute instances stop amphive-vm-in --zone=asia-south1-a
call gcloud sql instances patch amphive-db-in --activation-policy=NEVER
```

## 4. Code Cleanup
To keep the root of the repository pristine, we removed temporary test configurations and created a dedicated `tools/` directory housing the direct mode testing tools:
- `tools/relay_server.py`
- `tools/local_tapo_test.py`
- `tools/turn_on.py`
- `tools/turn_off.py`

Everything has been successfully committed to Git and pushed to GitHub.

> [!TIP]
> **Developer Workflow:** Whenever you sit down to develop or test the application, simply run `start-vm.bat`. Wait a few minutes for the database to boot, and the script will automatically wire up the dynamic IPs. When you are done for the day, run `stop-vm.bat`.
