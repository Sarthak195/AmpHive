# Idle fee (C1)

An opt-in, capped per-minute fee for a car left plugged in after it looks fully
charged. Off by default everywhere: nothing changes until a CPO sets it on a
tariff.

## Terms (per tariff)

| Column | Meaning | Default |
|---|---|---|
| `tariffs.idle_fee_per_min` | coins per whole idle minute past the grace | 0 (off) |
| `tariffs.idle_grace_min` | free minutes after the "looks full" signal | 0, and at least 15 whenever the fee is on |
| `tariffs.idle_fee_cap` | most coins one session can be charged | 0 (must be above 0 when the fee is on) |

Set in the CPO portal (Pricing, tariff form) or `POST/PUT /api/cpo/tariffs`
(`idle_fee_per_min`, `idle_grace_min`, `idle_fee_cap`). The routes refuse a fee
with no cap, or with a grace below 15 minutes. A grace left out when turning
the fee on defaults to 15. The terms resolve through the same chain as the rate
(plug, then group, then tenant default) and are snapshotted onto the session at
start (`charging_sessions.idle_grace_min / idle_fee_per_min / idle_fee_cap`), so
a tariff edit never changes a running session.

Drivers see the terms in the start dialog, in the weekly routine form when a
charger is chosen, and on the live session screen (all read `idle_fee` from
`GET /api/plugs/{id}/tariff-preview`; the live screen shows the plug's current
terms, while billing uses the start snapshot). The live screen hides the terms
and the "unplug within N min" hint on a session with auto-stop when full on.

## What counts as idle

All of these, measured on live telemetry (`services/idle_fee.py`):

1. the session is ACTIVE and the F1 detector already judged the car full
   (`charging_sessions.full_notified_at`, durable across restarts);
2. a live frame reports the relay ON with draw below `CHARGE_FULL_IDLE_W`;
3. telemetry is fresh: a gap of more than `PLUG_POWER_STALE_SEC` between the
   plug's live frames is never counted, and at the stop the open stretch is
   closed at the earlier of the plug's last live frame and the session's last
   attributed frame, never at the stop time.

A relay-ON frame at or above `CHARGE_FULL_IDLE_W` after the "looks full"
signal means the car is drawing again (a car-side pause can look full), so idle
was not continuous since that signal. The session is then voided for the fee
for good (`idle_seconds = -1`, written once): the F1 detector and its notice
fire once per session, so a later idle stretch would have no fresh signal and
no notice, and is never billed. A relay-OFF frame only pauses a stretch.

A session whose driver ticked "Stop automatically when the car is full" never
accrues an idle fee, and nothing is reserved for one.

State lives on the session row and is written on transitions only
(`idle_started_at` opens a stretch, `idle_seconds` banks closed parts, -1
marks a voided session), on the
row the telemetry path already holds locked. No per-frame writes.

## Notice

When the car looks full, a session with a fee gets ONE notice, "Charging done,
please stop and unplug" (type `idle_fee_notice`, in `ALWAYS_PUSH_TYPES` because
it is about money), stating the grace, the rate and the cap. It replaces the
plain `charge_full` notice for that session and uses the same durable claim
(`full_notified_at`), so a restart never repeats it.

The claim is stamped before `notify()` runs, and accrual keys on that stamp. When
`notify()` reports that it could not record the warning (it returns None), the
session's fee is voided for good (`idle_seconds = -1`, the renewed-draw
sentinel), so a fee is never billed for a warning that was never stored.

## Billing

Settled once, in `finalize_charging_session`, in the same transaction and under
the same session lock as the energy debit:

- fee = whole idle minutes past the grace x the per-minute rate, at most the cap;
- then at most what the session's hold has left after the energy debit;
- then clamped to the wallet (`debit_wallet_clamped`);
- written as its own `IDLE_FEE` ledger line (new `tx_type` value, migration
  `0052_idle_fee`), with an existence check so a second line is never written;
  nothing is written for a 0.00 fee;
- `charging_sessions.idle_fee_coins` records it and it is included in
  `coins_spent`, so the dispute route can refund it (its default refund is the
  whole `coins_spent`), payouts pay it to the CPO, and the GST invoice total
  includes it.

The start hold includes the cap: `hold = available` with no energy limit, else
`min(available, energy_cost(max_kwh, max_rate) + idle_fee_cap)`.

The receipt, the session detail route and the stop notification carry
`idle_fee_coins`.

## Known limits

- A car-side pause (scheduled charging, battery conditioning) can look full.
  The 15-minute minimum grace and the void on renewed draw limit this, and the
  fee can be disputed, but a pause longer than the grace that ends with the
  driver stopping the session (no renewed draw seen) is billed as idle.
- The void is measured: only a live frame can void the fee. A car that draws
  again only while telemetry is stale (gateway offline) is not seen, so idle
  resumes after the gap. One live frame at or above the threshold is enough to
  void, which favours the driver.
- The looks-full banner still shows the "unplug within N min" hint on a session
  already voided by a renewed draw (the banner hides itself while the car draws,
  but not after it stops again).
- Once a session stops, the relay is OFF and the plug reads 0 W, so a car left
  on the bay after the session ends cannot be detected and is never charged.
- The live session screen shows the plug's current terms; a CPO edit mid-session
  shows there although the session bills its start snapshot.
- The GST invoice has one line (energy); its total includes the idle fee.
- A weekly routine (C5) or a scheduled charge snapshots the plug's idle terms
  when the run actually starts. The routine form shows the plug's terms when the
  routine is saved, but a fee a CPO adds or raises later is not shown again
  before each run; the driver learns of it from the terms on the live
  session screen and the idle notice, and can dispute it.
- A crash (or a failed void UPDATE) between the notice claim and `notify()`
  loses the warning, and the fee still accrues and settles at finalize. A
  failed `notify()` that returns is handled (the fee is voided); a process
  dying in between is not. The driver can dispute such a fee.
- An unlimited session (no energy limit) reserves the available balance, so a
  session that spends its whole hold on energy has nothing left for the fee.
