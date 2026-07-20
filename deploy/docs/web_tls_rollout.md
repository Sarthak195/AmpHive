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
CADDY_CPO_DOMAIN=cpo.amphive.duckdns.org  # optional — CPO operator-portal hostname (added 2026-07-20)
ACME_EMAIL=          # optional — Let's Encrypt expiry notices
```

## CPO hostname (2026-07-20)

The CPO operator portal moved to its own hostname,
`cpo.amphive.duckdns.org` (DuckDNS resolves subdomains of the registered
host to the same IP — no extra DNS setup or duck.sh change). When
`CADDY_CPO_DOMAIN` is set in `.env`, `deploy.ps1` emits a second site block
in the generated Caddyfile with the **same** config as the main domain
(gzip, HSTS, `reverse_proxy frontend:80` — the frontend's Nginx already
proxies `/api` + `/socket.io`, and the SPA partitions driver vs. operator
UI by hostname, `frontend/src/utils/appHost.js`). Caddy obtains and renews
a separate Let's Encrypt cert for it automatically. Leave the variable
empty for a single-hostname deploy. Verification additions:

```sh
curl -sI https://cpo.amphive.duckdns.org            # 200, valid LE cert, HSTS header
curl -s  https://cpo.amphive.duckdns.org/api/config # API reachable via the CPO host
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

**Resolution:** DuckDNS recovered ~01:45 UTC (outage ≈ 1 h); Caddy's retry
loop obtained the certificate with no manual action (`notBefore` 2026-07-11
00:46:42 GMT, expires 2026-10-09, issuer Let's Encrypt). Full checklist then
verified: https 200 with validated cert, domain http→https 308, `/api/config`
+ Socket.io open packet over https, CPO login OK, both gateways online
throughout.

**Launch note:** this outage is a live demonstration that DuckDNS is a
single point of failure — get a real domain before launch (SECURITY.md §6
already recommends it).

## Rollback

```powershell
.\deploy\scripts\deploy.ps1 -NoTls   # ships docker-compose.prod.yml again
```

## Follow-ups once verified

- [x] ~~Drop `tcp:8000` from `allow-amphive-ports`~~ **Done 2026-07-11**
  (see log below). The rule was updated, not deleted — 80 must stay for the
  ACME HTTP-01 challenge + redirect. `:8000` remains published by compose
  for VM-local debugging over SSH.
- [x] ~~HSTS~~ **Done 2026-07-11**: the generated Caddyfile's domain block
  now sends `Strict-Transport-Security: max-age=31536000` (deploy.ps1).
- [ ] Trim the plain-`http://` origins out of the backend CORS / Socket.io
  allowlists (`backend/main.py`, `backend/services/socketio_manager.py`) —
  deferred: the bare-IP serve fallback still legitimately serves plain http,
  and same-origin traffic doesn't consult CORS. Revisit with the
  real-domain migration (which is also when bare-IP flips to a redirect).

## Follow-up rollout log (2026-07-11, ~22:30–22:45 IST)

Shipped together with the auth rate limiting (PR #10):

1. `deploy.ps1` (default TLS stack) — rebuilt backend (rate limiting) +
   regenerated the Caddyfile with the HSTS header + `caddy reload`.
2. `gcloud compute firewall-rules update allow-amphive-ports --allow tcp:80`
   — rule now allows tcp:80 only (was `tcp:80,tcp:8000` from `0.0.0.0/0`).

Verified live immediately after:

- `https://amphive.duckdns.org` 200 with
  `Strict-Transport-Security: max-age=31536000` in the response.
- Login rate limit: 12 rapid bad-password logins → exactly 10× 401 then
  429 + `Retry-After: 51`; a real login returned 200 after the window.
- `http://8.231.81.12:8000` from the internet: connection timeout (closed);
  `https://…/api/config` 200 via Caddy; bare-IP `http://8.231.81.12/` 200
  (serve-mode fallback intact).
- Both gateways stayed online through the backend restart (real gateway
  `1cc3abb4fb54` on `1.6.0-direct`, last_seen fresh).

Rollback: re-add the port with
`gcloud compute firewall-rules update allow-amphive-ports --allow tcp:80,tcp:8000`;
HSTS/rate limiting roll back by redeploying the previous commit.
