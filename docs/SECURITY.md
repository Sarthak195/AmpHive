# AmpHive — Security Notes

*Verified against source on 2026-07-02. This is a developer-facing inventory of
known security gaps, not a formal audit. Items are roughly ordered by severity.*

> Several real secrets are committed to the git history. Even after removing them
> from `HEAD`, they remain in history and must be **rotated**, not just deleted.

> **Recently fixed** (see [§7](#7-recently-fixed)): RBAC is now enforced on all
> `/api/cpo/*` routes, the unauthenticated `gateways/register` / `plugs/register`
> endpoints were removed, wallet updates are row-locked, and the Razorpay webhook
> credits idempotently. The items in §1–§6 are the gaps that **remain**.

---

## 1. Committed secrets (rotate these)

| File (tracked in git) | Secret |
|-----------------------|--------|
| `deploy/config/amphive_tunnel.conf` | WireGuard **private key** + peer public key |
| `setup_duckdns.sh` | Live **DuckDNS token** (contradicts `deploy_guide.md`, which says it must never be committed) |
| `tools/local_tapo_test.py`, `tools/relay_server.py`, `tools/turn_on.py`, `tools/turn_off.py` | Hard-coded **Tapo account email + password** |
| `deploy/scripts/deploy.ps1`, `scripts/start-vm.bat`, `deploy/docs/gcp_migration_runbook.md` | DB password `amphive_db_admin` (plaintext) |

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
- The PostgreSQL container on the VM uses user `postgres` with the known password
  `amphive_db_admin` (hardcoded in `docker-compose.prod.yml` / `deploy.ps1`).

## 3. Open / unauthenticated surfaces

- **MQTT broker is anonymous + no TLS** (`mosquitto.conf`: `allow_anonymous true`)
  and the firewall opens **1883 to `0.0.0.0/0`**. Anyone on the internet can
  publish/subscribe — including sending plug `ON`/`OFF` commands. Confidentiality
  was *intended* to come from running MQTT inside the overlay, but the broker is
  also exposed publicly. **Lock down**: bind to the overlay interface, require
  auth, and/or remove the public firewall rule.
- **CORS is fully open** (`allow_origins=["*"]`). Restrict to the known frontend
  origin(s) before production.
- **`/api/payments/webhook`** is unauthenticated by design but HMAC-gated. It now
  auto-credits coins on `payment.captured`; abuse via replay is mitigated by the
  HMAC signature check plus idempotency on `razorpay_payment_id`, but a leaked
  `RAZORPAY_WEBHOOK_SECRET` would allow forged credits — keep it secret and rotate
  if exposed.

## 4. AuthZ gaps

- **SSE auth gap.** The frontend opens `/api/sessions/live/{id}` with
  `EventSource`, which can't send the `Authorization` header. The code intends a
  `?token=` query param but doesn't add it. If/when the SSE route is enforced,
  this connection will fail; today it relies on the route being reachable. Fix:
  issue a signed, short-lived token in the query string.

> RBAC across the `/api/cpo/*` surface is now enforced (`require_role`) — see
> [§7](#7-recently-fixed).

## 5. Data-integrity gaps

- Wallet credit/debit is now **row-locked** (`SELECT ... FOR UPDATE`) in the stop,
  verify, and webhook paths — the previous race is closed. Remaining hardening:
  consider a single atomic `UPDATE ... SET balance = balance + :n` and DB-level
  check constraints to prevent negative balances.

## 6. Operational notes

- The **VM public IP is ephemeral** and is recorded inconsistently across docs
  (`35.200.131.98`, `34.100.200.152`, and others). The committed
  `amphive_tunnel.conf` endpoint will break whenever the VM IP changes. Prefer a
  stable hostname (DuckDNS) or a static IP, and always re-check with `gcloud`.
- **Firmware control plane defaults to Tailscale's public servers**, not the
  self-hosted Headscale, unless the host constants are overridden.

## 7. Recently fixed

*Previously listed as gaps here; resolved as of 2026-07-02. Kept for context so
older references don't read as still-open.*

- **RBAC is enforced.** `require_role("cpo","admin")` (`backend/services/rbac.py`)
  gates every `/api/cpo/*` route and checks the live DB role, not just the token.
- **Unauthenticated provisioning removed.** The old `POST /api/gateways/register`
  and `POST /api/plugs/register` are gone; provisioning now happens through the
  tenant-scoped, RBAC-gated `POST /api/cpo/gateways` / `POST /api/cpo/plugs`.
- **Wallet updates are row-locked** (`SELECT ... FOR UPDATE`) in the stop, verify,
  and webhook paths — no more balance race.
- **Webhook auto-credit is idempotent** — dedupes on `razorpay_payment_id` so the
  `/verify` and webhook paths can't double-credit.

---

## Quick remediation checklist

Still open:
- [ ] Rotate WireGuard keys, DuckDNS token, Tapo password, DB password.
- [ ] Remove committed secrets from tracking; add `.example` templates.
- [ ] Set a strong `JWT_SECRET_KEY` in every environment.
- [ ] Restrict the MQTT broker (auth + bind to overlay; drop public 1883 rule).
- [ ] Restrict CORS to the known frontend origin(s).
- [ ] Fix SSE auth (signed short-lived token in the query string).
- [ ] Consider a DB-level non-negative-balance constraint.

Done (2026-07-02):
- [x] Implement role checks for CPO/admin actions (`require_role`).
- [x] Remove/authenticate `gateways/register` / `plugs/register`.
- [x] Make wallet credit/debit atomic (row lock).
</content>
