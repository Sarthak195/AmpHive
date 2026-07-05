# CLAUDE.md

See [AGENTS.md](AGENTS.md) for how to work in this repo, and [`docs/`](docs/) for
the technical source of truth (start with
[docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) and
[docs/SECURITY.md](docs/SECURITY.md)).

**Non-negotiables:** don't run the app stack or a database locally — deploy/operate
the GCP VM from this box instead (via `deploy/scripts/deploy.ps1` / `gcloud`), and
confirm before destructive VM actions; don't commit secrets; keep `docs/` — not
scattered copies — up to date. Full details in [AGENTS.md](AGENTS.md).
