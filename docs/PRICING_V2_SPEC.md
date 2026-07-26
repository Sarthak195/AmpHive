# Pricing v2 — Time-of-Day Tariffs & Forward-Only Segmented Billing

*Design spec, drafted 2026-07-13. Status: **ALL PHASES 1–4 SHIPPED (2026-07-14).**
Phase 1 = schema + resolution + billing helpers; Phase 2 = billing wired to
`session_cost` + forward-only reprice (telemetry frame hook + reaper backstop) +
`rate_changed` notification + start-time hold at `max_rate_over_window`;
Phase 3 = operator-edit reprice trigger (`mark_tenant_sessions_for_reprice`,
env `AUTO_REPRICE_ACTIVE_SESSIONS`) on tariff/slot edits + PATCH-`/limits`
hold-at-`max_rate_over_window`; Phase 4 = operator slot-editor (`/cpo/tariffs` +
`tariff_slots` CRUD API with overlap validation) and driver current+next price
(`resolve_price_display` → `PlugResponse.price_next_per_kwh`/`price_changes_at`
→ Home ribbon hint). Deployed-safe: a flat tariff resolves no boundary, so it
bills byte-identically until a CPO adds a slot. All phases 1–4 are live in prod
(main @ 3a54377) and deployed.*

Related: [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) "Per-CPO/per-site
tariff model" (🟡) · [MARKET_GAP_ANALYSIS.md](MARKET_GAP_ANALYSIS.md) §1.5/§3 ·
`backend/services/pricing.py` · `backend/database/models.py` `Tariff`. The
driver/operator UI shapes were prototyped in the "High-Voltage Hive" concept
(§04 Operator controls).

---

## 1. Goal & scope

Deliver the client's two pricing asks:

1. **A CPO sets pricing per plug** — already possible (assign a `Tariff` to a
   plug); v2 adds the operator UI and **time-of-day (TOD) schedules** on a tariff.
2. **Different pricing for time of day** + **change pricing mid-charge with a
   notification** — the same underlying mechanism: a session's rate can change
   while it runs (at a TOD slot boundary, or when an operator edits the tariff),
   and the change is **forward-only** with a driver notification.

**In scope:** TOD tariff slots; a rate-resolution function that is time-aware;
segmented (forward-only) billing across all three billing paths; the reprice
triggers (telemetry hook + operator edit + reaper backstop); driver price
transparency (current + next price); the operator tariff/slot editor; a
`rate_changed` notification.

**Non-goals:** retroactive repricing (never); a currency/coin change; GST logic
changes (see §10); dynamic/surge pricing driven by anything other than a fixed
schedule or explicit operator edit; per-**circuit** tariffs (circuits are the
separate caps feature — §5 notes where they slot in later).

---

## 2. Baseline (what exists today)

- **`Tariff`** (`models.py`): `id, tenant_id, name, price_per_kwh Numeric(12,2)`.
  Assigned via `Plug.tariff_id`, `ChargerGroup.tariff_id`, or
  `Tenant.default_tariff_id`.
- **Resolution** (`services/pricing.py resolve_rate_for_plug`), first match wins:
  `plug.tariff → plug.group.tariff → tenant.default_tariff → global COINS_PER_KWH`.
  Returns one 2dp `Decimal`.
- **Snapshot invariant (today):** the resolved rate is written **once** to
  `ChargingSession.rate_coins_per_kwh` at start and **never re-resolved**. All
  three billing paths read that single snapshot:
  - `finalize_charging_session` → `energy_cost(final_energy, rate)`
  - live `TelemetryStore.update` → `cost_coins = energy_cost(energy_kwh, rate)`
    (rate seeded via `start_session(plug_id, rate)`)
  - `_maybe_auto_stop_on_exhaustion` → `accrued_cost = energy_cost(energy_kwh, rate)`

Every path computes **`energy_cost(cumulative_energy, single_rate)`**. That
single multiply is what v2 replaces.

---

## 3. The invariant change

> **Old:** a session's rate is fixed at start; a tariff edit never affects it.
>
> **New:** energy is billed at the rate **in effect when it was consumed**. A
> rate change (TOD boundary or operator edit) is **forward-only** — it applies
> only to energy metered *after* the change and **never re-prices** energy
> already metered.

This is safe by construction for the common case: a **flat tariff** (no TOD
slots, no edit) resolves to one rate for the whole session with no boundary, so
the session has exactly one segment and bills **identically to today**. Legacy
sessions (NULL segment columns, §4) also bill exactly as today. Segmented
billing only ever *adds* segments; it never changes a single-rate result.

---

## 4. Data model

### 4.1 `tariff_slots` (new table — TOD windows on a tariff)

| column | type | notes |
|---|---|---|
| `id` | PK | |
| `tariff_id` | FK → `tariffs.id` `ON DELETE CASCADE`, indexed | |
| `start_min` | `SmallInteger` | minute-of-day, `0..1439`, inclusive |
| `end_min` | `SmallInteger` | minute-of-day, `1..1440`, exclusive; half-open `[start,end)` |
| `price_per_kwh` | `Numeric(12,2)` | the rate inside this window |
| `days_mask` | `SmallInteger` default `0b1111111` | optional weekday bitmask (Mon=bit0…Sun=bit6); `all days` by default |

Rules:
- Windows are **half-open** `[start_min, end_min)` so adjacent slots (e.g.
  `06:00–18:00`, `18:00–22:00`) don't overlap at the boundary — same convention
  as `Reservation` `[start_at, end_at)`.
- **Wrap-around** (e.g. off-peak `22:00–06:00`) is stored as **two rows**
  (`1320–1440` and `0–360`) rather than a wrapping single row — keeps the
  covering-slot query a simple range test.
- **No overlap** within one tariff for the same day (validated on write; also a
  good candidate for an exclusion constraint later). Gaps are fine — an
  uncovered minute falls back to the tariff's base `price_per_kwh`.

Alternative considered: a JSON `slots` column on `Tariff`. Rejected — a table is
queryable (covering-slot lookup in SQL), migratable, and matches how the rest of
the schema models child rows.

### 4.2 `charging_sessions` — segment accrual columns (all nullable)

| column | type | meaning |
|---|---|---|
| `rate_coins_per_kwh` | *(existing)* `Numeric(12,2)` | **repurposed:** the *current* in-effect rate (still snapshotted at start, now also updated on each reprice) |
| `settled_cost_coins` | `Numeric(12,2)` NULL | cost accrued from all **closed** segments (energy consumed before the current rate took effect) |
| `rate_segment_start_kwh` | `Float` NULL | the session's `energy_kwh` reading when the current rate segment began (`0.0` at start) |
| `rate_valid_until` | `DateTime(tz)` NULL | when the current rate is scheduled to expire (end of the covering TOD slot); NULL for a flat tariff → no scheduled reprice ever |

Nullable everywhere → **legacy sessions** (started before this migration) have
NULL `settled_cost_coins`/`rate_segment_start_kwh` and bill via the old
single-rate formula. New sessions initialize `settled_cost_coins = 0`,
`rate_segment_start_kwh = 0`.

Alembic: one additive revision (renumber-at-merge per the marker-sweep protocol).

---

## 5. Rate resolution v2 (time-aware)

`resolve_rate_for_plug(db, plug, at=now) -> (rate: Decimal, valid_until: datetime|None)`

1. **Pick the tariff** via the *existing* chain
   (`plug.tariff → group.tariff → tenant.default`). Unchanged — one tariff wins.
   *(When the caps feature lands, a `Circuit.tariff_id` slots in between `plug`
   and `group`; noted, not built here.)*
2. **Resolve the rate within that tariff for `at`:** find the `tariff_slot`
   whose `[start_min, end_min)` (and `days_mask`) covers `at`'s local
   minute-of-day. If one matches → `(slot.price_per_kwh, end-of-slot)`. Else →
   `(tariff.price_per_kwh, start-of-next-slot-today or None)`.
3. **No tariff anywhere** → `(default_rate(), None)` (global env fallback).

`valid_until` is the timestamp of the **next** rate boundary (slot end, or the
start of the next slot when currently in a gap), or `None` when the tariff has
no slots. This is what lets the reprice check be a cheap timestamp compare
instead of a per-frame DB query (§6).

**Timezone:** slots are minute-of-day in a fixed **CPO-local timezone**
(new `Tenant.timezone`, default `Asia/Kolkata`). Minute-of-day + tenant tz →
absolute `valid_until`. Keeps "peak is 18:00–22:00" meaning wall-clock local.

**Back-compat:** `resolve_rate_for_plug(db, plug)` (no `at`) keeps working —
`at` defaults to `now`; callers that only want the number use `rate`.

---

## 6. Segmented billing engine

One shared helper computes cost from the session's segment state:

```
def session_cost(session, energy_kwh) -> Decimal:
    if session.rate_segment_start_kwh is None:        # legacy → single-rate
        return energy_cost(energy_kwh, session.rate_coins_per_kwh or COINS_PER_KWH)
    current_segment = energy_cost(
        max(0.0, energy_kwh - session.rate_segment_start_kwh),
        session.rate_coins_per_kwh,
    )
    return to_money(session.settled_cost_coins + current_segment)
```

Closing out a segment when the rate changes to `new_rate` at energy `E`:

```
def close_out_segment(session, new_rate, at_energy, valid_until):
    session.settled_cost_coins = to_money(
        session.settled_cost_coins
        + energy_cost(max(0.0, at_energy - session.rate_segment_start_kwh),
                      session.rate_coins_per_kwh))
    session.rate_segment_start_kwh = at_energy
    session.rate_coins_per_kwh = new_rate
    session.rate_valid_until = valid_until
```

**All three billing paths switch from `energy_cost(energy, rate)` to
`session_cost(session, energy)`:**
- `finalize_charging_session` — bills `session_cost(session, final_energy)`.
- live `TelemetryStore` — the store already knows the plug's rate; extend
  `start_session`/`update` to carry `settled_cost` + `segment_start` so the live
  `cost_coins` matches, OR (simpler) have `_persist_telemetry` compute
  `session_cost` and pass it to the store as the authoritative figure.
- `_maybe_auto_stop_on_exhaustion` — `accrued_cost = session_cost(session, energy)`.

**Rounding policy (decision, §11):** round **each closed segment** to 2dp as it's
added to `settled_cost_coins` (so the ledger/receipt show clean per-segment
amounts and the auth-hold math stays in whole coins). Accept ≤1 sub-cent of
drift per boundary — standard for metered billing; a session rarely crosses more
than 1–2 boundaries.

---

## 7. Reprice triggers

A rate change closes the current segment, applies the new rate, and notifies.
Three triggers, reusing existing hooks:

1. **TOD slot boundary (scheduled).** `_persist_telemetry` already runs every
   ~1 s per active session. Add a cheap guard: if `session.rate_valid_until`
   is set and `now >= rate_valid_until`, re-resolve `(rate, valid_until)` for the
   plug; if `rate != session.rate_coins_per_kwh`, `close_out_segment(...)` +
   notify. No per-frame DB query in the common case (only a timestamp compare
   until the boundary passes).
2. **Operator tariff edit (immediate).** The `/api/cpo/tariffs*` edit/slot
   endpoints proactively find ACTIVE sessions on affected plugs and either
   `close_out_segment` them directly, or set `rate_valid_until = now` so the next
   frame re-resolves. Gated by env `AUTO_REPRICE_ACTIVE_SESSIONS` (default on).
3. **Reaper backstop (safety net).** `SessionReaperService` adds a sweep (like
   `reap_time_limited_once`) for ACTIVE sessions whose `rate_valid_until` has
   passed but no telemetry frame has landed to trip the boundary (dropped
   gateway) — closes the segment against the last persisted energy.

All three call the **one** `close_out_segment` + notify helper — a single
race-safe path (session row locked, same as finalize).

**Notification:** a new `rate_changed` type (feed + Socket.io + Web Push):
> "Rate is now **6 coins/kWh** (peak 18:00–22:00). This applies only to energy
> from now on — what you've already used stays at the old rate."

Forward-only is guaranteed by the segment close-out; the driver is told, not
asked (per the product decision — no mid-charge consent gate in v1; see §11 for
the deferred "Stop now" opt-out).

---

## 8. Auth hold under TOD (the one real correctness snag)

The hold (`ChargingSession.hold_coins`) caps what finalize collects
(`min(final_cost, hold)`). If the rate **rises** mid-session and the hold was
sized at the *start* rate, `final_cost` can exceed the hold → forgiven overage
(the SECURITY.md §5 leak the hold exists to close).

**Decision:** size the hold at start against the **maximum applicable rate over
the session's max window**, not the current rate:

```
hold = min(available, max_kwh * max_rate_over_window(plug, now, max_duration))
```

where `max_rate_over_window` is the highest slot/base rate the session could hit
before its own `max_duration` cap. Deterministic, covers every TOD boundary the
session can reach, and needs no mid-session hold growth for the *scheduled* case.

For **operator edits** that raise the rate beyond that (unpredictable at start),
best-effort **grow the hold** on reprice — exactly the recompute already built
for editable sessions (`PATCH /limits`): `hold = min(available + own_hold,
remaining_max_kwh * new_rate)`. The **balance-exhaustion auto-stop** remains the
ultimate backstop (it already bills against `hold_coins`), so worst case the
session stops rather than over-forgiving.

---

## 9. API & frontend

**Operator (`/api/cpo/tariffs*`):**
- extend tariff CRUD with **slot** sub-resources: list / add / update / delete
  `tariff_slots` (window + rate + days), tenant-scoped, with overlap validation.
- `Tenant.timezone` on the CPO profile.

**Driver (transparency):**
- `PlugResponse` gains `price_next_per_kwh` + `price_changes_at` (resolved from
  the plug's tariff slots) so Home shows **"5 now · 6 from 18:00"** and the start
  card previews the schedule. (List endpoint stays N+1-free — resolve alongside
  the existing `price_per_kwh` in one grouped pass.)
- `GET /api/sessions/active` + the finalize receipt already carry
  `rate_coins_per_kwh`; add `settled_cost_coins` so the receipt can show a
  per-rate breakdown when a session spanned a boundary.

**Frontend** (prototyped in the concept, §04):
- Operator: tariff editor with the TOD slot table + "shown to drivers" toggle.
- Driver: current+next price on the charger card / start hint; the session
  monitor's **"Rate is now …"** notice (forward-only wording).

---

## 10. GST invoice interaction

Unaffected. `services/invoices.py` splits GST off the session's single
`coins_spent` total; segmented billing still produces one total, so the invoice
math is unchanged. *Optional future:* a per-segment line breakdown on the
`?format=html` invoice (needs `settled_cost` + the segment rows, which we don't
persist individually in this design — see §11 open decision on a segment log).

---

## 11. Open decisions (resolve before/with implementation)

1. **Per-segment rounding** — round each closed segment (chosen, §6) vs. accrue
   raw and round once at finalize. Chosen: per-segment, for clean ledger/holds.
2. **Segment audit log** — this design keeps only *aggregate* segment state
   (`settled_cost` + current segment). A `session_rate_segments` table (one row
   per segment: start_energy, rate, cost) would give a fully auditable breakdown
   for invoices/disputes at the cost of more writes. **Recommend deferring**
   unless the CPO/GST side needs the itemization.
3. **Weekday scope for v1** — ship `days_mask` in the schema but the editor UI
   can start "all days" only; per-weekday pricing is then a UI-only follow-up.
4. **Operator-edit reprices active sessions?** Default **on**
   (`AUTO_REPRICE_ACTIVE_SESSIONS`) — matches the ask; the flag lets a CPO opt
   out so an edit only affects new sessions.
5. **Driver opt-out on reprice** — v1 is **notify-and-continue** (forward-only).
   A "Stop now" action in the `rate_changed` notification (consumer-consent
   nicety, relevant to Indian mid-transaction pricing norms) is deferred.
6. **Hold sizing** — max-rate-over-window (chosen, §8) vs. current-rate + grow.
   Chosen the former for scheduled TOD; edits still grow best-effort.

---

## 12. Testing plan

- **Resolution:** covering-slot lookup (incl. boundary half-open, gaps→base,
  wrap-around two-row, `days_mask`), `valid_until` = next boundary, flat tariff →
  `(rate, None)`, empty chain → env default.
- **Segment math (DB-free):** `session_cost` single vs. multi-segment;
  `close_out_segment` accrues correctly; legacy NULL → single-rate; finalize
  foots to Σ segments to the cent.
- **Triggers:** frame-hook fires at `now >= valid_until` and only when the rate
  actually changed; operator edit closes out active sessions (flag on/off);
  reaper backstop closes stale-but-overdue sessions; each emits one
  `rate_changed` notification.
- **Hold:** sized at max-rate-over-window; a scheduled rise never forgives
  overage; an operator rise grows the hold (reuses the editable-session tests).
- **Regression:** a flat-tariff session bills byte-identically to pre-v2;
  exhaustion auto-stop + live cost use `session_cost`.

---

## 13. Rollout & doc updates

- Additive, nullable migration → zero-downtime; flat-tariff & legacy sessions
  behave identically, so **safe to deploy before any tariff has slots**.
- Flags: `AUTO_REPRICE_ACTIVE_SESSIONS` (default on). No flag needed for
  correctness (no slots = no reprice).
- On implementation, update the **invariant** wording everywhere it's stated:
  `services/pricing.py` module docstring, `models.py` `Tariff` docstring
  ("SNAPSHOTTED … never re-resolved"), and the IMPLEMENTATION_STATUS tariff row
  (🟡 → ✅ with the TOD/segmented note). This spec becomes the design reference.

---

### Phasing suggestion

1. ✅ **Schema + resolution v2** (tariff_slots, session columns, time-aware
   `resolve_rate_for_plug`, `session_cost`/`close_out_segment` helpers) — pure
   backend, fully unit-testable, no behavior change yet (no slots exist).
   *(Built: PR #34.)*
2. ✅ **Wire the billing paths** to `session_cost` + the frame-hook boundary
   trigger (`reprice_session_if_due`) + reaper backstop (`reap_reprice_once`) +
   `rate_changed` notification — behavior change behind "a tariff has slots".
   Start-time auth hold sized at `max_rate_over_window` (§8 scheduled-TOD case)
   pulled forward here so a rising boundary can't forgive overage. *(Built
   2026-07-14, feat/pricing-v2-phase2.)*
3. ✅ **Operator edit trigger + hold sizing.** `mark_tenant_sessions_for_reprice`
   (env `AUTO_REPRICE_ACTIVE_SESSIONS`, default on) stamps `rate_valid_until=now`
   on the tenant's ACTIVE sessions from the tariff update/delete + slot
   create/update/delete endpoints, so the existing frame-hook/reaper reprice
   them forward-only + notify where the rate moved; PATCH-`/limits` now sizes the
   hold at `max_rate_over_window`. *(Built 2026-07-14, feat/pricing-v2-phase3-operator-reprice.)*
   Not wired: tariff **reassignment** (plug/group/default → different tariff)
   still uses the session's start snapshot by design — flag if a reassignment
   should reprice in-flight sessions too.
4. ✅ **API + frontend** — slot editor (`/cpo/tariffs` page + `tariff_slots`
   CRUD sub-resources, `slot_overlaps` validation, tenant tz shown read-only),
   driver current+next price (`resolve_price_display`, Home ribbon "→ 6 @ 18:00").
   *(Built 2026-07-14, feat/pricing-v2-phase4-slot-editor.)* Deferred within P4:
   per-weekday `days_mask` UI (schema carries it; UI ships all-days), a
   `Tenant.timezone` edit endpoint (default Asia/Kolkata, shown read-only), and
   the session-monitor inline "rate is now" notice (the `rate_changed` bell
   notification already delivers it).
