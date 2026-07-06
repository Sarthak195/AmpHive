# AmpHive Secret Rotation Runbook

This guide documents the procedures for rotating all core secrets within the AmpHive PaaS infrastructure. 

---

## 1. Tapo Account Credentials Rotation

If the TP-Link/Tapo password is changed or compromised:

1. Update the `.env` file on both the local developer PC and the GCP VM:
   ```ini
   TAPO_PASSWORD=<new-password>
   ```
2. Redeploy the stack from the local machine:
   ```powershell
   .\deploy\scripts\deploy.ps1
   ```
3. Verify that the plug still toggles successfully (e.g. via direct plug control endpoints or the CPO portal).

---

## 2. DuckDNS Token Rotation

If the DuckDNS token needs rotation:

1. Log in to [duckdns.org](https://www.duckdns.org) and click **recreate token**.
2. Run the update script on the VM with the new token:
   ```bash
   DUCKDNS_TOKEN=<new-token> ~/amphive/scripts/setup_duckdns.sh
   ```
3. Check the log file to confirm the update succeeded:
   ```bash
   cat ~/duckdns/duck.log
   # Expected output: OK
   ```

---

## 3. WireGuard Keypair Rotation

To rotate the WireGuard keys on both the GCP VM and the Developer PC:

### 3.1 Generate Fresh Keypairs
Run on the local machine using Python or `wg` CLI:
```powershell
# Generate PC keypair
C:\Program Files\WireGuard\wg.exe genkey | Out-File -Encoding ascii pc_private.key
cat pc_private.key | C:\Program Files\WireGuard\wg.exe pubkey | Out-File -Encoding ascii pc_public.key

# Generate VM keypair (can also do directly on the VM via SSH)
# wg genkey | tee vm_private.key | wg pubkey > vm_public.key
```

### 3.2 Update local client config
Edit `deploy/config/amphive_tunnel.conf`:
```ini
[Interface]
PrivateKey = <NEW_PC_PRIVATE_KEY>
Address = 10.10.0.2/24

[Peer]
PublicKey = <NEW_VM_PUBLIC_KEY>
Endpoint = <VM_PUBLIC_STATIC_IP>:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```

### 3.3 Update remote server config
Edit `/etc/wireguard/wg0.conf` on the VM:
```ini
[Interface]
Address = 10.10.0.1/24
ListenPort = 51820
PrivateKey = <NEW_VM_PRIVATE_KEY>

[Peer]
PublicKey = <NEW_PC_PUBLIC_KEY>
AllowedIPs = 10.10.0.2/32, 192.168.1.0/24
```

### 3.4 Restart WireGuard
- On VM:
  ```bash
  sudo systemctl restart wg-quick@wg0
  ```
- On PC: Deactivate and re-activate the tunnel in the WireGuard Windows client app.
- Verify connectivity: `ping 10.10.0.2` from VM, `ping 10.10.0.1` from PC.

---

## 4. PostgreSQL Database Password Rotation

To rotate the live PostgreSQL database password:

1. Execute the `ALTER USER` query inside the running container on the VM:
   ```bash
   sudo docker exec -it amphive-db psql -U postgres -c "ALTER USER postgres WITH PASSWORD '<new-strong-password>';"
   ```
2. Update the `.env` on the local machine and remote VM with the new password:
   ```ini
   POSTGRES_PASSWORD=<new-strong-password>
   DATABASE_URL=postgresql+asyncpg://postgres:<new-strong-password>@db:5432/amphive
   ```
3. Redeploy the application to update backend connection strings:
   ```powershell
   .\deploy\scripts\deploy.ps1
   ```
4. Verify backend health by checking:
   ```bash
   curl http://<VM_IP>:8000/api/health
   # Expected: {"status": "ok"} or similar green status
   ```

---

## 5. Taking MQTT off the Public Internet

To restrict MQTT broker access to the WireGuard / Tailscale overlay networks:

1. Confirm the Tailscale overlay IP of the VM:
   ```bash
   tailscale ip -4
   # Expected e.g.: 100.87.241.70
   ```
2. Set the overlay bind IP in `.env`:
   ```ini
   MQTT_BIND_IP=100.87.241.70
   ```
3. Redeploy the application stack to bind Mosquitto to this IP:
   ```powershell
   .\deploy\scripts\deploy.ps1
   ```
4. Update the GCE firewall rule to remove port `1883` from the allowed public ports:
   ```powershell
   gcloud compute firewall-rules update allow-amphive-ports --allow tcp:80,tcp:8000
   ```
5. Verify public access is blocked from off-overlay networks:
   ```powershell
   nc -vz <VM_PUBLIC_IP> 1883
   # Expected: Connection timeout / failure
   ```
