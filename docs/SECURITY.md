# AmpHive — Security Notes

*Verified against source on 2026-06-20. This is a developer-facing inventory of
known security gaps, not a formal audit. Items are roughly ordered by severity.*

> Several real secrets are committed to the git history. Even after removing them
> from `HEAD`, they remain in history and must be **rotated**, not just deleted.

---

## 1. Committed secrets (rotate these)

| File (tracked in git) | Secret |
|-----------------------|--------|
| `deploy/config/amphive_tunnel.conf` | WireGuard **private key** + peer public key |
| `setup_duckdns.sh` | Live **DuckDNS token** (contradicts `deploy_guide.md`, which says it must never be committed) |
| `tools/local_tapo_test.py`, `tools/relay_server.py`, `tools/turn_on.py`, `tools/turn_off.py` | Hard-coded **Tapo account email + password** |
| `deploy/scripts/deploy.ps1`, `start-vm.bat`, `gcp_migration_runbook.md` | DB password `amphive_db_admin` (plaintext) |

✅ **Good news:** the application `.env` files are **gitignored** (`.gitignore`
lines 5–9) and are *not* committed. Backend Razorpay/JWT/Tapo secrets live only
in the local/VM `.env`, not in the repo.

**Remediation:** move the Tapo credentials and DuckDNS token to env vars / a
local untracked config; remove `amphive_tunnel.conf` from tracking (keep a
`.example` with placeholders); rotate the WireGuard keypair, the DuckDNS token,
the Tapo account password, and the DB password; scrub git history if feasible.

## 2. Default / weak credentials

- `JWT_SECRET_KEY` default is the literal `amphive-dev-secret-change-in-production`.
  If unset in prod, **all JWTs are forgeable**. Set a strong secret in `.env`.
- Cloud SQL user is `postgres` with a known password string.

## 3. Open / unauthenticated surfaces

- **MQTT broker is anonymous + no TLS** (`mosquitto.conf`: `allow_anonymous true`)
  and the firewall opens **1883 to `0.0.0.0/0`**. Anyone on the internet can
  publish/subscribe — including sending plug `ON`/`OFF` commands. Confidentiality
  was *intended* to come from running MQTT inside the overlay, but the broker is
  also exposed publicly. **Lock down**: bind to the overlay interface, require
  auth, and/or remove the public firewall rule.
- **`POST /api/gateways/register` and `POST /api/plugs/register` require no auth** —
  anyone can register gateways/plugs.
- **CORS is fully open** (`allow_origins=["*"]`).
- **`/api/payments/webhook`** is unauthenticated by design but HMAC-gated; it
  currently only logs, so there's no auto-credit abuse path yet.

## 4. AuthZ gaps

- **No role enforcement.** `role` is carried in the JWT but never checked; every
  authenticated user can call every JWT-protected route. The documented
  admin/CPO/driver model is not implemented.
- **SSE auth gap.** The frontend opens `/api/sessions/live/{id}` with
  `EventSource`, which can't send the `Authorization` header. The code intends a
  `?token=` query param but doesn't add it. If/when the SSE route is enforced,
  this connection will fail; today it relies on the route being reachable.

## 5. Data-integrity gaps

- **Wallet balance updates are not atomic** (`coin_balance += …` on the ORM
  object, no row lock / atomic UPDATE). Concurrent top-ups + debits can race and
  corrupt the balance despite the ledger.

## 6. Operational notes

- The **VM public IP is ephemeral** and is recorded inconsistently across docs
  (`35.200.131.98`, `34.100.200.152`, and others). The committed
  `amphive_tunnel.conf` endpoint will break whenever the VM IP changes. Prefer a
  stable hostname (DuckDNS) or a static IP, and always re-check with `gcloud`.
- **Firmware control plane defaults to Tailscale's public servers**, not the
  self-hosted Headscale, unless the host constants are overridden.

---

## Quick remediation checklist

- [ ] Rotate WireGuard keys, DuckDNS token, Tapo password, DB password.
- [ ] Remove committed secrets from tracking; add `.example` templates.
- [ ] Set a strong `JWT_SECRET_KEY` in every environment.
- [ ] Restrict the MQTT broker (auth + bind to overlay; drop public 1883 rule).
- [ ] Add auth to `gateways/register` / `plugs/register`.
- [ ] Restrict CORS to the known frontend origin(s).
- [ ] Implement role checks for CPO/admin actions.
- [ ] Make wallet credit/debit atomic (row lock or `UPDATE ... SET balance = balance + :n`).
- [ ] Fix SSE auth (signed short-lived token in the query string).
</content>
