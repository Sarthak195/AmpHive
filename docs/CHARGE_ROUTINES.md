# Charge routines (C5)

A driver saves a recurring weekly charge: a plug, weekdays, a local start
time and a mandatory time and/or energy limit (plus the optional "Stop
automatically when the car is full"). Up to 5 per driver. UI: the Charge
routines card on `/account`.

## Not a start path

A routine never switches a plug on. Each due occurrence becomes an ordinary
scheduled `QueuedCharge`, field for field the row `POST /api/sessions/queue`
with `not_before` creates (tenant from the gateway, the snapshot limits,
`auto_stop_when_full` as true or NULL, `expires_at = not_before +
schedule_ttl`). `reap_queued_starts_once` then does all admission, the wallet
hold, billing and the start, exactly as for a one-off scheduled charge.

## Reaper passes (services/routines.py, every 60 s)

1. `materialize_routines` (before the queued-start sweep). For local today and
   local yesterday in the plug's tenant timezone, an occurrence is due when
   its weekday bit is set (Mon=1 .. Sun=64), its start time was reached less
   than `min(5 min, schedule_ttl)` ago, and `last_materialized_date` is
   before that date. The routine row is locked `FOR UPDATE SKIP LOCKED` and
   the date is stamped in the same transaction as the insert, so a double
   tick queues one row. The plug row is then locked `FOR UPDATE` (routine,
   then plug), as in the queue endpoint's scheduled path, so the
   other-driver check below is serialized with it. A missed window (backend
   down) is skipped. Per occurrence:
   - closed or disabled account: stamp, nothing else;
   - queued/scheduled charging off for the plug: stamp and skip; the
     routine stays enabled and the driver gets one `routine_skipped`
     ("paused") notification per outage (`gate_notified`, re-armed by the
     next materialize pass that finds the gate on, whether or not that
     occurrence is queued, so a second outage is noticed too), so it resumes by itself when the gate is back on (the API
     refuses to create or re-enable a routine while the gate is off);
   - the driver already has a WAITING row on that plug: stamp and skip;
   - lost private-group access, another driver's WAITING row on the plug, or
     the driver at the queued-charge cap: a failed occurrence
     (`routine_skipped`, counted);
   - otherwise insert the row and remember it in `last_queued_charge_id`.
2. `settle_routines` (after the queued-start sweep) reads that row once the
   reaper decided it: STARTED resets `consecutive_failures` and sends
   `routine_started` (with the session id); EXPIRED or FAILED counts a
   failure; CANCELLED counts nothing. The third failure in a row turns the
   routine off and sends `routine_disabled`. Re-enabling resets the count.
   `routine_started` is the occurrence's only start notice: the queued-start
   sweep sees that a routine owns the row (`last_queued_charge_id`) and sends
   neither `queued_charge_started` nor C7's `session_started`
   (`begin_active_session(announce_start=False)`). If the routine is
   deleted (or its link cleared) between the sweep's start and the settle
   pass, settle no longer finds it and the reaper falls back to
   `queued_charge_started`, so the start still gets exactly one notice. On a plug outside its
   opening hours (C4) the row stays WAITING until the plug opens or the row's
   TTL lapses; that EXPIRED row counts as one failure. (Both integrated on
   `release/2026-11-17-w3`.)

Creating a routine, re-enabling it, or editing its days or start time on a
plug with opening hours (C4) is refused with 409 `plug_closed` when the plug
is closed at the start time on any selected weekday. When a CPO later edits a
plug's opening hours, every enabled routine whose start time is now closed on
a selected weekday is turned off in the same request (its WAITING row
cancelled) and its owner gets one `routine_disabled` notice. An occurrence
that already started keeps its one `routine_started`.

Creating a routine, re-enabling it, or editing its days or start time stamps
an occurrence whose start already passed (within the window), so a routine
never queues a row whose `not_before` is in the past; it first runs at its
next occurrence.

Turning a routine off or deleting it cancels its row if it has not started.
Account closure deletes the driver's routines.

## API (owner only; another driver's routine is 404)

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/me/routines` | `{routines: [...], max_routines}`; each row has `plug_name`, `weekdays`, `start_time` ("HH:MM", tenant wall-clock), limits, `auto_stop_when_full`, `enabled`, `consecutive_failures`, `last_materialized_date`, `tenant_timezone` |
| POST | `/api/me/routines` | `{plug_id, weekdays 1..127, start_time, max_kwh?, max_duration_seconds?, auto_stop_when_full, enabled}`; 422 without a limit, 409 `routine_limit`, 409 `queue_disabled`, 403 private group |
| PATCH | `/api/me/routines/{id}` | any of the above except `plug_id`; a limit may be cleared with null while another remains |
| DELETE | `/api/me/routines/{id}` | `{id, status: "deleted"}` |

Writes are refused for a cpo_viewer by the global read-only guard; a GET
returns only the caller's own rows and no infrastructure fields.

Migration `0050_charge_routines` adds the `charge_routines` table only
(including `gate_notified`).
