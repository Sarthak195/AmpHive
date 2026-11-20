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
     `promo_redeemed`, `reservation_no_show_fee` (C8, wave 4 R2, not deployed)
   - safety and faults: `orphan_off`, `charger_offline`, `unmetered_consumption`
   - operator alerts: `gateway_offline`, `gateway_online`, `gateway_fault`
     (carries OVERCURRENT_CUTOFF, THERMAL_CUTOFF, UNAUTHORIZED_ON, OTA_FAILED)
   - F1: `charge_full`; C1 (wave 4 R2, not deployed): `idle_fee_notice` (sent in place of `charge_full` on a session with an idle fee)
   - booking changes (C8, wave 4 R2, not deployed): `reservation_approved` (`reservation_declined` is severity `warning`)
   - charging routines: `routine_started`, `routine_disabled`
   - unattended auto-start: `queued_charge_started` (a one-off scheduled or
     outage-queue start; like `routine_started`, the relay energizes with
     nobody at the plug, so it always pushes once, fix round 2026-11-19)
2. **Per-type toggle** (`SUPPRESSIBLE_TYPES`, default on): `session_started`,
   `nearly_done`, `power_dropped`, and (wave 4, `fix/w4-fx3-notices`)
   `rate_changed`, `plug_available`, `reservation_booked`,
   `reservation_started` and the `queued` creation confirmation. A row saved
   before a toggle existed reads it as on. `reservation_cancelled` (an
   operator pulled the booking) and `reservation_ahead` (your session will be
   stopped) are severity `warning`, so they have no toggle and always push.
3. **Quiet hours**: any other push (the toggles above and every other info
   notice) is dropped while inside the window.
   Suppressed pushes are not queued for later. `queued_charge_expired` and
   `queued_charge_failed` are severity `warning`, so they always push.
   `rate_changed` stays muted by quiet hours on purpose: it is a forward-only
   info notice, a tariff boundary often sits at the quiet-hours edge, and the
   `session_stopped` receipt (always pushed) carries the totals.

If the prefs cannot be read, the push goes out (defaults).

## Storage and API

- `users.notification_prefs` JSONB, nullable; NULL = defaults. Migration
  `0048_notification_prefs`.
- `GET /api/me/notification-prefs` returns
  `{push: {session_started, nearly_done, power_dropped, rate_changed, plug_available, reservation_booked, reservation_started, queued}, quiet_hours: {enabled, start, end}, timezone}`.
- `PUT /api/me/notification-prefs` takes the same shape without `timezone`.
  Unknown keys, an allowlisted type, a time that is not 24-hour `HH:MM`, or an
  enabled window with start == end are rejected with 422. A `cpo_viewer` gets
  403 from the global read-only guard; the GET returns only the caller's own
  toggles and times, so no viewer masking applies.
- The personal data export includes `account.notification_prefs`.

Quiet hours are wall-clock times in the user's tenant timezone (resolved with
`services/pricing._zone`, falling back to `Asia/Kolkata`, then UTC). Drivers
are created with no tenant, so a driver's quiet hours are always read in the
default zone (`pricing.DEFAULT_TZ`, `Asia/Kolkata`), never a charger owner's
zone; the API returns that zone name as `timezone`. The window
is half-open `[start, end)`; start later than end wraps across midnight.

## The new notices

| type | when | where |
|---|---|---|
| `session_started` | once per session, after the ON publish succeeds | `services/session_start.begin_active_session`, direct (walk-up) starts only. Every start gets exactly one start notice: a direct start `session_started`, a plain queued or scheduled auto-start only `queued_charge_started` (the sweep passes `announce_start=False`), a charge-routine occurrence only the mandatory `routine_started`. If the routine is deleted, or its link cleared, between the sweep's start and the settle pass, the reaper sends the `queued_charge_started` it held back instead (in-memory hand-off on `SessionReaperService._routine_starts`; a restart in that window loses the notice). Wave 4, `fix/w4-fx3-notices`. |
| `nearly_done` | once, first live sample with `0.9 * max_kwh <= energy < max_kwh` | `services/mqtt/session_events.py` via `_persist_telemetry` |
| `power_dropped` | once, after the session peaked at `CHARGE_FULL_ACTIVE_W` or more, live power stays below 50% of the peak and at or above `CHARGE_FULL_IDLE_W` for 3 continuous minutes | same |

Detection uses real live samples only (no offline-resync replays, relay ON,
`watts` present), like the charge-complete detector. A drop to idle restarts
the `power_dropped` timer, so it never duplicates "Your car looks fully
charged". A relay-OFF frame or a gap longer than the reaper's stale window also
restarts it. When the same frame auto-stops the session (wallet or hold
exhausted, energy or time limit), neither notice is sent, and neither is the
"looks fully charged" handling: the driver already has the stop receipt. Once
the charge-complete detector has judged the session fully charged (that frame
or an earlier one), neither detector is fed again for that session, so
"Almost done" or "slowing down" never follows "Your car looks fully charged"
(`ChargeFullDetector.has_fired`, integrated on `release/2026-11-17-w3`).

The per-session dedupe is in memory and keyed by plug. A backend restart clears
it, so a session can see each notice once more at most.
