# ESP32-Tailscale-WoL Project Context

## System Overview
The ESP32 Tailscale Wake-on-LAN (WoL) Gateway is a headless firmware designed to run on ESP32-S3 microcontrollers with external PSRAM. It integrates Tailscale VPN capabilities, allowing remote users to securely trigger Wake-on-LAN magic packets to computers on the local network via an encrypted HTTP API endpoint.

## Architecture & Tech Stack
- **Framework**: ESP-IDF v5.3 (Expressif IoT Development Framework).
- **Core Library**: **MicroLink** for embedded Tailscale control client implementation.
- **WireGuard Stack**: Encapsulated packet routing using lwIP integration.
- **OS Layer**: FreeRTOS for task scheduling and memory synchronization.
- **Web Interface**: Lightweight ESP-IDF HTTP server (`esp_http_server`) running on port 80 over the VPN interface.

## Core Runtime Modules & Tasks
- **WiFi Coordinator**: Establishes STA mode connection and handles automatic reconnect logic.
- **HTTP Server**: Listens for `GET /wol?mac=...` API calls and validates MAC address formatting.
- **MicroLink Engine**: Background worker thread that coordinates peer exchanges, decrypts traffic, performs NAT hole punching via STUN, and handles DERP fallback routes.

## Key Mechanisms
1. **Private IP Allocation**: Upon successful connection to the Tailnet, the control server allocates a dedicated private VPN IP (100.x.y.z) to the ESP32.
2. **Direct Socket Operations**: Once a valid HTTP request is received, the system sets socket options to allow broadcasting and pushes the 102-byte Wake-on-LAN payload over UDP to port 9 of the configured subnet broadcast address (e.g. `192.168.1.255`).
3. **PSRAM Memory Split**: Allocates high-overhead TLS buffers to external PSRAM, preventing internal RAM exhaustion and system crashes.

## Directory Structure
- `/main`: Main application code.
  - `CMakeLists.txt`: Build rules.
  - `main.c`: WiFi init, HTTP handlers, MicroLink worker loop, and entry point.
- `/components`: Embedded libraries.
  - `/microlink`: Core library containing coordination logic, crypto handlers, NAT traversal, and peer registries.
  - `/wireguard_lwip`: WireGuard encapsulation layers for lightweight IP routing.
- `sdkconfig.defaults`: Hardware/IDF configuration variables (PSRAM sizing, MbedTLS features, heap policies).
- `partitions.csv`: Custom partition allocations.
