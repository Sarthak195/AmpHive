# Security audit — 2026-08-11

Method: **Claude-native fan-out audit** — a recon agent mapped the backend
attack surface, six specialist agents each hunted one vulnerability class
(authentication, multi-tenant isolation/IDOR, wallet/billing/payments, MQTT
ingestion, injection/SSRF/path-traversal, secrets/config), findings were deduped,
and every survivor was re-checked by an independent adversarial "try to refute
it" verifier. 15 agents total; 8 findings, all with confirmed code facts.

Context: this run replaced an attempted Strix scan driven through the local
Claude Code CLI (`strix-cc`). That integration reached a real multi-turn scan
for the first time but could not complete — Claude Code declines to be puppeted
as Strix's autonomous offensive agent (it reads Strix's sandbox framing as
fictional and stops emitting tool calls), and engineering the shim to suppress
that refusal is out of bounds. The fan-out audit below is the legitimate
Claude-native equivalent and is what produced these findings.

The 2026-08-04 multi-model audit (see `security-audit-2026-08-04.md`) had already
fixed a prior batch; those items were excluded from this run so it would surface
only *new* gaps.

## Remediation status

Fixed 2026-08-11 in two PRs: **MQTT ingestion DoS** (`backend/services/mqtt/router.py`)
and **auth hardening** (`auth.py` / `rate_limit.py` / `main.py` / `socketio_manager.py`).

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | MQTT ingestion DoS: a non-object JSON payload (`5`, `[]`, `"x"`, `true`, `null`) on any gateway topic reaches a handler `.get()` → `AttributeError` on the paho network-loop thread → thread dies (paho `suppress_exceptions=False`) → **all** MQTT ingestion freezes for **every** tenant until restart. Any single gateway credential can trigger it. | **HIGH** (live) | **FIXED** — `_on_message` now rejects non-dict payloads before dispatch AND wraps the whole decode+route+handler body in a catch-all `except Exception` (logs + drops one frame), so no current-or-future handler exception can ever kill the loop thread. `BaseException` still propagates. |
| 2 | Google OAuth account pre-hijacking: registration does no email-ownership verification; `google_callback` links Google to a pre-existing password account but never invalidates the old password or bumps `token_version`, so an attacker who pre-registered the victim's email keeps persistent login after the victim takes the account over via Google. | **HIGH** (gated on Google login being enabled — not yet live in prod) | **FIXED** — first-time OAuth link now overwrites `hashed_password` with a fresh unusable hash, bumps `token_version` (revokes the attacker's live sessions), and sets `auth_provider='google'`. The complete fix (email verification at registration) is noted in-code as follow-up. |
| 3 | Per-account login limiter enabled targeted lockout: the limit was keyed on the *victim's* email and enforced as a pre-handler dependency, so an IP-rotating attacker could 429 a victim out **before** the password was checked. | LOW | **FIXED** — the per-account cap moved inside the handler and now counts **only failures**; a correct password is verified first, clears the bucket, and is never rate-limited. Distributed wrong-password brute force is still capped. |
| 4 | `forgot-password` had a per-IP limit but no per-email cap → an IP pool could mailbomb any inbox / trip SMTP reputation. | LOW | **FIXED** — added `FORGOT_PASSWORD_EMAIL_RATE_LIMIT` (default 3/3600) keyed on the submitted email, checked before the account lookup so it is enumeration-safe. |
| 7 | Prod CORS allowlist trusted `http://localhost:*` dev origins with `allow_credentials=true`. | INFO | **FIXED** — localhost removed from the default allowlist; opt-in for dev via `CORS_EXTRA_ORIGINS` (empty in prod). Allowlist is now a single shared source of truth for FastAPI + Socket.io. |
| 5 | WireGuard private key in git history (`deploy/config/amphive_tunnel.conf`, and also `deploy/docs/wireguard_tunnel_setup.md:147`). | LOW | **NOT AN EXPLOITABLE VULN** (per 2026-08-04 assessment: key rotated 2026-07-06, endpoint `34.100.200.152` decommissioned, WireGuard overlay dissolved by the direct-MQTT pivot). Residual: hygiene only. A future history scrub (BFG/git-filter-repo) must cover **both** blob paths. Deferred — a public-repo history rewrite is an explicit-decision operation, not a code fix. |
| 6 | Admin OTA `firmware_url` is a device-directed fetch with no host allowlist. | INFO | **BY DESIGN** — admin-only (CPO is 403'd), the device rejects unsigned images, and admin already controls the whole fleet's firmware. Not remediated. |
| 8 | JWT stored in `localStorage` is exfiltratable under `connect-src 'https:'` **if** arbitrary JS runs in-origin. | INFO | **KNOWN TRADEOFF** (same as 2026-08-04). Requires an XSS/supply-chain precondition that the tight `script-src` is built to block. Moving to an httpOnly cookie is an auth-architecture change tracked separately. |

## Notes

- Nothing was a false alarm on the code facts; the verifier only right-sized severity (it downgraded 6/7/8 from the finders' ratings with explicit reasoning that the exploit doesn't independently function).
- New env vars added to `deploy/config/.env.template`: `FORGOT_PASSWORD_EMAIL_RATE_LIMIT`, `CORS_EXTRA_ORIGINS`.
- All rate limiters remain in-process (per-worker in a multi-worker deploy) — consistent with the existing design; a distributed store is out of scope.
