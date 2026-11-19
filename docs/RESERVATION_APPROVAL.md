# Reservation approval mode and no-show fee (C8)

Status: built on branch `feat/w4-c8-approval`, migration `0055_reservation_approval`.

## Approval mode

Each plug has `reservation_mode`: `instant` (default, a booking is `BOOKED` at once) or
`approval` (a booking is a `PENDING` request until the tenant's CPO answers it).

| From | Event | To | Driver notice |
| --- | --- | --- | --- |
| (new) | booking on an approval-mode plug | `PENDING` | `reservation_requested` (CPOs get `reservation_needs_approval`) |
| `PENDING` | CPO `POST /api/cpo/reservations/{id}/approve` | `BOOKED` | `reservation_approved` |
| `PENDING` | CPO `POST /api/cpo/reservations/{id}/decline` | `DECLINED` | `reservation_declined` |
| `PENDING` | driver (or operator) `POST /api/reservations/{id}/cancel` | `CANCELLED` | operator cancel only |
| `PENDING` | still pending at `start_at` (lazy, `expire_lapsed_reservations`) | `EXPIRED` | none |

Rules:

- `PENDING` blocks overlapping bookings and counts toward `MAX_UPCOMING_RESERVATIONS_PER_USER`.
- `PENDING` never authorizes a start and never blocks one: the session-start gate, the walk-up
  warning, the scheduled-start check and the reservation-start janitor all stay `BOOKED`-only.
- An approval-mode request must start in the future (400 otherwise), since it expires at `start_at`.
- Approve, decline and cancel lock the plug row, then the reservation row (the order booking and
  session start use), and lazily expire lapsed holds first, so an approve racing the driver's
  cancel settles exactly once.
- Approve and decline are tenant-scoped (404 for another tenant's reservation) and a `cpo_viewer`
  gets 403 from the global read-only guard.

## No-show fee (optional)

- `plugs.no_show_fee_coins`, default 0 (no fee), set in the CPO charger edit form. The API
  rejects a value above `RESERVATION_NO_SHOW_FEE_MAX_COINS` (env, default 100) with 400.
- The fee in force when a booking is made is snapshotted onto `reservations.no_show_fee_coins`
  and shown in the reserve dialog and the booking notice. Later plug edits do not change it.
- Charged only by the reaper sweep `reap_reservation_no_shows_once`
  (`services/reservations.py`, called every 60 s from `SessionReaperService`), never by the lazy
  `expire_lapsed_reservations` UPDATE.
- Candidates: `BOOKED` or `EXPIRED`, no session, fee > 0, not yet settled, and the no-show grace
  (`RESERVATION_NO_SHOW_GRACE_MIN`, 15) lapsed within the last
  `RESERVATION_NO_SHOW_SWEEP_MAX_LAG_MIN` (env, default 10) minutes. A window that lapsed while
  the backend was down is never charged.
- Each candidate is settled once, charged or skipped (`reservations.no_show_checked_at`), with
  `ledger_transactions.reservation_id` UNIQUE as the idempotency key. Lock order: user row, then
  reservation row.
- Skipped when any signal says the charger may have been unusable: plug `OFFLINE` or
  `MAINTENANCE`, gateway not live, plug not powered or its `powered_since` later than `start_at`,
  a `GATEWAY_BOOT` or `PLUG_UNREACHABLE` event since `start_at`, or any session on the plug
  overlapping the grace window.
- Amount: `min(snapshot, server cap, available balance)`, where available = balance minus active
  session holds. Never negative; nothing happens when that is 0.
- Ledger type `reservation_no_show_fee`; the driver gets `reservation_no_show_fee`, which is on
  the non-suppressible push allowlist (billing).
- A request that lapsed while `PENDING` is stamped settled at expiry, so it is never charged. A
  booking past its grace can no longer be cancelled (409, already expired).

## Known limits

- The fee is a driver debit only. It is not counted in CPO earnings, payouts or GST invoices.
- The driver-facing copy says "15 minutes"; it does not read `RESERVATION_NO_SHOW_GRACE_MIN`.
- There is no outage history table, so "offline in the window" is inferred from current liveness,
  `powered_since` and gateway events.
