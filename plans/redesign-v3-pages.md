# Redesign v3 — page design briefs

Companion to `plans/redesign-v3-contract.md` (read that first: primitives, APIs,
rules). Each section below is the authoritative anatomy for one page-owning agent.
Shared modal prop contracts are at the bottom — cross-page imports rely on them.

Voice: plain verbs, sentence case, no filler. Errors say what happened + what to
do. Driver surfaces never show gateway MACs, MQTT, "tenant", or raw enums.

---

## C1 — Marketing homepage (`pages/Marketing.jsx`, day theme, anon `/`)

The hero is the product's real gesture: the printed bay label.

1. **Hero** (two columns, stacks on mobile): left — eyebrow "SHARED EV CHARGING",
   display headline **"The charger was already there."**, lede: "AmpHive turns the
   ordinary plug points in your society, office, or shop into a paid EV-charging
   network — a smart plug and a matchbox-sized hub instead of lakhs of new
   hardware." CTAs: `Find a charger` (btn-primary → /map) + `Host your chargers`
   (btn-quiet → `${cpoOrigin()}/cpo`). Right — the **bay-label card** (signature):
   a sticker-like card (white, volt-lime top band with Zap icon + AMPHIVE, dashed
   inner border, big mono "PLUG ID"), beneath it an inline form: label "Already at
   a charger? Type the Plug ID printed on the label", numeric input + `Start`
   button → navigate `/login?next=${encodeURIComponent('/?plug='+id)}` (anon).
   Below: live proof line from `GET /api/plugs/public` — `<StatusDot state="available" live/>
   N chargers on the network · M available right now` (hide the line entirely on fetch error — never fake it).
2. **How it works** — numbered 1-2-3 (genuine sequence): Find or scan → Top up a
   prepaid wallet (UPI/cards) → Charge with a live meter; auto-stop at your limit
   or balance. Small honest footnote: reservations hold the slot, you still start
   the charge yourself.
3. **For drivers** — 3 cards: Live cost meter (₹ ticks as you charge) / Reserve &
   "notify me when free" / Wallet with receipts & GST invoices.
4. **For hosts** — full-width band styled with the VOLT palette (a literal preview
   of the console atmosphere inside the light page; hardcode a `data-theme="volt"`
   wrapper): "Own a parking spot with a plug point? Earn from it." Bullets: set
   your own ₹/kWh with time-of-day pricing · private access codes or public
   listing · live earnings, faults, and GST paperwork in one console. Small
   HTML-built mini console mock (KPI strip + a meter bar — no images). CTA
   `Open the host console` → cpoOrigin()/cpo.
5. **Safety strip** (quiet, small): outbound-only connectivity — hosts never open
   their network · prepaid with per-session holds · automatic stop on faults.
6. **Footer** (`.mkt-footer`): brand + three columns of REAL links only (Drivers:
   Find a charger, Wallet, Activity · Hosts: Host console, Become a host ·
   Account: Sign in, Create account). No fake About/Careers/Blog links.

Anon users hitting `/?plug=<id>`: this page must detect the param and immediately
funnel: banner card above the hero "Charger #id — sign in to start" + button to
`/login?next=/?plug=id`.

## C2 — Driver dashboard (`pages/Dashboard.jsx`, authed `/`) + PlugCard + modals

Order (single column mobile; desktop: main + right rail):
1. **Active session banner** (per active session): surface card, `dot-ok dot-live`,
   plug name, live ₹ + kW from SessionContext, `Open session` btn-primary-sm.
2. **Charge now card** (signature): big mono plug-ID input; debounced (400ms)
   lookup via `GET /api/plugs/{id}` → inline preview row (name, StatusDot+state,
   ₹/kWh) with `Start charging` btn enabled only when startable, or state-specific
   copy from statusCopy ("in use — you can reserve it"). Unknown id → inline
   "No charger with ID 7 — check the label" (no modal-two-steps-deep failure).
   Handles `?plug=` (autofill + auto-lookup; then clear the param).
3. **Up next strip** (only if any): reservation + queued-charge chips with
   countdown + cancel (ConfirmDialog). ABOVE the charger list on mobile.
4. **Your chargers**: `.seg` [Your groups | Public] + filter selects (state — all
   5 states, group) + `See map →` link to /map. Grid of **PlugCard**s.
5. **Rail** (desktop only): wallet card (₹ big via Money, `Top up` btn →/wallet,
   month stats from `GET /api/me/stats`: kWh + spend "this month").

**PlugCard state machine** (build as `components/PlugCard.jsx`, explicit and
boring): header row = name + StatusDot+label; meta row = ₹/kWh (+ "₹x after
22:00" when next_price differs) + group chip; action row by state:
- `available` → `Charge` (btn-primary-sm) + `Reserve` (btn-ghost-sm)
- `in_use` → `Notify me` bell toggle (aria-pressed) + `Reserve`; "Reserved until HH:MM" badge when applicable
- `unpowered` → `Queue charge` ONLY if the plug payload advertises queueability (same field the old Home used) else `Notify me`; sublabel "No mains power right now"
- `offline` → no actions; sublabel "Can't be reached right now"
- `maintenance` → no actions; badge "Under maintenance"
- reserved-for-you → `badge-brand "Reserved for you"` + Charge enabled during window.
No whole-card click. Real buttons. Fetch errors → ErrorState with retry (never the empty state); empty → EmptyState "No chargers yet" + `Join a group` + `Browse the map`.
Owns rebuilds of `ChargeSetupModal` (mode prop! see bottom) and `ReserveModal`
(overlap check against fetched windows; presets 30m/1h/2h/4h + custom end time).

## C3 — Map (`pages/MapPage.jsx` + `components/MapComponent.jsx` rebuild)

Full-height map (`calc(100vh - var(--nav-h))`, minus tabbar on mobile) with an
overlaid list panel (desktop left panel 360px; mobile bottom sheet ~40vh,
draggable not required). Data: authed → `/api/plugs/available`, anon →
`/api/plugs/public`; 60s usePoll + manual refresh button. Legend = 5 StatusDots.
Geolocate button (navigator.geolocation → map.setView, graceful deny). List rows:
name, StatusDot, ₹/kWh, distance if geolocated. Marker popup: name, state label,
price, button — authed `Charge` → `/?plug=id`; anon `Sign in to charge` →
`/login?next=/?plug=id` (context preserved — fixes the drop). Plugs without
coordinates: "N chargers not on the map yet" note above the list. OSM tiles stay
(CSP img-src allows https).

## C4 — Session (`pages/Session.jsx` + SessionMonitor + SessionReceipt + `components/ChargeRing.jsx`)

- No-session visit: friendly interstitial (state-block: "No active charge" +
  `Find a charger` + `Recent activity`), NOT an instant redirect.
- Multi-session: seg pills with per-session live ₹.
- **ChargeRing** (signature): SVG ring ~260px. With a kWh/time limit → determinate
  progress toward it; without → slow rotating brand-gradient arc (reduced-motion:
  static). Center: ₹ cost (font-data, text-3xl) + kWh beneath. Below the ring:
  meter row (kW now · elapsed · plug name). Rate line: fetch
  `/api/plugs/{plug_id}/tariff-preview` for `price_now` — label "₹x/kWh now"
  (hide on fetch failure; NEVER show the global config rate — it lies on
  time-of-day tariffs).
- Notices → `.banner`s via statusCopy: stale telemetry, alarms (eventTypeCopy),
  low balance (uses price_now, includes `Top up` link + "charging continues while
  you top up").
- Mid-session limit editor: keep, restyled (field + Save via PATCH limits).
- **Stop** → ConfirmDialog: "Stop charging? You'll be billed for X kWh — about ₹Y."
  busy state while stopping.
- Receipt: summary card (energy, duration, ₹, balance after; shortfall row gets
  plain-language help "The remaining ₹x couldn't be collected — it stays owed on
  your account"), auto-stop reason via stopReasonCopy, actions: `Download GST
  invoice` (existing invoice fetch w/ res.ok check) · `Report an issue`
  (DisputeModal) · `Charge again`.

## C5 — Wallet (`pages/Wallet.jsx`)

Header card: ₹ balance huge (Money), available-vs-held line when holds exist
("₹x reserved for the running session"). Top-up: quick chips 100/200/500/1000 +
custom amount input (min ₹50, max ₹10,000), `Pay ₹X` via `loadRazorpay()`
(`utils/razorpay.js`); dismiss = neutral banner-info "Payment not completed —
nothing was charged" (NOT an error); verify failure = banner-danger with order id
shown + "keep this reference" copy. Success: toast.ok + inline ok banner; if
`?next=` param present (arrived from a session low-balance link) show `Back to
your session`. Ledger below: DataTable (collapse), date, type via txTypeLabel,
description, signed ₹ (ok/danger color), balance after. One quiet line under the
header: "1 coin = ₹1 — your balance is prepaid credit." only if config rate is 1.

## C6 — Activity (`pages/Activity.jsx` + `components/DisputeModal.jsx` rebuild)

Tabs [Charging sessions | Issue reports]. Sessions: paginated DataTable
(`/api/sessions/history?limit&offset` new shape `{total,items}`), columns date,
charger (plug_name!), energy, ₹, status badge (sessionStatusLabel); row click →
detail Modal via `GET /api/sessions/{id}` (receipt layout reused from C4's
receipt markup — keep it self-contained here, don't import C4 files) with
invoice + dispute actions (dispute only for billed/completed). Issue reports tab:
`GET /api/sessions/disputes/my` — status badges (open/approved/rejected),
resolution note + refund shown when resolved. DisputeModal: adds category select
(Billing / Charger problem / Access / Other — prepended to the free-text reason
as "[Billing] ..." since the API takes reason only), success = toast + "We'll
notify you when the operator responds."

## C7 — Groups + Account + NotificationBell restyle

`pages/Groups.jsx`: join card (code input, uppercase hint "codes look like
SUNRISE24 — your host shares them"), group cards (name, Public/Private badge,
plug count, `View chargers` → `/?group=<id>` — Dashboard C2 reads ?group and
preselects the filter) + `Leave` (ConfirmDialog) via new DELETE leave endpoint.
`pages/Account.jsx`: profile card (name, email, member-since — read-only, no
edit endpoint exists: don't fake one), Security card (`Reset your password` →
sends the reset-email flow via existing forgot-password endpoint + explains),
Notifications card (web-push enable/disable — move the logic from the bell's
footer here), `Host your chargers` card → cpoOrigin()/cpo (driver-role only).
`components/NotificationBell.jsx`: restyle to primitives (no inline styles, no
emoji), panel = .user-menu-panel-like drawer at `--z-drawer`, items actionable:
notification with plug_id → /?plug=, session → /session, topup → /wallet; Esc
closes; refetch on open.

## C8 — Auth pages (Login split → Login + Signup, Forgot, Reset)

Shared `components/AuthShell.jsx` (C8-owned): centered narrow card, brand-bolt +
wordmark header, day theme. Login: email+password, show/hide password toggle,
`?next=` + location.state.from honored (next wins), error copy via apiErrorCopy,
footer "New here? Create an account". Signup (`pages/Signup.jsx`): name/email/
password (8-72 chars enforced client-side w/ live hint), terms-free (none exist —
don't invent), success → toast "Account created" → straight in. Forgot/Reset:
brand header added, reset failure shows inline `Request a new link` button
(the #1 stale-link path), password mismatch validated live.

---

## D0 — Console shell (`components/CpoLayout.jsx` + `contexts/TenantContext.jsx` + `components/CpoAlerts.jsx`)

CpoLayout: `useTheme('volt')`; `.console` grid per layouts.css. Sidebar: brand
(volt bolt + "AmpHive Console"), org name (TenantContext), nav groups —
ungrouped: Dashboard; INFRASTRUCTURE: Chargers, Gateways, Groups, Health;
OPERATIONS: Sessions, Reservations; REVENUE: Pricing, Earnings, Invoices,
Disputes; bottom: Settings. Badges (count-pill): Health = unacked events count,
Disputes = open count, Groups = pending capacity requests — counts from
TenantContext. Active = startsWith. Footer: signed-in email, `Driver app ↗`
(driverOrigin()), Sign out. Mobile: `.console-topbar` (hamburger + page title +
NotificationBell) + off-canvas sidebar + scrim (Esc closes, focus moves in).
TenantContext: fetches `/api/cpo/profile` once + badge counts; 60s usePoll;
exposes {profile, counts, refresh}. CpoAlerts: rendered by the LAYOUT on every
page (not just dashboard): unacked critical/warning events as banners, severity
sorted, `Acknowledge` verb everywhere (kills "Dismiss"), `View all → /cpo/health`.
All 13 console pages render inside CpoLayout and NO LONGER pass tenantName props
or fetch the profile themselves.

## D1 — Console dashboard (`pages/cpo/CpoDashboard.jsx`)

KPI row (all clickable links): Today's revenue ₹ → sessions · Today's energy →
sessions · Active sessions → sessions?status=active · Gateways online x/y →
gateways (badge-danger tint when < total). 30s usePoll. Charts (recharts stays):
revenue area + energy bars (30d), 24h load line — restyle: tokens for colors
(brand for revenue, info for energy), font-data tick labels, custom tooltip
(surface card, ₹ via formatINR), no gradients from the old theme. Recent
sessions: 8-row DataTable → `View all`. Failure of any fetch → ErrorState with
retry for that card only (no more silent zeros).

## D2 — Chargers (`pages/cpo/CpoChargers.jsx`, delete CpoPlugs.jsx)

Toolbar: search by name, filters (effective state via plugAvailability, gateway,
group) + `Add charger`. DataTable: checkbox column (bulk bar appears: assign to
group / set maintenance — loop existing PUTs), name, effective status
(StatusDot combining gateway online + plug status), gateway (name + dot, NOT the
MAC as primary), group, tariff (resolved name or "inherited"), price now, actions:
Edit, QR, Maintenance toggle (labelled button + ConfirmDialog). Edit modal-lg in
SECTIONS: Identity (name, group) / Pricing (tariff select from `/api/cpo/tariffs`
+ assignment via the existing assign endpoint; "Inherited from group/organization"
hint) / Safety (max_current_a with ONE banner-warn: "Below-16A limits are
enforced when starting sessions, not by the plug hardware") / Behavior (queued
charging tri-state, auto-start delay). QR modal: keep deep-link + `.print-area`
print sheet, restyled as an actual bay label matching the marketing hero design.

## D3 — Gateways + Health (`CpoGateways.jsx`, `CpoHealth.jsx`, delete CpoFaults.jsx)

Gateways: table (name, dot+last-seen relative, firmware chip + `badge-warn
"behind"` when < the fleet's max version, plugs count, actions: OTA, rename if
endpoint exists). OTA modal: shows current fw, URL input prefilled from the most
recent OTA audit-log URL if findable else empty, ConfirmDialog with gateway name.
`Add gateway` modal (existing create endpoint) with the registration steps
explained. Health: events DataTable (severity badge, event copy via
eventTypeCopy, gateway NAME resolved from gateways list, time), filters
(severity, unacked-only default ON), checkbox bulk `Acknowledge selected`,
in-maintenance strip on top with `Clear` actions. Socket gateway_alarm →
debounced refetch (>2s apart).

## D4 — Groups (`pages/cpo/CpoGroups.jsx`)

Master-detail: left = group cards (name, badge, plug/member counts, circuit
meter: live load vs limit_a with meter-fill warn>80% danger>95%, `N waiting for
capacity` badge-warn); click → right detail panel (mobile: full-screen):
Access (code display + copy + `New code` ConfirmDialog "every member's saved code
stops working"), Members (list via new members endpoint + remove w/ confirm),
Chargers in this group (names + link to /cpo/chargers?group=), Circuit (limit_a
editor + the same single advisory banner as D2), Danger zone (delete w/ the
"plugs become public" warning). Create/edit modals restyled.

## D5 — Sessions + Reservations (`CpoSessions.jsx`, `CpoReservations.jsx`)

Sessions: KPI strip from server-side `totals` (count/energy/revenue — no more
client-side sums), filters (7/14/30/90d, plug, status), paginated DataTable,
CSV button (existing endpoint), row → detail modal (reuse GET /api/sessions/{id}
as admin/cpo). Reservations: view toggle `.seg` [List | Day]: list = filters +
paginated table + cancel (ConfirmDialog, "the driver will be notified" only if
true — check: cancellation currently sends no notification, so say "the slot
frees up immediately"); Day = CSS-grid timeline, one row per plug, 24h columns,
reservation blocks positioned by time (pure CSS grid math, no dep), day picker.

## D6 — Pricing (`pages/cpo/CpoPricing.jsx`, delete CpoTariffs.jsx)

Three sections: (1) Tariffs list — name, base ₹/kWh, slots count, assigned-to
count; actions Edit (PUT — finally wired), Delete (ConfirmDialog with fallback
explanation). `New tariff` modal. (2) Slot editor per tariff (expand or modal-lg):
existing slot CRUD + weekday chips + **coverage grid**: 7×24 CSS-grid, cells
tinted by the priced slot covering them (base price = neutral), making overlaps
and gaps visible at a glance; auto-split windows that cross midnight into two
slots on submit. (3) Assignment: tenant default tariff select + a compact table
of groups & chargers with their assigned/inherited tariff and a change select
per row (existing assign endpoints).

## D7 — Earnings + Invoices + Disputes (`CpoEarnings.jsx`, `CpoInvoices.jsx`, `CpoDisputes.jsx`)

Earnings: Lifetime + Unsettled cards (₹ primary), ONE settlement explainer
banner-info ("Payouts are records — the transfer itself happens outside AmpHive"),
`Request payout` disabled-with-reason (0 unsettled / already pending), payout
history table. REMOVE the embedded admin mark-paid (admin portal owns it).
Invoices: days filter + paginated table + CSV (new endpoint) + view (res.ok
check, error toast). Disputes: table adds session cost column + driver email,
Resolve modal: approve → refund amount input (default = full session cost,
validate ≤ cost) + optional note; reject → note required; both ConfirmDialog'd;
link each row to the session detail modal.

## D8 — Settings (`pages/cpo/CpoSettings.jsx`)

Stacked cards: Organization (name — editable only if an endpoint exists, else
read-only with "contact support to rename"; timezone display + "used for tariff
slots"), Defaults (tenant defaults for queued charging/auto-start IF the profile
exposes them, else omit the card), GST & invoicing (GSTIN with format validation
`^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}$` soft-warning, legal name, invoice prefix with
banner-warn about numbering), each card has its own Save + toast.

---

## E — Admin portal (`components/AdminLayout.jsx`, `pages/admin/*`)

AdminLayout: `useTheme('volt','admin')` (violet accent), sidebar: Overview,
Tenants, Users, Payouts, Gateways, Disputes, Audit; footer: `Operator console`
link (only if user.tenant_id), `Driver app ↗`, Sign out. Same responsive shell
as CpoLayout (share CSS classes, not the component).
- **Overview**: KPI grid from `/api/admin/stats/overview` (platform revenue ₹,
  energy, active sessions, tenants, users, gateways online, requested payouts ₹,
  open disputes) — each links to its page.
- **Tenants**: search + paginated table (name, users, gateways online/total,
  chargers, 30d sessions, 30d revenue ₹, pending payouts badge) → detail page
  `/admin/tenants/:id` (identity + GST card, KPI row, recent sessions, payouts).
- **Users**: search + role filter, table (email, name, role badge, org,
  ₹ balance, disabled badge, joined); actions: change role (ConfirmDialog:
  "driver → cpo requires an organization" hint), disable/enable (ConfirmDialog,
  "all their sessions and tokens end now"), adjust balance (modal: signed ₹
  amount + required reason, shows resulting balance).
- **Payouts**: status seg (Requested default | Paid | Cancelled | All),
  cross-tenant table (org, requested ₹ gross/fee/net, requested date), `Mark
  paid` → ConfirmDialog ("confirm the transfer happened outside AmpHive first").
- **Gateways**: fleet table (org, name, dot+last seen, firmware chip with
  behind-latest badge, chargers), online/offline filter, firmware-spread mini
  summary (chips: "2.3.0 ×3 · 2.1.0 ×1").
- **Disputes**: cross-tenant read table (org, driver, session ₹, status, age) —
  no resolve actions (tenant CPO resolves; say so in an info banner).
- **Audit**: cross-tenant table (time, org, actor, action, detail), tenant filter.

---

## Shared modal prop contracts (cross-agent imports)

- `ChargeSetupModal({ open, onClose, plug, mode: 'start'|'queue', onConfirm })` —
  mode changes title/button/copy: start = "Set up your charge"/"Start charging";
  queue = "Queue this charge"/"Join the queue" + "starts automatically when the
  circuit has room". Limits: time as preset chips (30m/1h/2h/4h/8h/custom hh:mm)
  — no fractional-hours input; energy field labelled "Energy limit (kWh)";
  coverage line uses the plug's OWN price. `circuit_full` error → inline option
  `Ask for capacity` (existing request-capacity endpoint) with human copy.
- `ReserveModal({ open, onClose, plug, onReserved })` — client-side overlap check
  against fetched windows; fetch failure shows ErrorState INSIDE the modal (not a
  clean-looking empty schedule).
- `DisputeModal({ open, onClose, sessionId, onSubmitted })`.
- All three build on `ui/Modal`.
