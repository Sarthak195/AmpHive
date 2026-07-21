# Redesign v3 — implementation contract

Working contract for the `redesign/ui-v3` branch (2026-07-21). Every implementation
agent reads this first. The approved plan lives in the PR description; this file is
the *how* — APIs, shapes, ownership, and rules that keep parallel work coherent.

## 0. Non-negotiables

- **Never** run the app stack or a database locally. Frontend verification = `npm run test` / `lint` / `build` in `frontend/`. Backend = `.venv` pytest (DB-gated tests skip locally by design).
- **CSP**: everything self-hosted/bundled. No CDN scripts/styles/fonts. Only `https://*.razorpay.com` scripts and OSM tile images are allowed externally.
- Do not touch `deploy/k8s/headscale.yaml` (unrelated working-tree deletion) and never `git add` it.
- Roles are exactly `driver | cpo | admin`. Do not invent others.
- **Honesty traps**: payouts move no money in-app (out-of-band transfer, admin marks paid); sub-16A caps are admission-math only — keep the caveat copy; queued charging is feature-flag OFF (render affordance only when the API exposes it, exactly as today's Home does); reservations do not auto-start charging; GST invoices are intra-state HTML only.

## 1. Design system (already committed — do not reinvent)

- `src/styles/tokens.css` — semantic tokens, two themes: `data-theme="day"` (driver + marketing, light/warm) and `data-theme="volt"` (CPO + admin console, charcoal/lime). Admin adds `data-accent="admin"` (violet). **Components use semantic vars only — zero raw `hsl()` literals, zero hex, zero inline `style={{}}` for anything primitives cover.**
- `src/styles/base.css` — reset, type, focus-visible, motion (+ reduced-motion kill-switch), `.print-area` for QR sheets.
- `src/styles/primitives.css` — `.btn(-primary|-quiet|-danger|-danger-solid|-ghost, -sm|-lg|-full|-icon)`, `.field/.input/.select/.textarea/.checkbox-row`, `.card(-tight)/.card-link/.well`, `.dot(-ok|-warn|-danger|-info|-brand|-state-*) .dot-live`, `.badge(-ok|-warn|-danger|-info|-brand)`, `.count-pill`, `.chip`, `.kpi/.kpi-grid`, `.meter/.meter-fill`, `.table-wrap/.table/.table-collapse` (mobile card collapse via `td[data-label]`), `.pagination`, `.tabs/.tab`, `.seg/.seg-item`, `.banner(-info|-warn|-danger|-ok)`, `.overlay/.modal(-sm|-lg)/.modal-title/.modal-actions`, `.toasts/.toast(-ok|-danger|-warn|-info)`, `.state-block`, `.skeleton(-text|-title)`, `.page-header/.page-eyebrow/.page-actions`, `.filter-bar/.filter-count`.
- `src/styles/layouts.css` — `.appbar/.tabbar` (driver chrome), `.console-*` (sidebar shell), `.mkt-*` (marketing), `.page` scaffold, `.has-tabbar`, `.mobile-only/.desktop-only`.
- Page-specific **layout** CSS goes in a co-located file (`src/pages/Wallet.css` imported by `Wallet.jsx`) to avoid merge conflicts. Identity (color/type/elevation) always comes from tokens/primitives.
- Icons: import from `lucide-react` directly (`<Zap size={18} aria-hidden="true" />`). No emoji as UI glyphs anywhere. Decorative icons get `aria-hidden`.
- Fonts (self-hosted, imported once in `main.jsx`): Inter 400/500/600/700, Bricolage Grotesque 600/700/800, JetBrains Mono 400/500/700.

## 2. Shared UI components (`src/components/ui/`) — the exact APIs

```jsx
<Modal open onClose title size="sm|md|lg" footer={<.../>}>…</Modal>
   // portal to body, role="dialog" aria-modal, focus trap, Esc closes,
   // overlay click closes, restores focus to trigger on close
<ConfirmDialog open onClose onConfirm title body confirmLabel tone="danger|primary" busy />
   // replaces every window.confirm in the app
const toast = useToast();  toast.ok(msg); toast.error(msg); toast.info(msg); toast.warn(msg)
   // <ToastProvider> wraps app in main.jsx; aria-live="polite" region; 5s auto-dismiss
<Tabs tabs={[{id,label,count?}]} active onChange ariaLabel />
<DataTable columns={[{key,label,num?,render?(row)}]} rows keyField
           loading error onRetry emptyIcon emptyTitle emptyBody emptyAction
           onRowClick? pagination={{total,offset,limit,onPage}} collapse />
   // skeleton rows while loading; ErrorState (retry) ≠ EmptyState — never conflate;
   // collapse renders td data-label for the <720px card mode
<EmptyState icon={LucideComp} title body action={<.../>} />
<ErrorState error onRetry />       // "Couldn't load X" + Retry button — REQUIRED
                                   // wherever data is fetched; no more silent console.error
<Skeleton lines=3 /> <SkeletonTitle />
<StatusDot state="available|in_use|unpowered|offline|maintenance" live label />
<Money coins /> or <Money inr />   // ₹-first: renders formatINR(); coins only in tooltips/asides
<PageHeader eyebrow title sub actions={<.../>} />
```

Utils:
- `utils/money.js`: `formatINR(n)` (en-IN grouping, "₹1,240.50"), `coinsToINR(coins, rate)`, `formatKwh(n)`, `formatKw(w)`, `formatDuration(secs)` ("1h 24m").
- `utils/statusCopy.js`: single source for machine→human copy — `plugStateLabel`, `sessionStatusLabel`, `stopReasonCopy`, `txTypeLabel`, `eventTypeCopy` (`OVERCURRENT_CUTOFF` → "Current safety cutoff"), `apiErrorCopy(err)` (maps `err.code`: `circuit_full` → "This circuit is at capacity right now", gateway offline → "This charger can't be reached right now", plus fallback to `err.message`). Extend the table rather than writing ad-hoc strings.
- `utils/plugAvailability.js` (updated in Phase A): adds `maintenance` as 5th state; colors come from `--state-*` tokens; labels from statusCopy.

Data patterns (uniform across all pages):
1. Fetch → skeleton → **ErrorState with retry** on failure (never an empty state), EmptyState only for true zero-data.
2. Mutations → button `busy` state → `toast.ok()` on success / `toast.error(apiErrorCopy(err))` on failure.
3. Destructive/billing actions (stop charge, cancel reservation, delete, refund, regenerate code) → `ConfirmDialog` stating the concrete consequence.
4. Money renders through `<Money/>`/`formatINR` — ₹ everywhere, "coins" demoted to secondary copy where the 1:1 needs stating (rate from ConfigContext, never hardcoded).
5. Live/polled data: `usePoll(fn, ms)` hook (Phase A provides in `src/hooks/usePoll.js`) — poll only while tab visible (`document.visibilityState`).

## 3. Routing + theme application

- `App.jsx` (Phase A) owns all route trees (driver host / cpo host / unsplit) with `React.lazy` chunks per surface. Theme: layouts call `useTheme('day'|'volt', {accent})` (Phase A hook) which stamps `document.documentElement.dataset`.
- Driver host: `/` = `user ? <Dashboard/> : <Marketing/>`; `/map`, `/session`, `/wallet`, `/activity`, `/groups`, `/account`, `/login`, `/signup`, `/forgot-password`, `/reset-password`. Redirects: `/topup`→`/wallet`, `/history`→`/activity`. **`/?plug=<id>` deep-link must keep working** (printed QR labels): anon → login with `state.from` preserved; authed → Dashboard opens the charge flow.
- CPO host: `/cpo/dashboard`, `/cpo/chargers` (was plugs), `/cpo/gateways`, `/cpo/groups`, `/cpo/health` (was faults), `/cpo/sessions`, `/cpo/reservations`, `/cpo/pricing` (was tariffs), `/cpo/earnings`, `/cpo/invoices`, `/cpo/disputes`, `/cpo/settings`, `/cpo` (setup). Redirects: `/cpo/plugs`→`/cpo/chargers`, `/cpo/faults`→`/cpo/health`, `/cpo/tariffs`→`/cpo/pricing`. Admin: `/admin`, `/admin/tenants(/:id)`, `/admin/users`, `/admin/payouts`, `/admin/gateways`, `/admin/disputes`, `/admin/audit` — `AdminProtectedRoute`; `CpoLanding` routes role=admin → `/admin`.
- Unsplit host (bare IP/localhost fallback): all of the above internally. Every new route must appear in the applicable trees.

## 4. New/changed backend endpoints (build frontend against these shapes)

Auth header everywhere; errors follow existing FastAPI patterns. Paginated lists return `{ total, items: [...] }`; `limit`/`offset` query params.

**Admin** (`backend/routers/admin.py`, all `require_role('admin')`):
- `GET /api/admin/stats/overview` → `{tenants, users:{total,drivers,cpos,admins}, gateways:{total,online}, plugs:{total}, sessions:{active,today,total}, energy_kwh:{today,total}, revenue_coins:{today,total}, payouts:{requested_count,requested_net_coins}, disputes:{open}}`
- `GET /api/admin/tenants?q=&limit=&offset=` → items: `{id,name,created_at,user_count,gateway_count,gateways_online,plug_count,sessions_30d,revenue_30d_coins,pending_payouts}`
- `GET /api/admin/tenants/{id}` → the row above + `{gst_number,legal_name,default_tariff_id,recent_sessions:[…10],payouts:[…]}`
- `GET /api/admin/users?q=&role=&limit=&offset=` → items: `{id,email,full_name,role,tenant_id,tenant_name,coin_balance,is_disabled,created_at}`
- `PATCH /api/admin/users/{id}` body `{role?,is_disabled?}` (403 on self-demote/self-disable; bumps token_version; audited)
- `POST /api/admin/users/{id}/adjust-balance` body `{amount_coins,reason}` (± amount; ledger row; balance floor 0; audited) → `{new_balance}`
- `GET /api/admin/payouts?status=` → items: existing payout fields + `tenant_name`
- `GET /api/admin/gateways?online=&limit=&offset=` → items: `{id,gateway_id,name,tenant_id,tenant_name,online,last_seen_at,firmware_version,plug_count}`
- `GET /api/admin/disputes?status=` → items: dispute + `{tenant_name,user_email,session_cost_coins}`
- `GET /api/admin/audit?tenant_id=&limit=&offset=`

**Driver gaps**: `GET /api/sessions/{id}` (receipt shape of the stop response + status; owner/tenant-cpo/admin) · `GET /api/sessions/history?limit=&offset=` → `{total,items}` with `plug_name` added · `GET /api/me/stats` → `{month:{energy_kwh,spend_coins,sessions}, lifetime:{…}}` · `GET /api/plugs/{id}/tariff-preview` → `{base_price_per_kwh, price_now, slots:[{days,start_minute,end_minute,price_per_kwh}]}` (any JWT) · `GET /api/sessions/disputes/my` · `DELETE /api/groups/{id}/leave`

**CPO gaps**: `GET /api/cpo/groups/{id}/members` → `[{user_id,email,full_name,joined_at}]` + `DELETE /api/cpo/groups/{id}/members/{user_id}` · pagination (`offset`, `total`) + server-side filtered `totals:{count,energy_kwh,revenue_coins}` on `/api/cpo/analytics/sessions` · `offset+total` on events/reservations/invoices lists · `GET /api/cpo/invoices.csv` (mirrors sessions.csv) · verify/extend `PUT /api/cpo/profile` for tenant defaults only if columns already exist.

**Wire-up only (endpoints already exist)**: `PUT /api/cpo/tariffs/{id}`, tariff assignment (plug/group/tenant-default via `CpoTariffAssignRequest` shapes in `backend/routers/cpo.py` ~line 1760+), dispute resolve with partial `refund_coins` + `note`.

Migration `0025_user_disable.py`: `users.is_disabled` boolean NOT NULL DEFAULT false; enforced in login + `get_current_user` (clear 403 "account disabled" detail).

## 5. Testing + quality bar (every agent, every page)

- Rewrite the page's co-located `*.test.jsx` to the new UI (behavioral: renders states, actions call APIs, errors surface). Mock `api` via `vi.mock`; copy patterns from existing tests. Run `npx vitest run src/pages/<file>.test.jsx` (or component path) until green, plus `npx eslint <files you touched>`.
- A11y floor: interactive = real `<button>`/`<a>` (no clickable divs), labels on inputs, `aria-live` where numbers move, AA contrast (use tokens — they're pre-checked), keyboard path for every action, semantic heading order (one `h1` per page).
- Copy: sentence case; verbs on buttons ("Save changes", "Start charging"); errors say what happened + what to do; no jargon (gateway MACs, MQTT, "tenant", raw enums) in driver-facing surfaces.
- No new dependencies. No `Date.now()` seeds in render paths that break tests. Keep components small; extract subcomponents rather than 999-line pages.

## 6. File ownership (do not edit files another agent owns)

- Phase A (shell): `App.jsx`, `main.jsx`, `index.html`, `public/manifest.webmanifest`, `api/client.js`, `contexts/*`, `components/Navbar.jsx→AppBar`, `components/MobileTabBar.jsx`, `components/ErrorBoundary.jsx`, `components/BootSplash.jsx`, `components/ui/*`, `hooks/*`, `utils/money.js`, `utils/statusCopy.js`, `utils/plugAvailability.js`, stubs for all new pages.
- Phase C (driver, one agent per page): `pages/Marketing.jsx`, `pages/Dashboard.jsx` (+`components/PlugCard.jsx`, `components/ChargeSetupModal.jsx`, `components/ReserveModal.jsx`), `pages/MapPage.jsx` (+`components/MapComponent.jsx`), `pages/Session.jsx` (+`SessionMonitor/SessionReceipt/ChargeRing`), `pages/Wallet.jsx`, `pages/Activity.jsx` (+`DisputeModal`), `pages/Groups.jsx`+`pages/Account.jsx`, auth pages.
- Phase D (console): `components/CpoLayout.jsx`+`contexts/TenantContext.jsx`+`components/CpoAlerts.jsx` first, then one agent per page under `pages/cpo/`.
- Phase E (admin): `components/AdminLayout.jsx`, `pages/admin/*`.
- Shared file conflicts are resolved by NOT sharing: page CSS is co-located; new shared needs go through the orchestrator.
