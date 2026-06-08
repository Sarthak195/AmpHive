# AI Agent Guidelines for Headscale

Welcome to the Headscale repository. If you are an AI assistant or developer modifying or contributing to this codebase, please adhere to these guidelines to ensure code consistency, protocol compatibility, and database schema integrity.

---

## 1. Tech Stack & Tools
- **Language**: Go (Golang).
- **Protobuf Compiler**: Buf.
- **ORM**: GORM (Go Object Relational Manager) supporting SQLite and PostgreSQL.
- **API Engine**: gRPC for internal/CLI control, REST/JSON for client node interactions, and Tailscale Noise Protocol over HTTP.
- **Environment**: Nix development shell configuration is provided (`flake.nix`, `nix develop`).

---

## 2. Architectural Rules

1. **Protocol Compliance**:
   - Node-to-server communications must strictly follow the Tailscale protocol specs (including Noise handshakes, MapRequest/MapResponse structures, and coordination polling).
   - Refer to `hscontrol/noise.go` and `hscontrol/poll.go` before altering peer coordination pathways.
2. **Database Integrity**:
   - The SQLite database schema in `hscontrol/db/schema.sql` is the **source of truth** for database validations.
   - Any modifications to models (e.g. `users.go`, `node.go`, `preauth_keys.go` in `hscontrol/db/`) must be validated by running unit tests. Do not bypass the schema checker.
3. **Namespace/User Partitioning**:
   - Nodes must remain bound to a `User` (formerly Namespace) to maintain network encapsulation. Ensure all queries filter node maps and routes by `user_id` unless system-wide routing audits are requested.

---

## 3. Key Files to Understand

- `hscontrol/app.go`: Entry point for server configuration loading, database connections, and HTTP routing initialization.
- `hscontrol/noise.go`: Manages Noise authentication handshakes and stateful crypt-sockets between nodes and the server.
- `hscontrol/poll.go`: Implements client long-polling. Keeps sockets open to stream map changes (new peers, route updates) in real time to connected nodes.
- `hscontrol/db/schema.sql`: Contains the reference SQLite SQL schema.
- `cmd/headscale/cli/`: Holds CLI commands (users, nodes, routes, preauthkeys) wrapping gRPC client calls.

---

## 4. Development Workflow

### Codestyle & Formatting
- Code formatting uses `golines` (width 88) and `gofumpt`. Run formatting before commit:
  ```bash
  make fmt
  ```
- Code linting uses `golangci-lint`. Validate configuration via:
  ```bash
  make lint
  ```

### Protobuf Generation
If you modify Protobuf definitions in `proto/`, you must regenerate Go structures using Buf:
```bash
make generate
```
Commit files under `gen/` separately to simplify pull request reviews.

### Testing & Building
```bash
# Enter nix environment (if nix is installed)
nix develop

# Run unit tests
make test

# Build executable binary
make build
```
---

## 5. Deployment Warnings
- Do not add features that introduce dependencies on third-party SaaS clouds. Headscale is designed to run locally, offline, or self-hosted.
- Ensure CORS configurations, cookie naming, and OIDC tokens are mapped through configuration variables rather than hardcoded.
