# AmpHive — Market Gap Analysis

*Written 2026-07-09, grounded against source (backend routers/models, firmware
MQTT contract, frontend pages) and the product specs
([requirements.md](../requirements.md), [features_list.md](../features_list.md)).*

This page compares AmpHive against **features that commercial EV-charging apps
ship as table stakes** — the driver and operator experiences users have come to
expect from networks like ChargePoint, Tesla, Electrify America, PlugShare (global)
and Statiq, Tata Power EZ Charge, Ather Grid, ChargeZone, Kazam, Zeon (India, the
Razorpay/₹ market AmpHive targets). It is a **product/competitive** view, distinct
from the engineering-debt view in [TECH_DEBT.md](TECH_DEBT.md) and the security
view in [SECURITY.md](SECURITY.md).

**How to read the verdict column:**

- **Should-do** — a market baseline AmpHive plausibly needs to be competitive.
- **Aspirational** — already named in the internal specs but not built (so this
  page just confirms the market agrees it matters).
- **By design / out of scope** — a genuine market feature that AmpHive's
  *consumer-smart-plug* model (not OCPP EVSE hardware) deliberately doesn't chase.
  Listed so the omission is a conscious decision, not an oversight.

Legend: ✅ present · 🟡 partial · ❌ absent.

---

## 1. The headline gaps (highest user-visible impact)

1. **No QR-code start.** ~~Every mainstream Indian network (Statiq, Tata, Ather,
   ChargeZone) makes *scan-the-QR-on-the-charger* the primary start action.
   AmpHive is **Plug-ID-typed-by-hand only**~~ **Closed 2026-07-12:** the CPO
   Plugs page renders a printable per-plug QR (`qrcode.react`) encoding
   `/?plug=<id>`; the phone's own camera resolves it (still no in-app
   scanner, matching [requirements.md](../requirements.md)'s decision against
   one) and Home prefills + focuses the Plug ID input, still fully
   auth-gated — an anonymous scan is sent through `/login` and returned to
   the same deep link afterward. Closes this cheaply without changing the
   security model, as originally proposed here.
2. **No native mobile app.** AmpHive is a React web SPA; the market ships
   iOS/Android apps (with the OS-level push, background session alerts, and
   "add to home screen" polish drivers expect). A PWA is the low-cost bridge.
3. **No notifications of any kind.** No push / SMS / email for *charging complete*,
   *session stopped*, *low balance*, or *fault* — a baseline in every competitor
   and already asked for in the specs. Today a driver must keep the live page open
   to know their car finished. See [§4](#4-notifications--engagement).
4. **Single flat price, no pricing model.** Billing is one global env var
   (`COINS_PER_KWH`, default 5.0) — there is **no price column anywhere in the
   data model**. No per-CPO/per-site/per-group rates, no time-of-day/peak pricing,
   no per-minute, no idle fee, no session start fee. The market (and AmpHive's own
   CPO spec) treats configurable tariffs as core. See [§3](#3-pricing--billing-depth).
5. **No tax invoice / receipt.** Indian commercial charging must issue a
   GST invoice; AmpHive produces neither an invoice nor a formal per-session
   receipt (history shows energy + coins only). Blocks real-money operation.
6. **CPO payout / settlement — backend ledger now exists, manual settlement
   only.** Money still flows driver → platform, but a `Payout` ledger now
   tracks what's owed back to each CPO (`GET /api/cpo/earnings`,
   `GET`/`POST /api/cpo/payouts`, admin `POST .../mark_paid`) — closing
   [requirements.md §5.1](../requirements.md)'s "withdraw earnings" promise
   at the record-keeping level. There is **no bank/UPI/payment-gateway
   integration**: the admin marks a payout PAID after transferring funds
   out-of-band, and there is **no CPO-portal UI** yet (backend/API only).

---

## 2. Discovery, map & trip planning

| Market feature | Seen in | AmpHive | Verdict |
|----------------|---------|:-------:|---------|
| Map of chargers | all | 🟡 Leaflet/OSM markers of available public plugs | — |
| Real-time availability on map | all | ✅ (2026-07-12) markers color-coded Available/In use/Offline with a legend + live counts | — |
| Filter by power / price / connector / amenities | ChargePoint, PlugShare, Statiq | 🟡 (2026-07-12) availability + group-name filters shipped; power/price/connector remain absent — **there is no power-rating (or price) column anywhere in the plug data model**, so those specific filters aren't buildable without a schema change first | Should-do (power/price columns) |
| Search / autocomplete by location or Plug ID | most; specs §4 | ❌ (type exact Plug ID) | Should-do |
| "Find nearest" + hand-off to Google/Apple Maps nav | all | ❌ | Should-do |
| Favorites / saved chargers | ChargePoint, Tesla, PlugShare | ❌ | Should-do |
| Site detail: photos, address, access hours, directions | PlugShare, Statiq; specs §3 | ❌ (photo upload named in spec, not built) | Aspirational |
| Ratings / reviews / check-ins | PlugShare (its whole moat) | ❌ | Should-do |
| Charger reliability / uptime indicator | ChargePoint, EA "station health" | ❌ | Should-do |
| Trip / route planner across chargers | Tesla, ABRP, PlugShare | ❌ | By design (single-plug, not a road-trip network) |

## 3. Pricing & billing depth

| Market feature | Seen in | AmpHive | Verdict |
|----------------|---------|:-------:|---------|
| Configurable per-kWh tariff per site/CPO | all networks; specs §5.1, FL 1.2 | 🟡 backend live: `Tariff` model + resolution chain (plug → group → tenant default → env) snapshotted per session, CPO CRUD/assign API; no CPO-portal UI yet | Should-do |
| Time-of-day / peak / off-peak pricing | ChargePoint, Tata, Statiq | ❌ | Aspirational |
| Per-minute billing | ChargePoint, EA | ❌ | Aspirational |
| Idle / occupancy fee (car parked, not charging) | Tesla, ChargePoint, EA; FL 1.2 | ❌ | Aspirational |
| Flat session start fee | EA, Statiq; FL 1.2 | ❌ | Aspirational |
| Prepaid wallet top-up (UPI/cards/netbanking) | Indian norm | ✅ Razorpay | — |
| Pay-per-use *without* pre-funding a wallet | ChargePoint, Tata (post-paid/direct) | ❌ (must pre-load coins) | Should-do |
| Auto-recharge / auto-topup wallet | Statiq, Ather | ❌ | Should-do |
| Authorization *hold* sized to the session | Tesla, EA (pre-auth) | ❌ — 50-coin floor only; overage is forgiven | Should-do (see [SECURITY.md](SECURITY.md)) |
| GST tax invoice / downloadable receipt | Indian legal requirement | ❌ | Should-do |
| Refunds | all | ❌ (`REFUND` enum exists, no code path) | Should-do |
| Promo codes / coupons / referral credit | Statiq, Ather, Kazam | ❌ | Should-do |
| Loyalty / reward points | Tata, Ather | ❌ | Nice-to-have |
| Subscription / membership plans | ChargePoint, EA (Pass+) | ❌ | Nice-to-have |
| Corporate / fleet billing accounts | ChargePoint, Statiq business | ❌ | By design (consumer MVP) |

## 4. Notifications & engagement

| Market feature | Seen in | AmpHive | Verdict |
|----------------|---------|:-------:|---------|
| Charging-complete alert | all | ❌ | Should-do |
| Session start/stop confirmation | all | ❌ | Should-do |
| Low-balance / wallet warning | Indian wallet apps; specs §4 | ❌ | Should-do |
| Fault / safety alert to driver | ChargePoint, EA | ❌ (and the backend doesn't even ingest firmware `/alarms`) | Should-do |
| Idle-fee warning before charging | Tesla, EA | ❌ | Aspirational |
| Channels: push / SMS / email | all; specs §4 | ❌ none wired | Should-do |

## 5. Session start & control

| Market feature | Seen in | AmpHive | Verdict |
|----------------|---------|:-------:|---------|
| Remote start / stop from app | all | ✅ | — |
| Live session telemetry (kW, kWh, cost, time) | all | ✅ Socket.io | — |
| QR-scan to start | Indian norm | ✅ (2026-07-12) printable per-plug QR (CPO Plugs) deep-links to `/?plug=<id>`, prefilled + auth-gated on Home; Plug-ID typing still works too | — |
| RFID / tap card to start | ChargePoint, EA | ❌ | By design (app-first) |
| Reserve / book a charger ahead | Statiq, ChargeZone, Tata | ❌ | Should-do |
| Scheduled / delayed start (charge off-peak) | Tesla, ChargePoint | ❌ | Aspirational |
| Stop at target kWh / % SoC | Tesla, EA | 🟡 `max_kwh` safety cap exists; no user "stop at X" goal | Should-do |
| Plug & Charge / ISO 15118 autocharge | Tesla, EA, Ionity | ❌ | By design (smart plug, not ISO 15118 EVSE) |
| Vehicle profile (make/model/connector) | ChargePoint, PlugShare | ❌ | Nice-to-have |

## 6. Trust, support & account

| Market feature | Seen in | AmpHive | Verdict |
|----------------|---------|:-------:|---------|
| In-app support / help / FAQ | all | ❌ | Should-do |
| "Report a problem with this charger" | ChargePoint, PlugShare | ❌ | Should-do |
| Dispute / refund request flow | all | ❌ | Should-do |
| Charger uptime / status transparency | ChargePoint, EA | ❌ | Should-do |
| Multi-language / localization | Indian apps (Hindi + regional) | ❌ English only | Should-do |
| Profile management, saved payment methods | all | 🟡 basic account; Razorpay holds cards | — |

## 7. Operator (CPO) portal vs. market operator consoles

Benchmarked against ChargePoint/Statiq/Kazam operator dashboards.

| Market feature | AmpHive | Verdict |
|----------------|:-------:|---------|
| Gateway / plug / group CRUD | ✅ | — |
| Utilization & revenue analytics | 🟡 charts, no export | — |
| CSV / Excel export of sessions & revenue | ❌ (specs §4 want it) | Should-do |
| Configurable pricing / tariff UI | ❌ (no price model) | Should-do |
| Tax / GST configuration | ❌ | Should-do |
| Payout / earnings withdrawal | 🟡 backend ledger + endpoints (`Payout`, manual settlement); no CPO-portal UI yet | Should-do (UI) |
| Remote diagnostics / fault console | ✅ (2026-07-12) `/cpo/faults`: lists `gateway_events` (severity/unacknowledged filters), acknowledge, and a top strip of plugs currently in maintenance | — |
| Maintenance-mode toggle per plug | ✅ (2026-07-12) `POST /api/cpo/plugs/{id}/maintenance` (`enter`/`clear`, audited); a THERMAL_CUTOFF/OVERCURRENT_CUTOFF alarm now auto-enters MAINTENANCE too | — |
| Bulk plug provisioning (CSV) | ❌ (specs §4 want it) | Aspirational |
| Dynamic load balancing across a site | ❌ (specs FL 4.1 want it) | Aspirational |
| Carbon-offset / green metrics | ❌ (specs FL 1.2 mention it) | Nice-to-have |
| Granular RBAC (Super-Admin/CPO/Operator/Viewer) | 🟡 only `admin`/`cpo`/`driver`; specs §3 want 4 tiers | Should-do |

## 8. Interoperability & standards

This is where AmpHive most sharply diverges from the industry — largely **by
design**, but the trade-offs should be explicit.

| Standard | What it buys | AmpHive | Verdict |
|----------|--------------|:-------:|---------|
| **OCPP** (charger ↔ network) | Works with any compliant EVSE; the universal CPO protocol | ❌ proprietary MQTT contract | By design (smart plugs, not OCPP hardware) — but it means **zero interoperability with real EVSEs**, and no path to onboard commercial chargers later |
| **OCPI** (network ↔ network roaming) | A driver on network A charges on network B; eMSP roaming hubs | ❌ | By design (closed network) — caps AmpHive to an island; competitors increasingly roam |
| Connector model (Type 2 / CCS2 / CHAdeMO) | DC fast + standard AC charging | ❌ AC wall-socket (Tapo P110) only | By design (this is the cost-disruption thesis) — but it structurally excludes DC-fast and most public-charging use cases |

**Framing:** AmpHive's wedge is deliberately *below* the OCPP/EVSE market —
turning ₹1,000 smart plugs into shared slow-AC charging where installing a
₹1-lakh+ OCPP charger doesn't pencil out. Judged as an *OCPP network* it fails on
interoperability; judged as *monetized shared outlets* those omissions are the
point. Both framings should be stated so the interoperability gap reads as a
strategic boundary, not an omission — and so a future "also ingest OCPP chargers"
pivot is recognized as a large, deliberate scope expansion.

---

## 9. Where these already live in the backlog

Several market gaps overlap items the internal specs already flagged; this page
reframes them as *competitive* rather than *aspirational* and adds the ones the
specs missed. Cross-references:

- **Dynamic pricing, idle/start/per-minute fees, load balancing, carbon metrics,
  bulk provisioning, 4-tier RBAC, notifications** — named in
  [requirements.md §4](../requirements.md) / [features_list.md](../features_list.md)
  but unbuilt.
- **Refunds, payouts, authorization holds, alarm ingestion** — surfaced in the
  design-gap review; see [SECURITY.md](SECURITY.md) and
  [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).
- **QR start, native/PWA app, invoices, reservations, promo/referral, reviews,
  map filters, CSV export, in-app support, localization** — *not previously
  tracked anywhere*; this page is their first home.

## 10. Suggested priority (impact ÷ effort)

**Do first (cheap + high impact):** QR/deep-link start · charging-complete +
low-balance notifications (email/push/PWA) · per-CPO price model + tax invoice ·
CSV export · basic map availability legend/filters.

**Do next (revenue/trust):** CPO payout ledger · refund + dispute flow ·
authorization hold · reservations · auto-topup · maintenance-mode + fault console
(pairs with ingesting the firmware `/alarms` topic).

**Strategic (larger bets):** PWA/native app · dynamic/ToU pricing + idle fees ·
promo/referral/loyalty · localization · dynamic load balancing.

**Consciously not pursuing (state it, don't drift into):** OCPP/OCPI/ISO 15118 ·
DC-fast connectors · trip planning — unless the product pivots off the
smart-plug thesis.
