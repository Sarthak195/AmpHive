# AI Agent Guidelines for ESP32-Tailscale-WoL

Welcome to the ESP32 Tailscale VPN & Wake-on-LAN repository. If you are an AI assistant or developer working on this codebase, please adhere to these guidelines to ensure firmware stability, memory integrity, and network reliability.

---

## 1. Tech Stack & Environment
- **Framework**: ESP-IDF v5.3 (C-based environment). Do not use Arduino libraries unless strictly wrapped or isolated.
- **RTOS**: FreeRTOS. Utilize task prioritization and event groups for asynchronous flows (e.g. WiFi connection events).
- **Networking**: lwIP (Lightweight IP) stack integrated with the ESP-IDF TCP/IP layer.
- **Crypto & TLS**: MbedTLS configured via `sdkconfig`. Core cryptography utilizes custom x25519 and nacl_box wrappers located in `/components/microlink/src/`.

---

## 2. Architectural & Memory Rules

1. **PSRAM Requirement**:
   - The Tailscale protocol requires substantial heap memory for WireGuard key exchanges, STUN/DERP connection tables, and TLS handshakes.
   - You **must** ensure PSRAM configuration options are enabled (`CONFIG_SPIRAM=y`) and configured for Octal SPI if using ESP32-S3-N16R8 modules.
2. **Stack Allocation Warning**:
   - Standard ESP32 tasks default to 2KB–4KB stacks. The MicroLink/Tailscale handshake requires at least **32KB** of stack space. 
   - Always spawn the `microlink_task` with a stack size of at least `32768` bytes (`xTaskCreate` parameter).
3. **No Network Interrupt Blocking**:
   - Ensure the HTTP server handlers (`wol_handler`) do not perform blocking operations or heavy loops.
   - Keep network broadcast operations (like Wake-on-LAN packet sending) non-blocking and quick to close sockets.

---

## 3. Key Files to Understand

- `main/main.c`: Contains the main entry point (`app_main`), WiFi initialization, the HTTP server routing, and the `microlink_task` worker loop.
- `sdkconfig.defaults`: Defines configuration defaults like PSRAM activation, stack sizing, and the custom DISCO port.
- `components/microlink/src/microlink_coordination.c`: The core state machine coordinating connection handshakes with Tailscale control plane servers.
- `components/microlink/src/microlink_disco.c` & `microlink_stun.c`: Implement Tailscale NAT-traversal discovery and STUN mechanisms.

---

## 4. Development & Troubleshooting Workflow

### Port Conflicts
- By default, WireGuard utilizes UDP port 51820. 
- The Tailscale DISCO (Discovery) port has been modified to **51821** (`CONFIG_MICROLINK_DISCO_PORT`) to prevent socket binding collisions on the ESP32. Ensure this is maintained.

### Heartbeat Loop Reconnections
- In `microlink_coordination.c`, the heartbeat condition must check and keep connections active. If Tailscale drops connections after 2 seconds, ensure the skip heartbeat connection condition remains patched.

### Build Commands
```bash
# Verify environment is set up
source ~/esp/esp-idf/export.sh

# Target configuration
idf.py set-target esp32s3

# Build, flash and trace output
idf.py build
idf.py -p <PORT> flash monitor
```

---

## 5. System Log Monitoring
- Use the ESP logging framework (`ESP_LOGI`, `ESP_LOGW`, `ESP_LOGE`) to debug state transitions.
- Key logs to monitor:
  - `example: State: MICROLINK_STATE_IDLE -> MICROLINK_STATE_CONNECTING`
  - `example: *** TAILSCALE CONNECTED ***`
  - `wol: Magic packet sent to AA:BB:CC:DD:EE:FF via 192.168.1.255:9`
