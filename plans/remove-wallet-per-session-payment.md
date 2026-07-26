# Plan — Remove the prepaid wallet, charge per charging session

**Status:** proposal / scoping (not started)
**Author trigger:** Razorpay onboarding rejected the site as "wallet services" (prepaid
stored-value / PPI). Renaming copy alone does not change the substance — a user loads
₹ into a persistent balance and draws it down. This plan removes the stored-value layer
so every payment maps 1:1 to a delivered EV-charging service, which is what Razorpay
onboards.
**Design anchor:** `docs/MARKET_GAP_ANALYSIS.md:105` already frames "pay-per-use
*without* pre-funding a wallet" as a should-do. This is that.

---

## 1. Goal & non-goals

**Goal.** No persistent, user-spendable stored balance anywhere. Each charging session
results in exactly one payment for that session's metered cost. The site presents as an
EV-charging service (MCC 5552), not a wallet.

**Keep (survives the refactor):**
- The pricing/cost engine: `services/pricing.py`, `services/billing.py` (`session_cost`,
  segmented TOD accrual), `Tariff.price_per_kwh`, `COINS_PER_KWH`. These compute *what a
  session costs* — still needed.
- The CPO earnings/payout ledger driven by `coins_spent` (`services/payouts.py`). Still
  needed to pay hosts.
- Metering, MQTT, session lifecycle skeleton, reservations (already "free, no coin hold"
  — `services/reservations.py:5-13`).

**Non-goals.** No change to hardware/firmware, MQTT contract, tariffs, or CPO payout math
beyond removing the "minus offline top-ups" term. Rename to a real ₹ amount but keep the
`coins_per_kwh` *pricing* concept internally (it's just ₹/kWh at a 1:1 rate today).

---

## 2. The core constraint — the metered-amount trilemma

EV charging cost is **unknown until the session ends** (metered energy × ₹/kWh, possibly
crossing TOD rate segments). You therefore cannot charge an exact amount at the start.
There are only three ways to reconcile an unknown final amount with a real payment, and
each has a cost:

| # | Model | When money is taken | Exact? | UPI-friendly? | New Razorpay surface | Downside |
|---|-------|--------------------|--------|---------------|----------------------|----------|
| **B1** | Pre-auth hold + capture | Hold at start, capture actual at stop | ✅ | ⚠️ cards only (UPI pre-auth is limited) | manual-capture orders, void | Poor UPI coverage in India |
| **B2** | Prepay estimate + refund remainder | Capture ceiling at start, refund unused at stop | ✅ (net) | ✅ | Razorpay **refunds** | A refund on ~every session; T+ settlement lag; driver funds the ceiling |
| **B3** | Mandate / auto-debit post-paid | Exact debit at stop via UPI AutoPay / card-on-file | ✅ | ✅ | **mandates** (recurring) | Mandate-setup friction; failed-collection risk; heaviest build |
| **B4** | Post-paid checkout at stop | Driver pays exact bill after the session ends | ✅ | ✅ | **none new** (reuse Orders/verify/webhook) | Energy delivered before payment → collection risk |

### Recommendation — **B4 (post-paid exact charge) as MVP, B3 as fast-follow**

Rationale specific to AmpHive:
- **Market fit:** the target is closed communities (societies, offices, shops — see
  `Marketing.jsx`). Users are known, repeat, low-anonymity → post-paid collection risk is
  low and socially recoverable.
- **UPI-native:** India is UPI-heavy; B1's pre-auth is card-centric and weak on UPI. B4/B3
  capture a *known* amount, which UPI handles natively.
- **Least new code:** the exact session cost is **already computed** at stop
  (`session_cost`, `services/session_lifecycle.py:207`). B4 = create a Razorpay Order for
  that amount at stop and reuse the existing `create-order → verify → webhook` plumbing
  verbatim. No refunds (B2), no pre-auth (B1), no mandates (B3), lowest fees.
- **Unambiguously not a wallet:** each payment is "AmpHive charging session #123", which
  is exactly what defeats the "wallet services" classification.

**Collection-risk mitigations for B4 (must ship with it):**
1. **Start-gate on unpaid sessions:** a driver with an `UNPAID` finished session cannot
   start/queue a new one (replaces the old 402 balance floor — same gate location).
2. **Mandatory session ceiling:** make `max_kwh` (or a max-₹) **required** at start so the
   eventual bill is bounded (today it's optional). Existing energy/time auto-stop already
   enforces it — this replaces the "balance exhausted" auto-stop.
3. **Grace + nudge:** unpaid bill → notification + a "Pay now" surface; after N hours,
   restrict. Bad debt is bounded to one session per user.

**B3 fast-follow** (UPI AutoPay / card mandate registered once → auto-debit exact cost at
stop) eliminates the collection risk and enables tap-to-charge. Scoped as a later phase so
the resubmission is not blocked on mandate integration.

> **DECISION 1 (required from product/owner):** confirm **B4** or pick another. This is a
> money/risk call. The rest of this plan is written for B4; the payment-specific phase
> (Phase 4) is modular — switching models only rewrites that phase.

---

## 3. Legacy balances — real users hold coins today

`users.coin_balance` is non-zero for real accounts (e.g. the test driver has 500 coins).
Legally and ethically these prepaid balances must be honored or returned. Options:

- **A. Drain-down + freeze top-ups (recommended):** disable *all* credit paths
  immediately (Razorpay top-up, CPO cash top-up, admin adjust). Existing sessions consume
  any remaining balance first, then fall through to per-session payment. Balance decays to
  zero on its own. Reframe the balance UI as read-only "legacy charging credit (winding
  down — redeemable only for charging)". No money-out project.
- **B. Refund-out (clean cut):** proactively Razorpay-refund outstanding balances to the
  original payment method, zero the column, delete the wallet entirely. Cleanest end
  state, but: refunds >6 months are often impossible; CPO/admin-credited coins have no
  source payment to refund to; a mass-refund is its own operational task.
- **C. Hybrid:** drain-down now; a one-time manual settlement for any residual after a
  cutoff date.

> **DECISION 2 (required):** confirm **A (drain-down)** vs B/C. Recommended: **A** — it
> unblocks the resubmission (the top-up flow they objected to is gone) without a
> mass-refund. The residual draining balance is a closed-loop, redeem-for-charging-only
> instrument (RBI-exempt), and disappears over time.

---

## 4. Impact surface (from the full code map)

Grouped by what happens to each. File:line anchors are current as of this plan.

### Removed / frozen (the stored-value layer)
- **Wallet primitives** — `services/wallet.py`: `credit_wallet`, `available_balance`
  deleted; `debit_wallet_clamped` deleted (drain-down keeps a temporary
  "consume-legacy-balance" helper until balances hit zero, then deletes).
- **Start 402 floor** — `services/session_start.py:60,140-156` (`MIN_START_BALANCE_COINS`,
  the `available_balance < floor` check) → replaced by the **unpaid-session** gate.
  Re-export in `sessions.py:58-61`; queue gate `sessions.py:716-729`; reaper re-check
  `session_reaper.py:458-466`.
- **Holds** — `ChargingSession.hold_coins` (`models.py:334`, migration `0013_auth_holds`),
  placement `session_start.py:157-185`, re-size `sessions.py:445-468`, settle
  `session_lifecycle.py:244-267`. Under B4 there is no hold; under B1/B2 the hold becomes
  a real pre-auth/prepay amount (Phase 4 detail).
- **Top-up crediting** — `routers/payments.py` `_credit_topup` (50-104), `GET
  /api/wallet/ledger` (107-156), `POST /api/payments/verify` credits (189-281), webhook
  credit (284-361). Repurposed to per-session orders (Phase 4).
- **CPO cash top-up** — `routers/cpo/_topups.py` (`credit_wallet` at :113), model
  `OfflineTopup` (`models.py:765-808`, migration `0026`), pool math
  `services/payouts.py:193-205`. Becomes "mark session paid offline (cash)" instead of
  crediting a balance — see Phase 6.
- **Admin manual adjust** — `routers/admin.py:537-624` → removed or repurposed to
  refund/settlement tooling.
- **Exhaustion auto-stop** — `services/mqtt/telemetry.py:458-582`
  (`_maybe_auto_stop_on_exhaustion`, `AUTO_STOP_ON_BALANCE_EXHAUSTED`,
  `LOW_BALANCE_WARN_FRACTION`, `low_balance` notification) → replaced by the mandatory
  `max_kwh`/max-₹ ceiling (existing limit auto-stop already does this).

### Rewritten (settlement path)
- **Finalize** — `services/session_lifecycle.py:133-417`: keep cost math
  (`session_cost`), replace the `debit_wallet_clamped` step (244-250) with: mark the
  session `UNPAID` + its exact `amount_due`; drain legacy balance first if any; kick the
  per-session payment (create order + notify "Pay ₹X"). Receipt payload / notification
  copy (361-407) reframed from "coins charged, X left" to "₹X due / paid".
- **Disputes/refunds** — `routers/cpo/_disputes.py:69-206` (`credit_wallet` at :183): an
  approved dispute now issues a **real Razorpay refund** against the session's payment
  (new money-out plumbing) instead of crediting coins. Driver filing
  `sessions.py:920-1001` unchanged.

### Schema / migrations (new migration, forward-only)
- `users.coin_balance` + CHECK `ck_users_coin_balance_non_negative` (`models.py:163-175`,
  migrations `0001`,`0002`): retained read-only during drain-down; dropped in the final
  cleanup migration once all balances are zero.
- `tx_type` enum (`models.py:65-74`): `topup`/`cpo_topup` retired; add `session_payment`
  and `refund` (money-out). `ledger_transactions.balance_after` becomes nullable/removed.
- `ChargingSession`: add `amount_due` / `payment_status` (`UNPAID|PAID|REFUNDED|WAIVED`) +
  `razorpay_order_id`/`razorpay_payment_id`; drop `hold_coins` in cleanup.
- `offline_topups` table retired (or repurposed as `offline_session_payments`).
- Config: `min_start_balance_coins`, `coin_inr_rate` removed from `GET /api/config`
  (`main.py:201-205`); keep `coins_per_kwh` as the pricing input (rename display to ₹/kWh).

### Frontend (broad — `useWallet` is threaded widely)
- **Remove:** `contexts/WalletContext.jsx`, the AppBar wallet pill (`AppBar.jsx:101-103`),
  Dashboard "Wallet balance"+"Top up" rail (`Dashboard.jsx:674-679`), SessionMonitor
  low-balance banner (`SessionMonitor.jsx:129-271`), ChargeSetupModal "your balance covers
  ≈ N kWh" (`ChargeSetupModal.jsx:96-97,164`), and the `/wallet` top-up page
  (`Wallet.jsx`) → replaced by a **per-session "Pay ₹X" checkout** reached from the
  receipt (Session/Activity) and a "unpaid sessions" surface.
- **Reframe:** `Marketing.jsx` "top up a prepaid wallet" step → "pay per charge, no
  wallet"; `ConfigContext.jsx:13-18` defaults; `Account.jsx:207` push copy.
- **Legacy-credit (drain-down only):** a read-only "legacy credit" line until balances
  zero out, then delete.

### Docs
- `docs/DATA_MODEL.md`, `docs/SECURITY.md` (wallet atomicity/hold §§), `docs/API_REFERENCE.md`,
  `docs/IMPLEMENTATION_STATUS.md`, `docs/MARKET_GAP_ANALYSIS.md` (move line 105 to "done"),
  `docs/TODO.md`. `docs/PRICING_V2_SPEC.md` stays (pricing survives). Update the map/
  redirect stubs per the docs-source-of-truth rule (docs/, not root copies).

---

## 5. Phased implementation

Each phase is independently shippable and leaves the app working.

**Phase 0 — Decisions & schema.** Confirm DECISION 1 (B4) + DECISION 2 (drain-down).
New migration: add `charging_sessions.payment_status`, `amount_due`, `razorpay_order_id`,
`razorpay_payment_id`; add `session_payment` to `tx_type`. Do **not** drop `coin_balance`
yet.

**Phase 1 — Freeze all credit paths (ship first, unblocks resubmission).** Disable
`POST /api/payments/create-order|verify|webhook` top-up, CPO cash top-up, admin adjust.
Frontend: remove the top-up UI; show read-only legacy credit. After this phase the site no
longer offers "add money to a wallet" — the thing Razorpay flagged. Sessions still settle
against existing balance (drain-down).

**Phase 2 — Start-gate flip.** Replace the 402 balance floor (start + queue + reaper
auto-start) with the **unpaid-session** gate. Make `max_kwh`/max-₹ ceiling mandatory.
Remove the exhaustion auto-stop (`telemetry.py`); rely on the limit auto-stop.

**Phase 3 — Finalize → per-session bill.** Rewrite the finalize debit step: consume any
legacy balance first, then set `payment_status=UNPAID` + `amount_due` for the remainder.
Receipt/notification copy → "₹X due".

**Phase 4 — Per-session payment (B4).** At stop (or on the receipt), create a Razorpay
Order for `amount_due` (reuse `services/payments.py` Orders/verify/webhook, keyed by
session). On capture → `payment_status=PAID`, write a `session_payment` ledger row. Wire
the frontend "Pay ₹X" checkout on the receipt + an "unpaid sessions" list. *(If a model
other than B4 is chosen, this is the phase that changes.)*

**Phase 5 — Refunds (money-out).** Add Razorpay refund plumbing; rewire dispute approval
(`_disputes.py`) to refund the session payment instead of crediting coins. Repurpose admin
tooling to trigger/inspect refunds.

**Phase 6 — CPO cash payments.** Replace "credit driver wallet from cash" with "mark
session paid offline (cash)" recorded against the session; adjust payout pool math
(`payouts.py`) to drop the "minus offline top-ups" term.

**Phase 7 — Cleanup migration.** Once all `coin_balance` are zero (drain-down complete):
drop `coin_balance` + CHECK, `hold_coins`, `offline_topups`, retire `topup`/`cpo_topup`
enum values, delete the temporary legacy-consume helper and legacy-credit UI.

**Phase 8 — Fast-follow (optional): B3 mandates.** UPI AutoPay / card-on-file so exact
cost auto-debits at stop → removes collection risk, enables tap-to-charge.

---

## 6. Razorpay resubmission checklist (parallel to code)
- Register/confirm the merchant under **EV charging (MCC 5552 / utilities)**, not
  wallet/PPI.
- Resubmit only after **Phase 1** is live (top-up removed).
- Resubmission note: "AmpHive is a pay-per-use EV-charging service. Each payment settles a
  single metered charging session; there is no stored balance or wallet. Any legacy
  prepaid credit is closed-loop, redeemable only for charging on AmpHive, non-transferable,
  no cash withdrawal (RBI closed-system PPI exemption)."

---

## 7. Risks & open questions
- **Collection risk (B4):** bounded by the unpaid-session start-gate + mandatory ceiling +
  closed-community trust. Quantify acceptable bad-debt before Phase 4; B3 removes it.
- **Anonymous/public plugs:** post-paid trust weakens for truly public plugs. If AmpHive
  ever lists public plugs, gate those to B2 (prepay) or B3 (mandate).
- **Refund settlement lag** (if B2 chosen) and **mandate friction** (B3) — not on the B4
  path.
- **CPO payout coupling:** payouts key off `coins_spent`, which still exists (now the
  session's paid ₹). Verify the "minus offline top-ups" term removal doesn't skew existing
  payout windows.
- **Reconciliation:** `ledger_transactions.balance_after` was the audit spine; per-session
  payments need a new reconciliation view (session ↔ Razorpay payment_id). Reuse the
  existing UNIQUE `razorpay_payment_id` idempotency pattern.
- **In-flight sessions during migration:** Phase 2/3 must handle sessions started under the
  old hold model (NULL `payment_status`) — treat legacy rows as the old path until closed.

---

## 8. Testing
- Backend: rewrite `backend/tests/test_payments.py` for per-session orders; new tests for
  the unpaid-session start-gate, finalize→UNPAID, capture→PAID idempotency (verify vs
  webhook race, mirroring the existing top-up idempotency tests), refund-on-dispute, and
  legacy-balance drain-down ordering.
- Frontend: update `Wallet.test.jsx`→ per-session checkout; `SessionMonitor`,
  `Dashboard`, `AppBar`, `Marketing`, `statusCopy` tests for removed balance surfaces.
- E2E: charge → receipt shows ₹ due → pay → PAID; unpaid session blocks a new start.

## 9. Rollback
Phases 1–6 are additive/feature-flaggable and precede the destructive Phase 7 migration.
Rollback before Phase 7 = re-enable the credit paths (revert Phase 1) — no data lost, since
`coin_balance` is retained until cleanup. After Phase 7 the wallet columns are gone;
rolling back then requires a restore, so Phase 7 ships only after the model is proven.
