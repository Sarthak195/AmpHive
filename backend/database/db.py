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

import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from typing import AsyncGenerator

# Read the database URL from environment. Expected format:
# postgresql+asyncpg://user:password@host:port/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:amphive_dev@localhost:5432/amphive")

# Create the async engine with connection pooling.
# pool_pre_ping=True: issues a lightweight "SELECT 1" before reusing a pooled
# connection, preventing errors from Cloud SQL's idle connection reaper.
engine = create_async_engine(
    DATABASE_URL,
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


async def init_db():
    """
    Bring the schema to the current Alembic head. Called at app startup.

    Bootstrap case: a database created by the pre-Alembic path (create_all +
    in-place upgrades) already has every table but no alembic_version — it is
    STAMPED to the baseline revision instead of re-running it. Fresh databases
    execute the baseline + any later revisions.

    Alembic's command API is synchronous and env.py calls asyncio.run(), so
    both commands run in a worker thread (no event loop there).
    """
    import asyncio
    import logging

    from alembic import command
    from sqlalchemy import inspect

    logger = logging.getLogger("amphive.db")
    cfg = alembic_config()

    def _inspect(sync_conn):
        inspector = inspect(sync_conn)
        return inspector.has_table("alembic_version"), inspector.has_table("users")

    async with engine.connect() as conn:
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
