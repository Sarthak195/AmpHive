"""Alembic environment — async (asyncpg), shares the app's models + URL.

Invoked two ways:
- programmatically at app startup (backend.database.db.init_db builds the
  Config and runs `upgrade head` in a worker thread), and
- via the CLI for authoring: `alembic -c backend/alembic.ini revision ...`
  (autogenerate needs a reachable database — use the CI postgres service or
  the VM; this repo's dev boxes run no local DB by policy).
"""
import asyncio
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make `backend.*` importable regardless of invocation cwd: this file lives at
# <root>/backend/migrations/env.py both in the repo and in the Docker image.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database.models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from backend.database.db import DATABASE_URL
    return DATABASE_URL


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (--sql mode)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = async_engine_from_config(
        {"sqlalchemy.url": _database_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    # Callers must not have a running event loop on this thread; init_db()
    # guarantees that by invoking alembic commands in a worker thread.
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
