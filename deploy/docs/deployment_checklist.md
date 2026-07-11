# AmpHive Step-by-Step Deployment Checklist

This document is a comprehensive checklist detailing the exact steps required to deploy a new **AmpHive** charging site—from setting up the cloud servers to configuring network hardware, flashing microcontrollers, and verifying the end-to-end telemetry loop.

---

## Phase 1: Cloud Infrastructure Deployment (One-Time Setup)

- [ ] **1.1. Provision Cloud VM Instance**
  - Register/log in to Oracle Cloud (OCI) or Google Cloud (GCP).
  - Create a virtual machine instance running Ubuntu 22.04 LTS or newer.
  - Allocate a **Static Public IP Address** to this VM.
- [ ] **1.2. Configure Cloud Security List / Firewall**
  - Open the cloud console security group rules and add ingress rules for:
    - `80/TCP` (HTTP for Headscale node authentication / Let's Encrypt challenge)
    - `443/TCP` (HTTPS for secure web endpoints / DERP TCP fallback)
    - `50443/TCP` (gRPC control interface for managing Headscale)
    - `51820/UDP` (WireGuard VPN direct data traffic tunnel)
    - `8000/TCP` (If you want to expose FastAPI public docs directly - *otherwise keep closed*)
- [ ] **1.3. Register a Dynamic DNS (DuckDNS)**
  - Visit [duckdns.org](https://www.duckdns.org/) and register a free domain subdomain (e.g., `amphive-vpn.duckdns.org`).
  - Point it to your Cloud VM's public IP address.
  - Setup a simple `cron` updater on your VM (instructions on DuckDNS website) to keep the IP synchronized.
- [ ] **1.4. Initialize Kubernetes (K3s) on VM**
  - SSH into your VM and install K3s:
    ```bash
    curl -sfL https://get.k3s.io | sh -
    ```
- [ ] **1.5. Configure Kubeconfig Permissions**
  - Run the following to allow running `kubectl` without root:
    ```bash
    mkdir -p ~/.kube
    sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
    sudo chown $USER:$USER ~/.kube/config
    ```
- [ ] **1.6. Apply Kubernetes manifests**
  - Clone or copy the deployment configurations folder to the server.
  - Run the rollout command:
    ```bash
    kubectl apply -f deploy/k8s/
    ```
- [ ] **1.7. Verify Cloud Services Status**
  - Ensure all pods show `Running` status:
    ```bash
    kubectl get pods -n amphive
    ```

---

## Phase 2: Gateway Configuration & Hardware Flashing

- [ ] **2.1. Generate Headscale Gateway Authentication Key**
  - On your GCP VM (or wherever Headscale runs), find the Headscale pod name:
    ```bash
    POD_NAME=$(kubectl get pods -l app=headscale -n amphive -o jsonpath="{.items[0].metadata.name}")
    ```
  - Generate a secure pre-authenticated key for the new ESP32 gateway:
    ```bash
    kubectl exec -it $POD_NAME -n amphive -- headscale preauthkeys create --user default --expiration 24h
    ```
  - Copy the generated key token (format like `mkey:xxxxxxxxxxxxxxxx`).
- [ ] **2.2. Configure Firmware Settings**
  - In your local copy of `firmware/main/main.c`, update the configuration block:
    * `WIFI_SSID`: Set the SSID of the local VLAN 20 network.
    * `WIFI_PASSWORD`: Set the password of the VLAN 20 network.
    * `TS_AUTH_KEY`: Paste the Headscale preauthkey token generated in Step 2.1.
    * `DEVICE_NAME`: Define a unique node hostname (e.g., `gateway_block_a`).
    * `GATEWAY_ID`: Set a unique identifier (usually the ESP32's MAC address).
    * `TARGET_PLUG_IP`: Set the static IP address that you will allocate to the smart plug.
- [ ] **2.3. Build and Flash ESP32 Gateway**
  - Connect the ESP32 microcontroller to your local development computer via USB.
  > **Note on OS Compatibility:** If you are experiencing issues connecting to the ESP32's COM port or flashing the device on Windows, it is highly recommended to use a native **Linux** or macOS environment. Windows (especially via WSL) often encounters USB pass-through and driver issues with ESP-IDF.
  - Load the ESP-IDF toolchain environment in your command line.
  - Build the binary and flash the hardware:
    ```bash
    cd firmware
    idf.py set-target esp32s3
    idf.py build
    idf.py flash monitor # Use -p /dev/ttyUSB0 or let idf.py auto-detect
    ```
  - Verify in the serial monitor that the ESP32 connects to WiFi and prints the VPN IP assignment (e.g., `VPN IP: 100.64.0.5`).

---

## Phase 3: Local Site Network Setup & Smart Plug Commissioning

- [ ] **3.1. Configure local Router (VLAN Isolation)**
  - Log into the charging site's primary network router (e.g. Ubiquiti, TP-Link, OPNsense).
  - Create a new Virtual Local Area Network: **VLAN 20** (SSID: `AmpHive_VLAN20_IoT`).
  - Configure the DHCP server for VLAN 20 to allocate IPs in a distinct subnet (e.g. `192.168.20.0/24`).
  > **Deployment Tip for Public/Mobile Sites:** If you are deploying at a site without a dedicated router (e.g., a coffee shop with public Wi-Fi) or using mobile data, do **not** connect the ESP32 and plug directly to the public Wi-Fi, as "Client Isolation" will block their local communication. Instead, install a cheap 4G LTE or travel router to broadcast a private local network for your hardware.
- [ ] **3.2. Setup Firewall Isolation Rules**
  - Create a firewall rule blocking all traffic between VLAN 20 and other private VLANs (VLAN 10 / home computers).
  - Permit outbound traffic from VLAN 20 to the WAN (Internet) only.
- [ ] **3.3. Commission Smart Plugs (Tapo P110)**
  - Power on the Tapo smart plug near the vehicle charging bay.
  - Open the Tapo mobile application on a smartphone.
  - Pair the smart plug and connect it to the VLAN 20 WiFi network (`AmpHive_VLAN20_IoT`).
  - Log into the router and configure a **Static DHCP lease** for the smart plug, ensuring its local IP matches the `TARGET_PLUG_IP` defined in the gateway firmware (e.g., `192.168.20.10`).

---

## Phase 4: Platform Registration & Validation

- [ ] **4.1. Register Gateway and Plug in Backend**
  - Register the hardware components via HTTP requests to your FastAPI endpoint:
    * **Register Gateway:**
      ```bash
      curl -X POST "http://localhost:8000/api/gateways/register" \
           -H "Content-Type: application/json" \
           -d '{"gateway_id": "00_11_22_33_aa_bb", "name": "block_a_gateway", "vpn_ip": "100.64.0.5"}'
      ```
    * **Register Smart Plug:**
      ```bash
      curl -X POST "http://localhost:8000/api/plugs/register" \
           -H "Content-Type: application/json" \
           -d '{"gateway_id": "00_11_22_33_aa_bb", "name": "bay_01_plug", "local_ip": "192.168.20.10", "plug_model": "tapo_p110"}'
      ```
- [ ] **4.2. Verify Telemetry Streaming**
  - Monitor the backend server logs to verify that the ESP32 is successfully publishing telemetry parameters:
    ```bash
    kubectl logs deployment/backend -n amphive --tail=50
    ```
  - Verify that log prints show periodic power and energy status updates.
- [ ] **4.3. Run End-to-End Command Validation**
  - Trigger a test charging session start:
    ```bash
    curl -X POST "http://localhost:8000/api/sessions/start" \
         -H "Content-Type: application/json" \
         -d '{"gateway_id": "00_11_22_33_aa_bb", "plug_id": 1, "user_id": 1}'
    ```
  - Verify that the physical smart plug click-switches ON and draws power.
  - Trigger a test charging session stop:
    ```bash
    curl -X POST "http://localhost:8000/api/sessions/stop" \
         -H "Content-Type: application/json" \
         -d '{"gateway_id": "00_11_22_33_aa_bb", "plug_id": 1, "session_id": 1}'
    ```
  - Verify that the physical smart plug switch cuts power immediately.
