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
   `GET /api/sessions/{id}/invoice` issues an invoice on first call, so it
   stays closed to the viewer (403).
4. **Socket.io.** A viewer may connect and receive pushes; `subscribe_session`
   (which raises the plug's telemetry rate over MQTT) is refused with
   `read_only_demo`.
5. **Privacy.** `routers/cpo/_common.redact_for` is the one helper that masks
   emails (`d***@example.com`) and phone numbers for a viewer, in whole fields
   and inside free text: sessions list and CSV, disputes, audit log (actor
   and detail), group members, reservations, top-ups, plug reports, promo
   codes (`created_by_email` and `note`, added at the wave 2 merge). A private
   group's `access_code` is never returned to a viewer (it is a credential).

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

Operator only, after migration `0045_cpo_viewer_role` is applied:

```
python -m backend.seed_cpo_viewer --tenant-id <id> --email <address>
```

It refuses to convert an existing driver, CPO or admin account, prints a
random password once, and on re-run resets the password and bumps
`token_version` (old tokens stop working).

## Limits

- Driver names (not emails) still show where the CPO console shows them.
- Downgrade of 0045 is a no-op (Postgres cannot drop an enum value); tag a
  rollback image built from this revision before deploying.
- An admin can set a user's role to `cpo_viewer` through the admin API; a
  viewer without a tenant gets 400 on the tenant-scoped CPO routes.
