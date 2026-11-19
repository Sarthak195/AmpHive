# Notification preferences and quiet hours (C7)

Per-user control over which notices reach the phone as a Web Push, plus three
new session notices. Built on the existing `notify()` pipeline; no new service,
no new dependency.

## What is gated

`services/notifications.notify()` always writes the in-app feed row and emits
the live Socket.io event. Only the Web Push step is gated (notify() sends no
email). The decision lives in `services/notification_prefs.py`:

1. **Always pushed** (`always_pushed`): severity `critical` or `warning`, or a
   type in `ALWAYS_PUSH_TYPES`. Decided before any DB read, ignores toggles and
   quiet hours:
   - billing receipts and refunds: `session_stopped` (also carries safety
     cutoffs and the current-cap stop), `topup_credited`, `topup_refunded`,
     `promo_redeemed`
   - safety and faults: `orphan_off`, `charger_offline`, `unmetered_consumption`
   - operator alerts: `gateway_offline`, `gateway_online`, `gateway_fault`
     (carries OVERCURRENT_CUTOFF, THERMAL_CUTOFF, UNAUTHORIZED_ON, OTA_FAILED)
   - F1: `charge_full`
   - charging routines: `routine_started`, `routine_disabled`
2. **Per-type toggle** (`SUPPRESSIBLE_TYPES`, default on): `session_started`,
   `nearly_done`, `power_dropped`.
3. **Quiet hours**: any other push (the toggles above and every other info
   notice, for example `rate_changed`, `plug_available`, `reservation_*`,
   `queued*`) is dropped while inside the window. Suppressed pushes are not
   queued for later.

If the prefs cannot be read, the push goes out (defaults).

## Storage and API

- `users.notification_prefs` JSONB, nullable; NULL = defaults. Migration
  `0048_notification_prefs`.
- `GET /api/me/notification-prefs` returns
  `{push: {session_started, nearly_done, power_dropped}, quiet_hours: {enabled, start, end}, timezone}`.
- `PUT /api/me/notification-prefs` takes the same shape without `timezone`.
  Unknown keys, an allowlisted type, a time that is not 24-hour `HH:MM`, or an
  enabled window with start == end are rejected with 422. A `cpo_viewer` gets
  403 from the global read-only guard; the GET returns only the caller's own
  toggles and times, so no viewer masking applies.
- The personal data export includes `account.notification_prefs`.

Quiet hours are wall-clock times in the user's tenant timezone (resolved with
`services/pricing._zone`, falling back to `Asia/Kolkata`, then UTC). The window
is half-open `[start, end)`; start later than end wraps across midnight.

## The new notices

| type | when | where |
|---|---|---|
| `session_started` | once per session, after the ON publish succeeds | `services/session_start.begin_active_session` (every start path; a queued auto-start also gets `queued_charge_started`) |
| `nearly_done` | once, first live sample with `0.9 * max_kwh <= energy < max_kwh` | `services/mqtt/session_events.py` via `_persist_telemetry` |
| `power_dropped` | once, after the session peaked at `CHARGE_FULL_ACTIVE_W` or more, live power stays below 50% of the peak and at or above `CHARGE_FULL_IDLE_W` for 3 continuous minutes | same |

Detection uses real live samples only (no offline-resync replays, relay ON,
`watts` present), like the charge-complete detector. A drop to idle restarts
the `power_dropped` timer, so it never duplicates "Your car looks fully
charged". A relay-OFF frame or a gap longer than the reaper's stale window also
restarts it. When the same frame auto-stops the session (wallet or hold
exhausted, energy or time limit), neither notice is sent, and neither is the
"looks fully charged" handling: the driver already has the stop receipt.

The per-session dedupe is in memory and keyed by plug. A backend restart clears
it, so a session can see each notice once more at most.
