# Charge-complete detection (F1)

AmpHive notices when the car stops drawing power, tells the driver once, and,
only if the driver opted in for that session, stops the session and frees the
plug.

## Detection

Code: `backend/services/mqtt/charge_full.py` (`ChargeFullDetector`), fed from
`MQTTTelemetryMixin._persist_telemetry` (`backend/services/mqtt/telemetry.py`).

Only real power samples count: a live telemetry frame (not an `offline: true`
resync replay) that carries a `watts` field, is attributed to the plug's ACTIVE
session and reports the relay ON. A frame with the relay OFF restarts the idle
timer. Missing frames are never zero-filled: time advances only between the
timestamps of samples that arrived.

The detector fires once per session when all of these hold:

1. the session has drawn at least `CHARGE_FULL_ACTIVE_W` once (read from the
   persisted `charging_sessions.peak_power_w`);
2. the sample is at least `CHARGE_FULL_GRACE_MIN` after the session started;
3. power then stays below `CHARGE_FULL_IDLE_W` continuously for
   `CHARGE_FULL_MINUTES`. Any sample at or above the idle threshold restarts
   the timer, and so does a gap between samples longer than the reaper's stale
   window (`SESSION_STALE_TIMEOUT_SEC`, default 300 s: the gateway was offline).

| Env var | Default | Why |
| --- | --- | --- |
| `CHARGE_FULL_ACTIVE_W` | 200 | Far above EVSE standby/pilot draw (a few W), far below the smallest real AC charge (6 A x 230 V, about 1.4 kW). A car that never really charged can never look full. |
| `CHARGE_FULL_IDLE_W` | 30 | Above standby draw and the P110 noise floor, below any real charging. |
| `CHARGE_FULL_GRACE_MIN` | 5 | Cars often pause while they handshake after the relay closes. |
| `CHARGE_FULL_MINUTES` | 10 | Rides out short pauses (car-side balancing, preconditioning cycles). |

The knobs live in `backend/services/mqtt_manager.py` and are read at call time.

## What happens when it fires

1. The live telemetry snapshot carries `charge_full: true` until the session
   ends, so the session monitor shows a "Your car looks fully charged" banner.
2. One `notify()` to the driver (type `charge_full`, bell + web push). On a session with an idle fee (C1, wave 4 R2, not deployed) the notice is `idle_fee_notice` instead, "Charging done, please stop and unplug", also always pushed; see [IDLE_FEE.md](IDLE_FEE.md).
3. Only when `charging_sessions.auto_stop_when_full` is true: the session is
   stopped with `finalize_charging_session(db, session_id,
   expected_user_id=<driver>, reason="auto-stopped: charge complete")`, the same
   call `POST /api/sessions/stop` makes (relay OFF, bill, free the plug, receipt
   notification and bill email). No new actuation path; nothing is ever
   switched ON. The "looks full" notice only says auto-stop is on; the driver
   is told the session stopped by finalize's own receipt notification. If the
   stop raises, later idle live frames retry it, at most once per 60 s, while
   the session is ACTIVE and opted in (retry state is in memory).

## Opt-in

`POST /api/sessions/start` and `POST /api/sessions/queue` accept
`auto_stop_when_full` (bool, default false). A queued or scheduled charge
stores it on `queued_charges.auto_stop_when_full` and the reaper passes it to
the session it starts. The driver sets it with the "Stop automatically when the
car is full" toggle in the charge setup modal (default off). Migration
`0044_session_auto_stop_full` adds both nullable columns (NULL = off).

## Restarts

Detector state is in memory. A backend restart clears it: the idle timer starts
over, so detection is only delayed (the "has charged" check uses the persisted
peak power). Since migration 0051 (wave 4 R2, built, not deployed) the notice
itself is claimed once per session on `charging_sessions.full_notified_at`, so a
session that had already fired is not notified a second time after a restart
(before 0051 it could repeat one notification); an auto-stopped session is no
longer ACTIVE, so it is never stopped twice.

## Known limits

- Car-side scheduled charging, preconditioning or a car that pauses for more
  than `CHARGE_FULL_MINUTES` mid-charge can look full. That is why the stop is
  opt-in per session and the notification is worded "looks".
- Firmware older than 2.8.0 may publish a frame for a failed plug poll; such a
  frame cannot be told apart from a real 0 W reading.

## Local demo

`tools/fake_plug.py --drop-to-zero-after SECONDS` (or `--taper-after SECONDS`,
which ramps down to 0 W over 300 s) keeps the relay ON while the simulated car
stops drawing power.
