# Contributing to AmpHive

Thanks for your interest in AmpHive. This document is the practical, day-to-day
counterpart to [AGENTS.md](AGENTS.md) (repo rules and conventions for AI agents
and humans alike) and [`docs/`](docs/) (the technical source of truth). Read
those first if you haven't.

> **Before you touch hardware:** read [SAFETY.md](SAFETY.md). This codebase
> controls high-voltage, high-current equipment, is not electrically
> certified, and must not be deployed to control real charging hardware.

---

## Prerequisites

- Python 3.11
- Node.js 20
- Git
- (Optional) Docker Desktop, only if you want to run the full containerized
  stack locally — see the note below on local development.

## Local setup

```bash
git clone https://github.com/Sarthak195/AmpHive.git
cd AmpHive
```

**Backend:**

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1   |   Linux/macOS:  source .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
```

**Frontend:**

```bash
cd frontend
npm ci
npm run dev   # Vite dev server at http://localhost:5173
```

> **Do not run the full application stack (backend + PostgreSQL + MQTT
> broker) or a database locally.** This project's convention — see
> [AGENTS.md](AGENTS.md) — is that the shared, canonical environment is the
> GCP VM, operated through `deploy/scripts/deploy.ps1`. The backend test
> suite below does not need a live database for the non-DB-gated tests, and
> the frontend dev server runs independently of the backend.

## Running tests

**Backend** (from the repo root, with the venv above activated):

```bash
pytest backend/tests
```

Most tests run without any external services. A subset are **DB-gated**
(they need a live PostgreSQL) and are skipped automatically when no database
is available — they run in CI, where a Postgres service container is
provisioned. To also measure coverage locally the way CI does:

```bash
pytest backend/tests -q --cov=backend --cov-report=term-missing
```

CI enforces a coverage floor (`--cov-fail-under=66`); since CI also runs the
DB-gated tests, its measured coverage is higher than what you'll see locally.

**Frontend:**

```bash
cd frontend
npx vitest run   # or: npm test
```

## Linting and type-checking

**Backend:**

```bash
ruff check backend/
mypy
```

`ruff` and `mypy` are both configured in the root [`pyproject.toml`](pyproject.toml)
(mypy is currently scoped to a subset of `backend/services` and
`backend/routers` — see that file for the exact list and rationale).

**Frontend:**

```bash
cd frontend
npm run lint
```

All of the above (`ruff check backend/`, `mypy`, `pytest backend/tests --cov`,
`npm run lint`, `npm test`) run in CI on every push and pull request — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). A PR won't merge with
a red CI run.

## Branch and PR conventions

- Branch off `main`, using a short descriptive name (e.g.
  `fix/session-stop-race`, `feat/reservation-reminders`).
- Open a pull request against `main`. Keep PRs focused — one logical change
  per PR is easier to review and easier to revert.
- CI (backend tests + lint + type-check, frontend tests + lint) must be green
  before merge.
- Write commit messages and PR descriptions that explain *why*, not just
  *what*.

## Code style

- **Backend:** Python 3.11, FastAPI, SQLAlchemy 2.0 (async). Follow the
  existing router/service/schema split (routes in `backend/routers/*.py`,
  business logic in `backend/services/`, Pydantic schemas in
  `backend/schemas.py`) rather than adding logic inline in route handlers.
  Formatting/lint rules are enforced by `ruff` (config in `pyproject.toml`).
- **Frontend:** React 19 + Vite, plain hand-written CSS (no CSS framework).
  Follow the existing component/page structure under `frontend/src/`.
  Enforced by `eslint` (config in `frontend/eslint.config.js`).
- **Firmware:** ESP-IDF (C), targets ESP32-C3. See
  [docs/FIRMWARE.md](docs/FIRMWARE.md) before making changes.

## Documentation

**`docs/` is the single source of truth — update it, not scattered copies.**
If your change affects behavior, update the relevant file in `docs/` (start
at [docs/README.md](docs/README.md) for the index) in the same PR, and record
any works/stub/aspirational status change in
[docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md). Do not create
new top-level "map" documents at the repo root (`architecture.md`,
`api-map.md`, `routes.md`, etc.) — that duplication is exactly what caused
the docs to drift before this convention was adopted. Infrastructure changes
made against the live GCP VM should be logged in the matching runbook under
[`deploy/docs/`](deploy/docs/).

Product vision and roadmap live in [requirements.md](requirements.md) and
[features_list.md](features_list.md), separate from `docs/`, which describes
current, verified behavior.

## Safety

This project controls real electrical hardware. Read
[SAFETY.md](SAFETY.md) before working on firmware, the agent, or any
session/billing/current-limiting code, and **do not deploy this software to
control real charging equipment.**

## Getting help

Start with [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) to
see what's implemented, stubbed, or aspirational, and
[docs/SECURITY.md](docs/SECURITY.md) for known open security gaps. If
something in `docs/` looks stale or wrong, open an issue or PR — it's meant
to reflect the code as it actually is today.
