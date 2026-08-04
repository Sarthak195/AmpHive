"""
AmpHive Database Connection Layer
=================================
Async SQLAlchemy engine and session factory for PostgreSQL (Cloud SQL).
Provides a FastAPI dependency (get_db) to inject database sessions into routes.

Design decisions:
- Using asyncpg driver for non-blocking I/O (matches FastAPI's async model).
- Connection pool: 5 base connections, overflow up to 10 — suitable for the
  e2-highcpu-4 VM's 4GB RAM. Increase if scaling to K8s multi-replica.
- Pool pre-ping enabled to detect and discard stale connections from Cloud SQL
  idle timeouts.
"""

import logging
import os
import re
from typing import AsyncGenerator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_log = logging.getLogger("amphive.db")

# Read the database URL from environment. Expected format:
# postgresql+asyncpg://user:password@host:port/dbname
#
# The fallback below is a LOCAL DEV credential only (guessable password,
# "amphive_dev") — local dev/tests rely on it, so it is NOT removed or
# swapped for something safer here (unlike services/auth.py's
# JWT_SECRET_KEY guard, there's no safe auto-generated substitute for a DB
# password: a broken/garbage DATABASE_URL must fail on the actual connection
# attempt, not be silently patched). Mirrors that guard's logging style —
# loud instead of silent when the weak default is in play.
_DEV_DATABASE_URL_DEFAULT = "postgresql+asyncpg://postgres:amphive_dev@localhost:5432/amphive"
_env_database_url = os.getenv("DATABASE_URL")
# DATABASE_URL is the OWNER connection: it holds DDL rights and is used ONLY for
# migrations (alembic upgrade) and one-time least-privilege-role provisioning at
# startup — never to serve requests (see RUNTIME_DATABASE_URL below).
DATABASE_URL = _env_database_url or _DEV_DATABASE_URL_DEFAULT
if not _env_database_url or "amphive_dev" in DATABASE_URL:
    _log.critical(
        "DATABASE_URL is unset (falling back to the local-dev default) or "
        "still uses the known-insecure 'amphive_dev' password. This is "
        "expected in local development/tests, but a real deployment MUST "
        "set a strong DATABASE_URL in the environment/.env to fix this."
    )

# ── Least-privilege runtime role (DB privilege separation) ────────────────────
# SECURITY: the audit flagged that request-serving ran on the DATABASE_URL role,
# which on prod is the Postgres SUPERUSER (full DDL + COPY..TO PROGRAM). So any
# future SQL-injection bug would escalate straight to DROP TABLE / DB-host RCE.
# Fix: serve requests through a separate NON-superuser role (`amphive_app`) that
# holds only SELECT/INSERT/UPDATE/DELETE — it *cannot* DROP/ALTER a table — while
# the owner role keeps its DDL rights for migrations alone.
#
# Activation is a single env var and is SAFE-INERT by default: with
# APP_DB_PASSWORD unset, RUNTIME_DATABASE_URL == DATABASE_URL and behavior is
# byte-identical to before this change. When APP_DB_PASSWORD is set, the runtime
# engine connects as APP_DB_USER (default "amphive_app"), and init_db()
# idempotently provisions that role (create-if-missing, set password, grant DML)
# using the owner connection right after migrations, then verifies the runtime
# connection works before serving. See docs/SECURITY.md + deploy/config/.env.template.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
APP_DB_USER = os.getenv("APP_DB_USER", "amphive_app")
_APP_DB_PASSWORD = os.getenv("APP_DB_PASSWORD")
# Separation is on iff a runtime password is provided AND the owner URL parses.
SEPARATION_ACTIVE = bool(_APP_DB_PASSWORD)

try:
    _owner_url = make_url(DATABASE_URL)
    _DB_NAME = _owner_url.database
except Exception:  # pragma: no cover - malformed URL fails later at connect
    _owner_url = None
    _DB_NAME = None

if SEPARATION_ACTIVE:
    if not _IDENT_RE.match(APP_DB_USER):
        raise RuntimeError(
            f"APP_DB_USER '{APP_DB_USER}' is not a valid SQL identifier "
            "([A-Za-z_][A-Za-z0-9_]*) — refusing to build a runtime role name from it."
        )
    RUNTIME_DATABASE_URL = _owner_url.set(
        username=APP_DB_USER, password=_APP_DB_PASSWORD
    ).render_as_string(hide_password=False)
else:
    RUNTIME_DATABASE_URL = DATABASE_URL

# Create the async engine with connection pooling. This is the RUNTIME engine —
# every get_db() request session uses it, so it connects as the least-privilege
# role when separation is active.
# pool_pre_ping=True: issues a lightweight "SELECT 1" before reusing a pooled
# connection, preventing errors from Cloud SQL's idle connection reaper.
engine = create_async_engine(
    RUNTIME_DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=False,  # Set to True for SQL query logging during debugging
)

# Session factory — produces AsyncSession instances bound to the engine.
# expire_on_commit=False: prevents lazy-load errors when accessing attributes
# on ORM objects after a commit (common in FastAPI response serialization).
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async database session.
    Usage in routes:
        @app.get("/api/example")
        async def example(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(User))
            ...
    The session is automatically closed after the request completes.
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


# --- Schema management: Alembic (adopted 2026-07-07) --------------------------
# The old create_all() + hand-written _INPLACE_UPGRADES path is retired; their
# combined result is frozen in migrations/versions/0001_baseline.py. All
# future schema changes ship as Alembic revisions (backend/migrations/).


def alembic_config():
    """Programmatic Alembic config sharing the app's DATABASE_URL.

    Path-derived so it works both from a repo checkout (<root>/backend/...)
    and inside the Docker image (/app/backend/...).
    """
    from pathlib import Path

    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    return cfg


async def _provision_runtime_role(owner_conn, role: str, password: str, dbname: str) -> None:
    """Idempotently ensure the least-privilege runtime role exists with only DML
    rights. Runs on the OWNER connection (which has CREATEROLE/GRANT) right after
    migrations, so GRANT ... ON ALL TABLES covers the freshly-migrated schema and
    ALTER DEFAULT PRIVILEGES covers tables future migrations create (as the same
    owner). The role gets NO DDL — it cannot DROP/ALTER a table — which is the
    whole point: a future SQLi caps out at row-level DML, never schema loss.

    OBJECT-CLASS LIMIT (read before adding a migration): the grant covers
    tables/views/sequences created by THIS owner in schema `public` only. It does
    NOT reach materialized views, objects in a new schema, or objects created by
    a different role — those would give the runtime role "permission denied" at
    runtime (invisible to CI, which runs as the owner). If a migration adds any,
    grant to APP_DB_USER explicitly. See docs/TODO.md "2026-08-04" backlog.

    Injection-safe: `role`/`dbname` are validated SQL identifiers double-quoted
    into the DDL; the password (the only free-form value) is quoted server-side
    via Postgres `format('%L', ...)`, never string-built here.
    """
    from sqlalchemy import text

    if not _IDENT_RE.match(role):
        raise RuntimeError(f"APP_DB_USER '{role}' is not a valid SQL identifier.")
    if not dbname or not _IDENT_RE.match(dbname):
        raise RuntimeError(
            f"Database name '{dbname}' from DATABASE_URL is not a simple identifier; "
            "cannot safely GRANT CONNECT — provision the runtime role manually."
        )

    exists = (
        await owner_conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        )
    ).scalar()
    if not exists:
        await owner_conn.execute(
            text(f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT')
        )
    # Set/refresh the password each boot (self-heals a rotated APP_DB_PASSWORD)
    # via Postgres-side literal quoting — never string-built from the env value.
    # cast(... AS text): asyncpg can't infer the type of a bare parameter passed
    # to the variadic "any" args of format() (IndeterminateDatatypeError) — the
    # explicit text cast pins it. %I quotes as identifier, %L as a literal, both
    # server-side, so ANY password is escaped safely (never string-built here).
    alter_sql = (
        await owner_conn.execute(
            text(
                "SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE PASSWORD %L', cast(:r AS text), cast(:p AS text))"
            ),
            {"r": role, "p": password},
        )
    ).scalar()
    await owner_conn.execute(text(alter_sql))

    for stmt in (
        f'GRANT CONNECT ON DATABASE "{dbname}" TO "{role}"',
        f'GRANT USAGE ON SCHEMA public TO "{role}"',
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}"',
        f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{role}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
        f'GRANT USAGE, SELECT ON SEQUENCES TO "{role}"',
    ):
        await owner_conn.execute(text(stmt))


async def init_db():
    """
    Bring the schema to the current Alembic head. Called at app startup.

    Bootstrap case: a database created by the pre-Alembic path (create_all +
    in-place upgrades) already has every table but no alembic_version — it is
    STAMPED to the baseline revision instead of re-running it. Fresh databases
    execute the baseline + any later revisions.

    Alembic's command API is synchronous and env.py calls asyncio.run(), so
    both commands run in a worker thread (no event loop there).

    Migrations + role provisioning run on a dedicated OWNER engine (DATABASE_URL),
    NOT the runtime `engine` — which, when privilege separation is active, is the
    least-privilege role that has no DDL rights and may not exist yet on first boot.
    """
    import asyncio
    import logging

    from alembic import command
    from sqlalchemy import inspect, text

    logger = logging.getLogger("amphive.db")
    cfg = alembic_config()

    def _inspect(sync_conn):
        inspector = inspect(sync_conn)
        return inspector.has_table("alembic_version"), inspector.has_table("users")

    owner_engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, echo=False)
    try:
        async with owner_engine.connect() as conn:
            has_alembic, has_users = await conn.run_sync(_inspect)

        loop = asyncio.get_running_loop()
        if has_users and not has_alembic:
            # Stamp the BASELINE (not head): the pre-Alembic schema equals the
            # baseline by construction, and any revisions added later must still
            # be applied by the upgrade below.
            logger.warning(
                "Pre-Alembic database detected (tables exist, no alembic_version) — "
                "stamping baseline revision."
            )
            await loop.run_in_executor(None, command.stamp, cfg, "0001_baseline")

        logger.info("Applying Alembic migrations (upgrade head)...")
        await loop.run_in_executor(None, command.upgrade, cfg, "head")
        logger.info("Database schema is at Alembic head.")

        if SEPARATION_ACTIVE:
            # Provision the least-privilege runtime role AFTER migrations so the
            # GRANT covers every existing table, then prove the runtime engine can
            # actually connect as it before we start serving requests.
            async with owner_engine.begin() as conn:
                await _provision_runtime_role(
                    conn, APP_DB_USER, _APP_DB_PASSWORD or "", _DB_NAME
                )
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.critical(
                "DB privilege separation ACTIVE — requests serve as non-superuser "
                "role '%s' (DML only; cannot DROP/ALTER tables). Migrations still "
                "run as the DATABASE_URL owner.",
                APP_DB_USER,
            )
        else:
            logger.critical(
                "DB privilege separation NOT active — requests serve as the "
                "DATABASE_URL role (superuser in prod). Set APP_DB_PASSWORD to "
                "enable a least-privilege runtime role that cannot DROP tables."
            )
    finally:
        await owner_engine.dispose()
