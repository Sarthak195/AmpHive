# MQTT broker TLS certificates

Self-signed CA + broker server cert for the MQTT TLS listener (port 8883),
added 2026-07-08. The gateway firmware embeds `ca.crt` and validates the
broker's cert against it (chain + SAN matching the dialed host: DNS SAN
`mqtt.amphive.app` for fw ≥ 2.3.0, IP SAN for older raw-IP firmware); the ESP
does **not** check cert dates (`CONFIG_MBEDTLS_HAVE_TIME_DATE` off), so no
clock/SNTP is required.

| File | Secret? | Tracked | Used by |
|------|:-------:|:-------:|---------|
| `ca.crt` | no | ✅ | firmware embed (`firmware/main/certs/mqtt_ca.crt`) + mosquitto `cafile` |
| `server.crt` | no | ✅ | mosquitto `certfile` |
| `ca.key` | **yes** | ❌ gitignored | re-signing new server certs only |
| `server.key` | **yes** | ❌ gitignored | mosquitto `keyfile` |

## Regenerating

```bash
# Server cert only, SAME CA (normal case — new SAN, e.g. the DNS name):
RESIGN_SERVER=1 MQTT_TLS_SAN_IPS=100.87.241.70,8.231.81.12 \
  MQTT_TLS_SAN_DNS=mqtt.amphive.app bash deploy/config/gen_mqtt_certs.sh
```

The SANs **must** carry every address a gateway may dial (`mqtts://<host>:8883`)
— mbedTLS checks the dialed host against the cert SANs. Keep the legacy IP
SANs alongside the DNS SAN until the whole fleet is on fw ≥ 2.3.0. **Never
regenerate the CA** unless you intend to re-flash/OTA every gateway — the CA is
embedded in deployed firmware; use `RESIGN_SERVER=1` to reissue only the
server cert. See `deploy/docs/mqtt_dns_rollout.md` for the full rollout order.

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
