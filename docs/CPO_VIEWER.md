# Read-only CPO viewer (F10)

A `cpo_viewer` account opens one tenant's CPO console and can look at
everything in it, but cannot change anything. It exists so a demo login can be
shared (for example with a recruiter) without handing out control of the real
gateway and plug.

## Why a new role, not a flag

A new `user_role` enum value fails closed: every existing role check
(`require_role("cpo", "admin")`, `require_role("admin")`, the
`UserRole.CPO` branches in the session reads) already refuses it. Only routes
that were deliberately widened admit it. A `tenant_role` column next to `cpo`
would have failed open on any forgotten route.

## Enforcement (backend)

1. **Global write guard.** `services/auth.get_current_user` calls
   `enforce_viewer_read_only`: a `cpo_viewer` on any method other than
   GET/HEAD/OPTIONS gets `403 {"detail": "read_only_demo"}`. The only exempt
   write is `POST /api/auth/logout` (exact method and path). A direct call
   with no request counts as a write. Every authenticated route inherits
   this, including driver-side writes, password change, logout-everywhere and
   account deletion.
2. **CPO gate.** `routers/cpo/_common.require_cpo_tenant` admits `cpo_viewer`
   and repeats the read-only check on its own. `GET /api/cpo/profile` and
   `GET /api/cpo/firmware-releases` are widened the same way. Admin routes
   never admit it. Session detail and telemetry reads
   (`_assert_session_viewer`) admit a viewer of the owning tenant.
3. **Side-effect-free GETs.** GETs that lazily persist reservation expiry
   (`/api/cpo/reservations`, `/api/reservations/my`,
   `/api/plugs/{id}/reservations`, and `/api/plugs/available` plus
   `/api/plugs/{id}` through `_reservation_fields_for_plugs`) skip the commit
   for a viewer.
   `GET /api/sessions/{id}/invoice` issues an invoice on first call and its
   HTML prints the driver's name and email, so a viewer always gets the
   missing-session 404 (same status and body for its own tenant's, another
   tenant's or a missing session; decided before any lookup).
   A private-group plug the viewer has not joined answers the route's own
   missing-plug 404 (not 403) on `/api/plugs/{id}` and its sub-reads and on
   `/api/plugs/{id}/reservations`, so a viewer cannot probe plug ids.
4. **Socket.io.** A viewer may connect and receive pushes; `subscribe_session`
   (which raises the plug's telemetry rate over MQTT) is refused with
   `read_only_demo`.
5. **Privacy.** `routers/cpo/_common.py` holds the three viewer helpers, each
   a no-op for a cpo/admin:
   - `redact_for` masks emails (`d***@example.com`), phone numbers, IPv4
     addresses (`[ip hidden]`), `hint=`/`claim_code_tail=` values and the
     hint in a promo top-up note ("Promo code ...[hidden]"), in whole
     fields and inside free text: sessions list and CSV, disputes (reason and
     resolution note), audit log (actor and detail), event feed `detail`
     (a driver problem report puts its description there), group members,
     reservations, top-ups, plug reports (description and resolution note),
     payout notes, promo codes (`created_by_email` and `note`).
   - `redact_name_for` shows a driver's name as initials ("Sarthak Jain" to
     "S. J.", a one-word name such as an email local part to "S."): group
     members `full_name`, CPO reservations `user_name`.
   - `hide_for` returns null for plug `local_ip`, `latitude` and `longitude`
     (`/api/cpo/plugs`), gateway `vpn_ip` (`/api/cpo/gateways`) and promo
     `code_hint` (the console shows "Hidden").
   A private group's `access_code` is never returned to a viewer (it is a
   credential).
   Gateway log text (`GET /api/cpo/gateways/{id}/logs`) is replaced with
   "(log text hidden in the read-only demo)" for a viewer, since firmware WARN
   lines carry the home Wi-Fi SSID and the broker URL; id, level and time stay.

`backend/tests/test_cpo_viewer_role.py` walks every route the app serves and
asserts each write returns 403 for a viewer; a new unauthenticated write
route must be added to its `PUBLIC_WRITES` list on purpose.

## Frontend

`frontend/src/utils/roles.js` is the one role helper. `hasCpoConsole` admits
`cpo`, `admin` and `cpo_viewer` to the CPO shell; `isReadOnlyViewer` makes
`CpoLayout` show a "Read-only demo" banner and add `.console-readonly`, which
hides every control marked `data-write`. The API client turns the
`read_only_demo` 403 into a readable sentence for anything not hidden.

## Creating the demo login

Status: seeded in production on 2026-11-19 (one `cpo_viewer` on tenant 1, the owner's tenant). Its credentials are kept outside the repo. Verified in production as that viewer: plug `local_ip` and coordinates null, gateway log text replaced by the placeholder, no raw email or IP in sessions, events, audit, meter or promo responses, the commissioning checklist readable with no IP, and a write attempt answers 403 `read_only_demo`. Still open, viewer-adjacent: public plug reviews show the first word of `full_name` to everyone.

Operator only, after migration `0045_cpo_viewer_role` is applied:

```
python -m backend.seed_cpo_viewer --tenant-id <id> --email <address>
```

It refuses to convert an existing driver, CPO or admin account, prints a
random password once, and on re-run resets the password and bumps
`token_version` (old tokens stop working).

## Limits

- Still visible by design: gateway ids (the device MAC, also the route key;
  claiming needs an admin-minted code), the tenant's own seller legal name and
  GSTIN on invoices, firmware release URLs (public bucket), and names written
  inside CPO free text. Public plug reviews show the first word of a driver's
  name to everyone (not a viewer issue, see docs/TODO.md).
- A new field with personal or network data must go through one of the three
  helpers above; nothing masks it automatically.
- Downgrade of 0045 is a no-op (Postgres cannot drop an enum value); tag a
  rollback image built from this revision before deploying.
- An admin can set a user's role to `cpo_viewer` through the admin API; a
  viewer without a tenant gets 400 on the tenant-scoped CPO routes.
