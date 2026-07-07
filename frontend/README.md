# AmpHive Frontend

React 19 + Vite single-page app for the AmpHive shared EV-charging network:
driver flows (wallet top-up via Razorpay, plug discovery on a Leaflet map,
live session monitoring over Socket.io) and a CPO operator portal
(gateways, plugs, groups, session analytics via Recharts).

> Canonical technical docs live in [`../docs/`](../docs/) — start with
> [IMPLEMENTATION_STATUS.md](../docs/IMPLEMENTATION_STATUS.md) and
> [API_REFERENCE.md](../docs/API_REFERENCE.md). This README only covers
> working in this package.

## Commands

```bash
npm install
npm run dev      # Vite dev server (http://localhost:5173)
npm run lint     # ESLint (same check CI runs)
npm run build    # production build to dist/
npm run preview  # serve the production build locally
```

Note: this repo's workflow does **not** run the app stack locally — the
frontend is served from the GCP VM (deployed via
`deploy/scripts/deploy.ps1`, which builds the Docker image from this
source). `npm run dev` is only useful against a reachable backend.

## Environment

| Variable | Purpose |
|----------|---------|
| `VITE_API_URL` | Backend base URL. Empty (default) = same-origin, which is how production serves it. |
| `VITE_RAZORPAY_KEY_ID` | Razorpay checkout key (falls back to the key returned by the backend's create-order response). |

## Layout

```
src/
├── api/client.js       # fetch wrapper: JWT header, JSON errors
├── contexts/           # AuthContext (JWT/user), SessionContext (active
│                       #   sessions + focused live session over Socket.io)
├── components/         # SessionMonitor, WalletCard, MapComponent (Leaflet), …
├── pages/              # Home, Login, Session, TopUp, Groups, History
│   └── cpo/            # operator portal (Setup, Dashboard, Plugs, Groups,
│                       #   Sessions) behind CpoProtectedRoute
└── styles/
```

Conventions worth knowing:

- All app code is plain `.jsx`/`.js` (a TypeScript adopt-or-remove decision
  is tracked in [docs/TODO.md](../docs/TODO.md), TD#14).
- Live telemetry is Socket.io only — connect with the JWT in the auth
  payload, then `subscribe_session` with a session id (see the Socket.io
  events reference in [API_REFERENCE.md](../docs/API_REFERENCE.md)).
- A user can hold up to 2 concurrent sessions (backend-enforced);
  `SessionContext` tracks the full `activeSessions` list plus one *focused*
  session that drives the live monitor.
- There are no frontend tests yet (planned: Vitest + React Testing Library —
  see TESTING Phase 4 in [docs/TODO.md](../docs/TODO.md)).
