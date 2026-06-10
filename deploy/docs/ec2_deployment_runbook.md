# AWS EC2 Deployment Log & Operation Runbook

This document details the exact sequence of commands executed, configurations applied, and debugging steps taken to deploy **AmpHive** to the AWS EC2 instance (`ec2-3-111-213-25.ap-south-1.compute.amazonaws.com`) on June 9, 2026.

---

## 1. Sequence of Operations Performed

### Step 1: Environment Audit
We ran a remote script to identify existing infrastructure on the EC2 instance:
* **Command:**
  ```bash
  docker --version; docker compose version; k3s --version; kubectl version --client
  ```
* **Result:**
  * Docker: `command not found` (Not installed)
  * K3s: `v1.35.5+k3s1` (Installed & running)
  * Kubectl: `v1.35.5+k3s1` (Installed)

### Step 2: Preparing and Uploading Code
We compressed and copied the backend application and deployment configs to the server:
* **Local Zip Command (PowerShell):**
  ```powershell
  Compress-Archive -Path backend, deploy, mosquitto.conf, docker-compose.yml -DestinationPath amphive.zip -Force
  ```
* **Local SCP Upload Command:**
  ```powershell
  scp -i "AmpHive EC2.pem" amphive.zip ubuntu@ec2-3-111-213-25.ap-south-1.compute.amazonaws.com:~
  ```

### Step 3: Installing Dependencies & Extraction
We updated the Ubuntu package index, installed the unzipper and Docker daemon, and extracted the codebase:
* **Remote Commands:**
  ```bash
  sudo apt-get update
  sudo apt-get install -y unzip docker.io
  mkdir -p ~/amphive
  unzip -o ~/amphive.zip -d ~/amphive
  ```

### Step 4: Building & Importing the Backend Container Image
Since the K3s cluster runs on a local containerd runtime, we compiled the image locally and side-loaded it (bypassing the need for a Docker registry push):
* **Build Command:**
  ```bash
  cd ~/amphive
  sudo docker build -t sarthak195/amphive-backend:latest ./backend
  ```
* **Export and K3s Import Commands:**
  ```bash
  sudo docker save sarthak195/amphive-backend:latest -o ~/amphive/backend.tar
  sudo k3s ctr images import ~/amphive/backend.tar
  ```
  *(Note: K3s's container runtime, containerd, now holds the `sarthak195/amphive-backend:latest` image locally. The `imagePullPolicy: IfNotPresent` parameter in `backend.yaml` triggers K3s to use this image).*

### Step 5: Kubernetes Manifest Application
We deployed all manifest resources to K3s:
* **Remote Commands:**
  ```bash
  sudo kubectl apply -f ~/amphive/deploy/k8s/namespace.yaml
  sudo kubectl apply -f ~/amphive/deploy/k8s/
  ```

---

## 2. Troubleshooting & Configuration Fixes (Headscale Crash)

Upon first deploy, the `headscale` Pod entered a `CrashLoopBackOff` status. 

### Diagnostics:
Running `sudo kubectl logs deployment/headscale -n amphive` revealed:
1. `Fatal config error: headscale now requires a new noise.private_key_path field`
2. `Fatal config error: dns.nameservers.global must be set when dns.override_local_dns is true`

### Fix Applied:
We modified `deploy/k8s/headscale.yaml` locally, re-uploaded it, and re-applied it. The changes made to `config.yaml` inside the ConfigMap were:
* **Noise configuration block:** Nested the private key path under the `noise:` YAML object.
  ```yaml
  noise:
    private_key_path: /var/lib/headscale/noise_private.key
  ```
* **DNS configuration block:** Added DNS parameters to global nameservers.
  ```yaml
  dns:
    magic_dns: true
    base_domain: amphive.mesh
    override_local_dns: true
    nameservers:
      global:
        - 1.1.1.1
        - 1.0.0.1
  ```
* **Subnet prefixes config:** Restructured `ip_prefixes: - 100.64.0.0/10` to `prefixes: v4: 100.64.0.0/10`.
* **SQLite database config:** Restructured `db_type` and `db_path` to:
  ```yaml
  database:
    type: sqlite
    sqlite:
      path: /var/lib/headscale/db.sqlite
  ```

### Rollout Command:
```bash
sudo kubectl apply -f ~/amphive/deploy/k8s/headscale.yaml
sudo kubectl rollout restart deployment/headscale -n amphive
```
This resolved the error, and the headscale Pod launched successfully.

---

## 3. Operational Troubleshooting Cheat Sheet

If you experience issues in the future, use this cheat sheet to interact with the EC2 K3s cluster:

### A. General Cluster Status
```bash
# Get all running pods, services, and deployments in the AmpHive namespace
sudo kubectl get all -n amphive

# Describe a specific pod to debug startup/scheduling issues (e.g., pending PVCs)
sudo kubectl describe pod <pod-name> -n amphive

# Check storage claims status
sudo kubectl get pvc -n amphive
```

### B. Checking Logs
```bash
# View FastAPI Backend logs:
sudo kubectl logs deployment/backend -n amphive --tail=100 -f

# View Headscale VPN Server logs:
sudo kubectl logs deployment/headscale -n amphive --tail=100 -f

# View Mosquitto MQTT logs:
sudo kubectl logs deployment/mosquitto -n amphive --tail=100 -f

# View PostgreSQL Database logs:
sudo kubectl logs deployment/postgres -n amphive --tail=100 -f
```

### C. Rebuilding and Redeploying Code Changes
If you make code modifications in your `backend/` directory and want to update the running container:
1. **Re-upload** the backend directory or zip to the EC2 server.
2. **Rebuild the image:**
   ```bash
   cd ~/amphive
   sudo docker build -t sarthak195/amphive-backend:latest ./backend
   ```
3. **Import it into K3s containerd:**
   ```bash
   sudo docker save sarthak195/amphive-backend:latest -o ~/amphive/backend.tar
   sudo k3s ctr images import ~/amphive/backend.tar
   rm -f ~/amphive/backend.tar
   ```
4. **Trigger a rolling restart:**
   ```bash
   sudo kubectl rollout restart deployment/backend -n amphive
   ```
   Kubernetes will spin up the new pod, verify its health check, and tear down the old pod automatically.

### D. Executing CLI commands inside Headscale Container
To manage Tailscale preauthkeys, registers, and nodes:
```bash
# Get the headscale pod name
POD_NAME=$(sudo kubectl get pods -l app=headscale -n amphive -o jsonpath="{.items[0].metadata.name}")

# List all registered nodes in the mesh
sudo kubectl exec -it $POD_NAME -n amphive -- headscale nodes list

# Create a pre-authenticated key for an ESP32 gateway
sudo kubectl exec -it $POD_NAME -n amphive -- headscale preauthkeys create --user default --expiration 24h
```

---

## 4. Frontend MVP Deployment (June 9, 2026)

We containerized the React Vite Driver Wallet frontend and deployed it to the K3s cluster.

### Step 1: Transfer Code
We archived the `frontend` folder to avoid transferring `node_modules`, and scp'd it to the EC2 host:
```powershell
tar.exe -czf frontend.tar.gz --exclude=frontend/node_modules --exclude=frontend/dist frontend
scp -i "AmpHive EC2.pem" -o StrictHostKeyChecking=no frontend.tar.gz ubuntu@ec2-3-111-213-25.ap-south-1.compute.amazonaws.com:~/amphive/
```

### Step 2: Build and Deploy on EC2
We connected via SSH and executed a chained command to extract, build the Nginx container, load it into `containerd`, and apply the Kubernetes manifests.

* **Remote Command Run:**
```bash
cd ~/amphive
rm -rf frontend
tar -xzf frontend.tar.gz

# Build multi-stage Docker image (Node 22 + Nginx)
sudo docker build -t sarthak195/amphive-frontend:latest ./frontend

# Export and load to K3s containerd
sudo docker save sarthak195/amphive-frontend:latest -o ~/amphive/frontend-image.tar
sudo k3s ctr images import ~/amphive/frontend-image.tar

# Apply frontend manifest (Deployment, Service, Ingress)
sudo kubectl apply -f ~/amphive/deploy/k8s/frontend.yaml
```

The frontend is now deployed using the Traefik Ingress controller capturing traffic on HTTP port 80.
