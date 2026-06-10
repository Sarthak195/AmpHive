# AmpHive EV Charging Platform: Detailed Features Roadmap

This catalog contains the specifications, workflows, and implementation details for the remaining features of the **AmpHive** platform, designed to guide step-by-step development across multiple conversations.

---

## Feature Category 1: Frontend Applications & User Interfaces

### 1.1. Prepaid Driver Wallet Web Application (End-User App)
* **Goal:** A mobile-responsive web app (React or Next.js) for EV drivers to top up wallets, start/stop charging, and check session progress.
* **Key Workflows:**
  * **QR Code Scan Landing Page:** User scans a QR code label on the charging outlet (`amphive.com/charge?plug_id=X`). The app parses `plug_id`, checks if the user is authenticated, and checks their wallet balance.
  * **Interactive Session Monitor:** Displays real-time charging telemetry: active charging speed (kW), current drawn (Amps), elapsed duration, energy added (kWh), and the real-time cost debited from their balance.
  * **Wallet Top-Up Screen:** Displays their current coin balance. Exposes a Stripe/Apple Pay/Google Pay payment checkout interface to purchase additional virtual coins.
* **Implementation Steps:**
  1. Build a responsive, dark-mode glassmorphic frontend utilizing React + Vite.
  2. Implement state management using React Context (`AuthContext` and `SessionContext`).
  3. Integrate SSE (Server-Sent Events) or WebSockets connecting to the backend API (`/api/sessions/live`) for real-time telemetry updates.

### 1.2. Charging Point Operator (CPO) Administrative Portal
* **Goal:** A Next.js web dashboard for property managers and operators to configure their locations and manage assets.
* **Key Workflows:**
  * **Location & Asset Configurator:** Allows CPOs to register new ESP32 gateways, pair smart plugs, assign local VLAN IP addresses, and name outlets.
  * **Dynamic Pricing Engine Interface:** A control panel where operators can set charging rates:
    * Cost per kWh (e.g. 5 coins / kWh)
    * Cost per minute (e.g. 1 coin / minute)
    * Flat session start fee (e.g. 10 coins)
    * Idle fee (charged if a car is plugged in but drawing less than 10W for over 15 minutes).
  * **Financial Revenue Dashboard:** Graphical chart reporting total electricity consumed, revenue earned (in coins/fiat equivalent), carbon offset metrics, and hourly load graphs.
* **Implementation Steps:**
  1. Set up a Next.js administrative dashboard.
  2. Incorporate charts (`recharts` or Chart.js) showing time-series database metrics.
  3. Implement backend API routes under `/api/cpo/` to fetch aggregated analytics.

---

## Feature Category 2: Financial & Stripe Billing Integrations

### 2.1. Stripe Checkout Integration
* **Goal:** Implement credit card payments to buy virtual coins.
* **Key Workflows:**
  * **Checkout Session Creation:** The driver requests a top-up (e.g., "$10 for 100 coins"). The backend calls Stripe's API to create a `CheckoutSession` and returns the payment URL.
  * **Secure Webhook Handler:** A backend endpoint (`/api/payments/webhook`) receiving Stripe event signals.
  * **Automated Ledger Credits:** When Stripe reports `checkout.session.completed`, the webhook parses the user ID and credit amount, updates `users.coin_balance` in a thread-safe database transaction, and inserts an audit log into `ledger_transactions`.
* **Implementation Steps:**
  1. Install `stripe` Python SDK in `backend/requirements.txt`.
  2. Implement `/api/payments/checkout` generating Stripe session URLs.
  3. Implement the webhook receiver verifying signatures using your Stripe Webhook Secret.
  4. Build database lock validations to prevent coin duplication or race conditions.

---

## Feature Category 3: Hardware & Onboarding Optimizations

### 3.1. ESP32 WiFi Captive Portal (On-Premise Onboarding)
* **Goal:** Allow technicians to configure new gateways on-site without opening a code editor.
* **Key Workflows:**
  * **Setup Mode AP:** If the ESP32 boots and fails to connect to its saved WiFi credentials, it spins up an internal Access Point: `AmpHive_Setup_{MAC}` and hosts a local HTTP portal on `192.168.4.1`.
  * **Config Portal Page:** When a phone connects, it displays a captive portal form requesting:
    * Local WiFi SSID and Password
    * Headscale VPN Auth Key (generated from the cloud server CLI)
    * Target Smart Plug IP address on VLAN 20
  * **Validation & Reboot:** Once submitted, the credentials are saved to ESP32 Non-Volatile Storage (NVS), the setup AP turns off, and the gateway reboots into production.
* **Implementation Steps:**
  1. Implement ESP-IDF `wifi_prov_mgr` (WiFi Provisioning Manager) or standard DNS redirect captive portal.
  2. Write config data to flash NVS.
  3. Modify `main.c` to read settings from NVS on boot before running `wifi_init()`.

### 3.2. ESP32 Remote Firmware Over-The-Air (OTA) Updates
* **Goal:** Remotely deploy firmware updates to all gateways over the secure VPN.
* **Key Workflows:**
  * **OTA Check-In:** The gateway routinely queries `/api/gateways/version` over the VPN, reporting its current firmware hash.
  * **Firmware Streaming:** If a newer version exists, the backend authorizes a binary stream download.
  * **Safe Partition Rollback:** The ESP32 writes the binary to its inactive OTA flash partition, verifies the MD5 checksum, updates the bootloader to target the new partition, and reboots. If the new partition crashes, it rollbacks automatically to the safe partition.
* **Implementation Steps:**
  1. Setup secondary APP partition slots in `partitions.csv` for the firmware.
  2. Implement the standard ESP-IDF `esp_https_ota` driver wrapper connecting over the VPN socket.
  3. Expose a secure binary upload endpoint on the FastAPI server.

---

## Feature Category 4: Load Management & Grid Safety

### 4.1. Dynamic Load Balancing (DLB)
* **Goal:** Limit total power consumption across all smart plugs in a building to protect the physical electric grid breaker.
* **Key Workflows:**
  * **Grid Constraint Rules:** CPO defines a grid capacity limit for a gateway (e.g. max 32 Amps total across 4 outlets).
  * **Real-Time Load Analysis:** If 3 vehicles plug in simultaneously (drawing 12A + 12A + 12A = 36A), the gateway detects the threshold breach using real-time telemetry.
  * **Throttling Queue:** The gateway cycles outlets ON and OFF (e.g., duty cycling) or uses local communication protocols to dynamically dial down charging currents, preventing the building breaker from blowing.
* **Implementation Steps:**
  1. Extend `schema.sql` and `models.py` with `gateways.max_current_amps` limits.
  2. Create a thread-safe monitoring algorithm in the backend or on the ESP32 edge gateway to coordinate plug power allocation.
