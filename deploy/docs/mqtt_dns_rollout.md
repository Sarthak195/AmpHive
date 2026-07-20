# MQTT broker DNS un-pinning rollout (fw 2.3.0)

Goal: gateways dial `mqtts://mqtt.amphive.app:8883` instead of the pinned IP
`8.231.81.12`, so the broker can move machines by flipping one A record — no
firmware change, no reprovisioning.

## What changed

- **Server cert** (same CA — the CA is embedded in every deployed gateway and
  MUST NOT be regenerated): reissued with dual SANs —
  `DNS:mqtt.amphive.app` **plus** the legacy `IP:8.231.81.12` /
  `IP:100.87.241.70`. Old firmware (≤ 2.2.0) keeps validating by IP SAN; new
  firmware validates the DNS SAN. One cert serves both during the transition.
- **Firmware 2.3.0-direct**: compiled default broker URI is now the DNS name;
  esp-tls verifies the URI host against the cert SANs (default hostname
  verification — no `skip_cert_common_name_check`). An NVS `broker_url`
  override still wins, EXCEPT that a stored value containing a legacy pinned
  IP is self-migrated (erased, logged) to the DNS default on boot — so an OTA
  alone moves the fleet.

## Rollout order

1. **Reissue the server cert** (same CA):
   ```bash
   RESIGN_SERVER=1 MQTT_TLS_SAN_IPS=100.87.241.70,8.231.81.12 \
     MQTT_TLS_SAN_DNS=mqtt.amphive.app bash deploy/config/gen_mqtt_certs.sh
   ```
2. **Create the DNS record**: `mqtt.amphive.app` A → `8.231.81.12` (the
   current VM). Verify: `nslookup mqtt.amphive.app 1.1.1.1`.
3. **Deploy the broker** with the new `server.crt`/`server.key`
   (`deploy/scripts/deploy.ps1`; restart the `amphive-mqtt` container).
   Old-firmware gateways reconnect and still validate — the IP SANs are
   unchanged.
4. **OTA the fleet to fw 2.3.0-direct**. Gateways come back dialing the DNS
   name; boot log shows `MQTT broker: mqtts://mqtt.amphive.app:8883
   (compiled default)` (plus a one-time migration warning if NVS held the
   legacy IP).
5. **Later broker moves are invisible**: point the A record at the new
   machine (which has the same CA-signed server cert + broker state); the
   fleet follows on its next reconnect. Keep the old IP reachable until DNS
   TTL expires and stragglers on ≤ 2.2.0 are OTA'd.

## Caveats

- **DNS dependency**: gateways resolve via the DHCP-provided LAN resolver. If
  that resolver is down, fw ≥ 2.3.0 cannot connect (there is no compiled-in
  IP fallback). Workaround per site: set NVS `broker_url` to the raw-IP URI
  (`mqtts://8.231.81.12:8883` still validates thanks to the retained IP SAN —
  but note fw 2.3.0 self-migrates exactly the two *legacy* IPs away, so a
  deliberate raw-IP pin must use a *future* broker IP, or the device must stay
  on ≤ 2.2.0).
- **Do not regenerate the CA** — it would strand every deployed gateway.
  `gen_mqtt_certs.sh` refuses unless the old keys are deleted; use
  `RESIGN_SERVER=1`.
- Backend/agents (`AMPHIVE_BROKER`) still default to the IP in their env
  files; switch them to `mqtt.amphive.app` opportunistically (they trust the
  same CA and the cert now carries the DNS SAN).
