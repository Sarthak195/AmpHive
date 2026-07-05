# WireGuard Tunnel Setup Guide — AmpHive Direct Mode

This guide sets up a WireGuard tunnel between the GCP VM (`amphive-vm-in`) and the
developer's Windows PC, allowing the cloud backend to reach a TP-Link Tapo P110
smart plug on the home LAN without an ESP32 gateway.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│             GCP VM  (amphive-vm-in)                   │
│             Public IP: 34.100.200.152                  │
│                                                       │
│  [Docker: amphive-backend]                            │
│      │ connects to 10.10.0.2:80                       │
│      ▼                                                │
│  [WireGuard wg0: 10.10.0.1/24]                        │
│      │ UDP 51820                                      │
└──────┼────────────────────────────────────────────────┘
       │ Encrypted WireGuard Tunnel (Internet)
       │
┌──────┼────────────────────────────────────────────────┐
│      ▼                                                │
│  [WireGuard wg_amphive: 10.10.0.2/24]                 │
│      │ netsh portproxy: 10.10.0.2:80 → 192.168.1.4:80│
│      ▼                                                │
│  [Windows PC — Home Network: 192.168.1.x]             │
│      │                                                │
│      ▼                                                │
│  [Tapo P110 Smart Plug: 192.168.1.4]                  │
└───────────────────────────────────────────────────────┘
```

## WireGuard IP Assignments

| Peer | WireGuard IP | Public IP | Role |
|------|-------------|-----------|------|
| GCP VM | 10.10.0.1 | 34.100.200.152 | Server (listening) |
| Developer PC | 10.10.0.2 | (dynamic, home ISP) | Client (connecting) |

---

## Step 1: GCP VM Setup (Server Side)

### 1.1 Install WireGuard

```bash
# SSH into the VM
gcloud compute ssh amphive-vm-in --zone=asia-south1-a

# Install WireGuard
sudo apt update && sudo apt install -y wireguard

# Enable IP forwarding (already enabled by Docker, but ensure it persists)
echo "net.ipv4.ip_forward=1" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### 1.2 Generate Keys

```bash
# Generate VM keypair
wg genkey | sudo tee /etc/wireguard/server_private.key | wg pubkey | sudo tee /etc/wireguard/server_public.key
sudo chmod 600 /etc/wireguard/server_private.key

# Generate PC keypair (for convenience — can also generate on PC)
wg genkey | sudo tee /etc/wireguard/client_private.key | wg pubkey | sudo tee /etc/wireguard/client_public.key
sudo chmod 600 /etc/wireguard/client_private.key

# Display keys (needed for config files)
echo "=== VM Public Key ===" && cat /etc/wireguard/server_public.key
echo "=== PC Private Key ===" && cat /etc/wireguard/client_private.key
echo "=== PC Public Key ===" && cat /etc/wireguard/client_public.key
```

### 1.3 Create Server Config

```bash
# Read the VM private key
SERVER_PRIVATE=$(sudo cat /etc/wireguard/server_private.key)
CLIENT_PUBLIC=$(cat /etc/wireguard/client_public.key)

sudo tee /etc/wireguard/wg0.conf << EOF
# AmpHive WireGuard Server Config — GCP VM
# Purpose: Tunnel to developer's home network for direct Tapo P110 control
[Interface]
Address = 10.10.0.1/24
ListenPort = 51820
PrivateKey = ${SERVER_PRIVATE}

# Developer's Windows PC
[Peer]
PublicKey = ${CLIENT_PUBLIC}
AllowedIPs = 10.10.0.2/32
EOF

sudo chmod 600 /etc/wireguard/wg0.conf
```

### 1.4 Start WireGuard

```bash
# Start and enable on boot
sudo wg-quick up wg0
sudo systemctl enable wg-quick@wg0

# Verify
sudo wg show
```

### 1.5 Open Firewall Port

```bash
# On your local machine (not SSH), run:
gcloud compute firewall-rules create allow-amphive-wireguard \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=udp:51820 \
    --source-ranges=0.0.0.0/0 \
    --target-tags=amphive \
    --description="Allow WireGuard UDP for AmpHive direct plug tunnel"
```

> **Note**: If the VM doesn't have the `amphive` network tag, omit `--target-tags`
> or use `--target-tags=$(gcloud compute instances describe amphive-vm-in --zone=asia-south1-a --format='value(tags.items[0])')`.

---

## Step 2: Windows PC Setup (Client Side)

### 2.1 Install WireGuard

Download and install from: https://www.wireguard.com/install/

### 2.2 Import Tunnel Configuration

Open the WireGuard application and click **"Add Tunnel" → "Add empty tunnel..."**
or import the pre-generated config file at:
[`deploy/config/amphive_tunnel.conf`](file:///c:/Users/Sarthak/Documents/AmpHive/deploy/config/amphive_tunnel.conf)

The config contains (see [`amphive_tunnel.conf.example`](file:///c:/Users/Sarthak/Documents/AmpHive/deploy/config/amphive_tunnel.conf.example)):

```ini
[Interface]
PrivateKey = <PC-private-key>          # generate with `wg genkey` — NEVER commit a real key
Address = 10.10.0.2/24

[Peer]
PublicKey = <VM-server-public-key>     # from the VM: `wg genkey | tee sk | wg pubkey`
Endpoint = <VM-public-ip>:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```

> ⚠️ **Burned keypair.** An earlier revision of this doc committed a real
> `PrivateKey` (and the matching server `PublicKey`/endpoint). Those values are
> **compromised and in git history** — regenerate the WireGuard keypair on both
> ends and never paste a private key back into this file. Keep the live config
> only in the gitignored `deploy/config/amphive_tunnel.conf`.

### 2.3 Activate the Tunnel

Click **"Activate"** in the WireGuard Windows app.

### 2.4 Set Up Port Forwarding

The backend connects to `10.10.0.2:80` (the PC's WireGuard IP). We need to
forward that to the actual plug at `192.168.1.4:80` on the home LAN.

Open **PowerShell as Administrator** and run:

```powershell
# Forward port 80 from WireGuard IP to the Tapo plug's LAN IP
netsh interface portproxy add v4tov4 listenport=80 listenaddress=10.10.0.2 connectport=80 connectaddress=192.168.1.4

# Verify the rule was added
netsh interface portproxy show all
```

### 2.5 Verify Connectivity

```powershell
# From PC: ping the VM through the tunnel
ping 10.10.0.1

# From GCP VM (SSH): ping the PC through the tunnel
ping 10.10.0.2
```

---

## Step 3: Verify End-to-End

Once both sides are up:

1. **Verify tunnel**: From GCP VM, `ping 10.10.0.2`
2. **Verify port proxy**: From GCP VM, `curl -v http://10.10.0.2:80/app` (should get a Tapo response or connection)
3. **Verify API**: Call the direct mode endpoints:

```bash
# Get a JWT token first
TOKEN=$(curl -s -X POST http://34.100.200.152:8000/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"your@email.com","password":"your-password"}' \
    | jq -r '.token')

# Health check
curl -H "Authorization: Bearer $TOKEN" http://34.100.200.152:8000/api/direct/plug/health

# Turn ON
curl -X POST -H "Authorization: Bearer $TOKEN" http://34.100.200.152:8000/api/direct/plug/on

# Turn OFF
curl -X POST -H "Authorization: Bearer $TOKEN" http://34.100.200.152:8000/api/direct/plug/off
```

---

## Troubleshooting

### Tunnel not connecting
- Check GCP firewall rule: `gcloud compute firewall-rules list --filter="name=allow-amphive-wireguard"`
- Check WireGuard is running on VM: `sudo wg show`
- On Windows, check the WireGuard app shows "Active"

### Port proxy not working
- Verify the rule: `netsh interface portproxy show all`
- Test locally on PC: `curl http://10.10.0.2:80` (should try to connect to the plug)
- Check Windows Firewall isn't blocking port 80 on the WireGuard interface

### Tapo plug not responding
- Verify plug IP in Tapo app: Settings → Device Info → IP Address
- Ensure "Third-Party Compatibility" is enabled: Tapo app → Device → Settings → Third-Party Compatibility → ON
- Try from PC directly: `curl http://192.168.1.4/app` (should get some response)

### Docker container can't reach WireGuard peer
- Docker's default bridge network should route through the host. Verify:
  ```bash
  docker exec amphive-backend ping 10.10.0.2
  ```
- If blocked, check iptables: `sudo iptables -L FORWARD -v -n`
- As a workaround, you can temporarily use `network_mode: host` for the backend container

---

## Cleanup (When ESP32 Board Arrives)

Once the ESP32 gateway is available, disable direct mode:

1. Set `DIRECT_MODE=false` in `.env`
2. Redeploy the backend
3. Optionally stop WireGuard on VM: `sudo wg-quick down wg0`
4. Deactivate the WireGuard tunnel on your PC
5. Remove the port proxy: `netsh interface portproxy delete v4tov4 listenport=80 listenaddress=10.10.0.2`
