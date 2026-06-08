# ESP32-Tailscale-WoL Firmware Architecture

This document outlines the firmware architecture, task hierarchy, network routing structures, and cryptographic processes utilized by the ESP32 Tailscale Wake-on-LAN gateway.

---

## 1. Boot Execution Sequence

When the ESP32-S3 boots up, execution follows a synchronous initialization flow:

```
[ Power On ]
      │
      ▼
[ Initialize NVS Flash ]
      │
      ▼
[ Initialize TCP/IP Adapter & WiFi STA Mode ]
      │
      ▼
[ Wait for WiFi connection (WIFI_CONNECTED_BIT) ]
      │
      ├────────────────────────┐
      ▼                        ▼
[ Start HTTP Server (Port 80) ]  [ Spawn MicroLink Task (32KB Stack) ]
                               │
                               ▼
                         [ Tailscale Handshake ]
                               │
                               ▼
                         [ Active Tunnel Polling ]
```

---

## 2. FreeRTOS Task Hierarchy & Priorities

To maintain network stability and CPU utilization balance across the dual cores of the ESP32-S3, the application schedules three core tasks:

1. **WiFi Event Handler (System Priority: High)**:
   - Handled via system-registered events.
   - Monitors connections and initiates reconnect sequences immediately if WiFi signal drops.
2. **HTTP Server Daemon (Priority: Normal - 5)**:
   - Managed by `esp_http_server`.
   - Listens on port 80. Handlers are called synchronously within the HTTP server context.
3. **MicroLink Coordination Worker (Priority: Above Normal - 6)**:
   - Managed by the FreeRTOS task `microlink`.
   - Stack Size: `32768` bytes (allocated from external PSRAM).
   - Driven by a `while(1)` loop containing the `microlink_update(ml)` call. Loop interval uses a small delay (`vTaskDelay(pdMS_TO_TICKS(10))`) to yield execution time to lower-priority processes.

---

## 3. Network Packet Encapsulation & Routing

The integration of WireGuard and lightweight IP (lwIP) allows standard IP packets to route transparently over the VPN interface:

```
[ Incoming Payload over Tailscale IP ]
                 │
                 ▼
     [ Decrypt (WireGuard Core) ]
                 │
                 ▼
        [ lwIP Virtual Interface ]
                 │
                 ▼
  [ ESP-IDF Network Router (Virtual) ]
                 │
                 ▼
        [ HTTP Server Endpoint ]
                 │
                 ▼
       [ Send WoL Magic Packet ]
                 │
                 ▼
  [ Socket Broadcast to Physical LAN ]
```

- **WireGuard Core**: Encapsulates outgoing UDP packets and decapsulates incoming packets. Keys are negotiated via the Tailscale Control Plane and updated dynamically when peers roam.
- **lwIP Integration**: Defines a virtual network interface (netif). Packets intended for the Tailscale private network (100.x.y.z subnet) are automatically routed through this virtual interface.

---

## 4. Cryptographic Engine & NaCl Box

Tailscale coordination and WireGuard tunnels utilize asymmetric cryptography:
- **x25519 Elliptic Curve DH**: Used for public/private key pairs and generating shared secrets.
- **NaCl Box (Chacha20-Poly1305)**: Encrypts control and discovery packets exchanged with the control plane and other nodes.
- **MbedTLS**: Handles SSL/TLS certificate validations and security handshakes when checking into the Tailscale coordination server over HTTPS.

---

## 5. Wake-on-LAN Magic Packet Details

The Magic Packet is a standard protocol framework that operates at the Data Link Layer (Layer 2) of the OSI model but is sent via UDP (Layer 4) for ease of transmission:
- **Payload Construction**: Contains 6 bytes of `0xFF` followed by 16 repetitions of the target NIC's 48-bit physical MAC address.
- **Destination**: Broadcast address `255.255.255.255` or subnet broadcast (e.g. `192.168.1.255`) on Port 9.
- **Socket Options**: Open UDP socket -> set `SO_BROADCAST` to 1 -> perform write -> close socket.
