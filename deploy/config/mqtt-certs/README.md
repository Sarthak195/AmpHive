# MQTT broker TLS certificates

Self-signed CA + broker server cert for the MQTT TLS listener (port 8883),
added 2026-07-08. The gateway firmware embeds `ca.crt` and validates the
broker's cert against it (chain + IP SAN); the ESP does **not** check cert
dates (`CONFIG_MBEDTLS_HAVE_TIME_DATE` off), so no clock/SNTP is required.

| File | Secret? | Tracked | Used by |
|------|:-------:|:-------:|---------|
| `ca.crt` | no | ✅ | firmware embed (`firmware/main/certs/mqtt_ca.crt`) + mosquitto `cafile` |
| `server.crt` | no | ✅ | mosquitto `certfile` |
| `ca.key` | **yes** | ❌ gitignored | re-signing new server certs only |
| `server.key` | **yes** | ❌ gitignored | mosquitto `keyfile` |

## Regenerating

```bash
MQTT_TLS_SAN_IP=100.87.241.70 bash deploy/config/gen_mqtt_certs.sh
```

The `SAN_IP` **must** be the overlay address the gateway dials
(`mqtts://<ip>:8883`) — mbedTLS checks it against the cert's IP SAN. If the
VM overlay IP changes, regenerate (delete the old keys first), rebuild +
re-OTA the firmware (new CA), and redeploy.

## Deployment

`deploy.ps1` transfers `ca.crt`, `server.crt`, and `server.key` to the VM and
mounts them into the mosquitto container. The private keys live only here
(local) and on the VM — losing `ca.key` means you can't issue new server
certs without re-rolling the CA (and re-flashing every gateway), so keep it
safe.

## Rollout safety

The broker keeps the plaintext `1883` listener during the transition, so a
gateway whose TLS image fails to connect rolls back (OTA bootloader rollback)
to a working plaintext image. Only close `1883` (bind internal-only) once
every gateway is confirmed on `8883`.
