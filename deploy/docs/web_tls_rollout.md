# Web HTTPS rollout — Caddy TLS front door

*Opened 2026-07-11. Companion to [docs/SECURITY.md §3](../../docs/SECURITY.md)
(web tier served plain HTTP) and [docs/DEPLOYMENT.md §1](../../docs/DEPLOYMENT.md).*

The web tier (SPA + `/api` + `/socket.io`) was served plain-HTTP on `:80`.
The fix: `deploy.ps1` ships `docker-compose.tls.yml` by default — a Caddy
container terminates HTTPS on 80/443 with an auto-renewed Let's Encrypt cert
and is the only public web entrypoint; the frontend container loses its host
port. The Caddyfile is generated on the VM from `CADDY_DOMAIN` / `ACME_EMAIL`
in `.env` on every deploy.

## Pre-rollout steps performed 2026-07-11 (from the dev box)

```powershell
# tcp:443 ingress (additive — allow-amphive-ports keeps 80/8000):
gcloud compute firewall-rules create allow-amphive-https `
  --allow=tcp:443 --source-ranges=0.0.0.0/0 `
  --description="HTTPS to the AmpHive web front door (Caddy TLS terminator)"

# DNS: amphive.duckdns.org -> 8.231.81.12 (static IP) confirmed via 1.1.1.1;
# the VM keeps it current: crontab '*/5 * * * * ~/duckdns/duck.sh'.
```

`.env` additions (also in `deploy/config/.env.template`):

```
CADDY_DOMAIN=amphive.duckdns.org
ACME_EMAIL=          # optional — Let's Encrypt expiry notices
```

## Rollout

```powershell
.\deploy\scripts\deploy.ps1     # TLS stack is the default
```

First run creates `amphive-caddy`, recreates `amphive-frontend` without its
host port, and leaves db/mqtt/backend as-is (compose parity — the tls file
carries the same 8883/passwd/ACL/cert config as prod). Initial cert issuance
takes up to ~a minute after Caddy starts; the cert persists in the
`caddy_data` volume across redeploys.

## Verification checklist (after deploy)

```sh
curl -sI https://amphive.duckdns.org            # 200, valid LE cert
curl -sI http://amphive.duckdns.org             # 308 -> https://
curl -sI http://8.231.81.12                     # 200 — bare-IP serves the app (see incident log)
curl -s  https://amphive.duckdns.org/api/config # public config JSON via Caddy
# Log in on https://, watch a live session (Socket.io upgrades over wss://),
# and confirm gateways stayed connected (CPO /cpo/gateways page — 8883 is
# untouched by this change).
# Caddy's view: sudo docker logs amphive-caddy | grep -i 'certificate obtained'
```

## Incident log — rollout 2026-07-11

The stack deployed cleanly (db/mqtt untouched — broker never restarted, both
gateways stayed online; backend/frontend recreated; caddy created), but the
first Let's Encrypt issuance failed: **DuckDNS's authoritative nameservers
were SERVFAILing globally** ("the domain's nameservers may be malfunctioning"
from LE prod *and* staging; the VM couldn't resolve the domain either). Not a
config problem — LE validates against the authoritative servers, so no cert
until DuckDNS recovers. Caddy auto-retries (backoff, `max_duration` 30 days)
and needs no action once DNS is back.

Two fixes shipped as a result:

1. The generated Caddyfile's `http://` catch-all **serves the app** instead of
   redirecting to the domain — during the outage the bare-IP URL redirected to
   an unreachable https origin, taking the site fully down. Serving by IP
   restores pre-TLS availability whenever the DNS provider is out. Tighten to
   a redirect once a paid/reliable domain replaces DuckDNS.
2. Caddyfile updates now `tee` in place (the file is bind-mounted — `mv` swaps
   the inode and the running container keeps the old config) and `deploy.ps1`
   runs `caddy reload` after `docker-compose up` (compose doesn't restart a
   container for volume-content changes).

Verified through the front door by IP during the outage: SPA 200, `/api/config`,
Socket.io open packet (websocket upgrade offered), CPO login + gateway list
(both gateways online, fresh `last_seen_at`).

**Launch note:** this outage is a live demonstration that DuckDNS is a
single point of failure — get a real domain before launch (SECURITY.md §6
already recommends it).

## Rollback

```powershell
.\deploy\scripts\deploy.ps1 -NoTls   # ships docker-compose.prod.yml again
```

## Follow-ups once verified

- Drop `tcp:8000` from `allow-amphive-ports` (backend's direct plain-HTTP
  port; the SPA reaches the API via the frontend nginx proxy, so nothing
  public needs it). Update the rule rather than deleting it — 80 must stay
  for the ACME HTTP-01 challenge + redirect.
- Consider HSTS (`header Strict-Transport-Security ...` in the generated
  Caddyfile) once https has soaked.
- Trim the plain-`http://` origins out of the backend CORS / Socket.io
  allowlists (`backend/main.py`, `backend/services/socketio_manager.py`).
