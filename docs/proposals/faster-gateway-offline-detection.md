# Proposal: Make a gateway going offline show up fast on the site

Status: **Draft plan (not built).** Options + recommendation for review.
Author: dev + Claude
Date: 2026-07-14

---

## Context

When a gateway is unplugged, the plug can take **~2 minutes** to show offline on the
site. This is two independent delays stacked, and they need different fixes.

**Layer 1 — backend won't declare offline for up to ~120s (by design).**
- The driver-facing "online" is `gateway_is_live()` = status `ONLINE` **and**
  `last_seen_at` within `GATEWAY_LIVENESS_WINDOW_SEC = 120s`
  (`backend/services/session_lifecycle.py:36,79`). `last_seen_at` is refreshed by
  telemetry/status, throttled to once per 60s (`GATEWAY_SEEN_BUMP_INTERVAL_SEC = 60`,
  `backend/services/mqtt_manager.py:23,357`). When telemetry stops, it reads "fresh" for
  the rest of the window → online for **~60–120s** after unplug.
- The hard OFFLINE flag (CPO pages) flips only when the broker fires the gateway's Last
  Will, which waits out the **MQTT keepalive = 60s** (`agent/amphive_agent/core.py:238`);
  per spec the broker declares the client dead at ~1.5× keepalive → **~90s** before
  `_persist_gateway_status` marks it OFFLINE (`mqtt_manager.py:996`).

The lax window is intentional (telemetry is bursty; a tight window flaps plugs offline on
every blip). It is the same conflated per-gateway signal behind
`docs/proposals/queued-charge-offline-plug.md` and audit findings REC-08/REC-09
(`docs/audits/reconciliation-audit-2026-07-14.md`).

**Layer 2 — the frontend is never told; it only updates on a refetch.**
- Driver Home fetches plugs on mount/login only (`frontend/src/pages/Home.jsx:145-149`) +
  a manual Refresh button (`:601`). **No periodic poll.**
- The only live push is Socket.io `plug_status`, and it fires **only for session
  OCCUPIED/AVAILABLE flips** (`Home.jsx:156-169`). A gateway going offline emits **no**
  socket event — `gateway_online` is computed server-side only inside
  `/api/plugs/available` (`backend/routers/plugs.py:221`), and `_persist_gateway_status`
  never calls `emit_plug_status`.
- CPO plug/gateway pages behave the same (fetch on mount + manual/after-action refetch).

So even after the backend knows (Layer 1), the screen won't change until the user
refetches — which is what makes it *feel* like it takes forever.

**Net:** `worst case ≈ (up to ~120s backend detection) + (time until the next page refresh)`.

---

## The three levers

### Lever 1 — Push connectivity live (fixes Layer 2; recommended primary)

Emit a socket event when a gateway's connectivity changes, so plug cards flip without a
refetch. Reuse the existing broadcast plumbing (`emit_plug_status`,
`backend/services/socketio_manager.py:33`).

- **Backend:** in `_persist_gateway_status` (`mqtt_manager.py:996-1022`), after the
  status write, look up the gateway's plugs and emit a new `plug_connectivity`
  (`{plug_id, gateway_online}`) event — on **both** the `offline` (LWT) and `online`
  (reconnect) transitions, so the card flips off *and* back. (The plug list is already
  gathered in `_republish_off_for_orphaned_plugs` / `_notify_drivers_gateway_offline`;
  the same query serves here.)
- **Frontend:** add a `socket.on('plug_connectivity', …)` handler in `Home.jsx` alongside
  the existing `plug_status` one (`:156-169`), updating `gateway_online` in place. Same
  for the CPO plug/gateway pages.
- **Covers the LWT path instantly.** Gap: the pure *silence* case (gateway TCP-connected
  but telemetry wedged, no LWT) still relies on the 120s window and emits nothing. To push
  that too, have the session reaper (or a tiny new sweep) emit `plug_connectivity(offline)`
  when a gateway crosses `GATEWAY_LIVENESS_WINDOW_SEC` with no fresh telemetry — a small
  add, and it dovetails with the per-plug heartbeat in the queued-charge proposal.
- **Effort:** small (one backend emit + one frontend handler); + small for the window-cross
  sweep. **Flap risk:** none — it reflects the same state the backend already computes.

### Lever 2 — Tighten detection (fixes Layer 1)

Lower the timeouts so the backend decides faster.
- `keepalive=60` → e.g. 20–30s in `agent/amphive_agent/core.py:238` (and
  `tools/fake_plug.py:342`, `backend/services/mqtt_manager.py:122` for symmetry) → LWT in
  ~30–45s instead of ~90s.
- `GATEWAY_LIVENESS_WINDOW_SEC` (env, default 120) → e.g. 45–60s, and the throttle
  `GATEWAY_SEEN_BUMP_INTERVAL_SEC` proportionally.
- **Trade-off:** more sensitive to real network blips → more offline↔online flap, more
  reconnect churn, and it also tightens the **session-start gate** and the **reaper**
  staleness behavior (they read the same window). Change deliberately, not aggressively.
- **Effort:** trivial (constants), but needs field validation on real gateways/mobile
  links before lowering much. **Flap risk:** real — this is the knob that trades latency
  for stability.

### Lever 3 — Poll as a catch-all (backstop for Layer 2)

A modest `setInterval` refetch of the plug list on Home/CPO pages (e.g. every 15–30s).
- **Pro:** dead simple; a safety net that also covers anything the socket misses (missed
  event, socket drop, the silence case).
- **Con:** constant background load (a `/api/plugs/available` query per client per tick)
  and still up to one interval of lag. Best as a **low-frequency backstop**, not the
  primary mechanism.
- **Effort:** trivial. **Flap risk:** none.

---

## Recommendation

- **Do Lever 1 as the primary fix** — it removes Layer 2 entirely for the common
  (LWT) case with zero flap-risk change, and is the smallest correct change. Include the
  reaper window-cross emit so the silence case also pushes.
- **Add Lever 3 at a low interval (~30s)** as a cheap backstop for missed events / socket
  drops. Together, Levers 1+3 make the UI reflect reality within seconds of the backend
  knowing, without touching the flap-sensitive Layer-1 constants.
- **Hold Lever 2** unless field data shows the ~90s LWT / 120s window is itself too slow
  for the operator's needs; if so, lower keepalive first (smaller blast radius than the
  liveness window, which the start-gate and reaper also depend on).

This is UX/latency only — none of it changes billing or safety. It slots into the same
area as REC-08/REC-09; REC-08's "notify only on a real ONLINE→OFFLINE transition" guard
should land together with Lever 1's emit (compute the transition once, use it for both the
notification and the socket push).

---

## Verification

Hardware-free via the fake-plug rig (`tools/fake_plug.py`, gateway `fakeplug-gw-01`,
plug_id 2):
1. With Home open, kill the fake plug's agent (ungraceful) and time how long until the
   card flips offline — before: needs a manual refresh + up to ~120s; after Lever 1:
   flips within a second or two of the LWT (~keepalive), no refresh.
2. Restart the agent and confirm the card flips back online live (the `online` emit).
3. Simulate the silence case (agent connected but telemetry stopped) and confirm the
   reaper window-cross emit flips it offline at ~`GATEWAY_LIVENESS_WINDOW_SEC`.
4. Confirm CPO plug/gateway pages update the same way.
