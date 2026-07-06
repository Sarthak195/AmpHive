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


# Lightweight, idempotent in-place schema upgrades.
#
# create_all() only creates *missing tables* — it never adds a column to a
# table that already exists. Since this project has no migration tool, columns
# added to existing models must be reconciled here. Each statement is written
# to be safe to run on every startup (IF NOT EXISTS), and to no-op on a fresh
# DB where create_all already produced the column. Postgres-specific DDL,
# which matches the only supported database (asyncpg/Postgres).
def _money_col_upgrade(table: str, column: str) -> str:
    """DDL that converts a legacy ``double precision`` money column to
    ``NUMERIC(12,2)``, but only when it isn't numeric already.

    Guarded by an information_schema check inside a DO block so it is a true
    no-op on repeat startups (a bare ``ALTER COLUMN … TYPE`` would rewrite the
    whole table on every boot). The ``USING …::numeric(12,2)`` cast rounds the
    existing float values to 2 dp — lossless in practice, since every write
    already quantised to 2 dp.
    """
    return (
        f"DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM information_schema.columns "
        f"WHERE table_name='{table}' AND column_name='{column}' "
        f"AND data_type <> 'numeric') THEN "
        f"ALTER TABLE {table} ALTER COLUMN {column} "
        f"TYPE NUMERIC(12,2) USING {column}::numeric(12,2); "
        f"END IF; END $$;"
    )


_INPLACE_UPGRADES = (
    # razorpay_payment_id + UNIQUE: dedupes concurrent /verify + webhook credits.
    "ALTER TABLE ledger_transactions "
    "ADD COLUMN IF NOT EXISTS razorpay_payment_id VARCHAR(64)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_razorpay_payment_id "
    "ON ledger_transactions (razorpay_payment_id)",
    # Money columns: Float → NUMERIC(12,2) so wallet math stops drifting.
    _money_col_upgrade("users", "coin_balance"),
    _money_col_upgrade("charging_sessions", "coins_spent"),
    _money_col_upgrade("ledger_transactions", "amount"),
    _money_col_upgrade("ledger_transactions", "balance_after"),
    # Plug geolocation for the map (falls back to the gateway's coords when NULL).
    "ALTER TABLE plugs ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION",
    "ALTER TABLE plugs ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION",
)


async def init_db():
    """
    Creates all database tables defined in the SQLAlchemy models, then applies
    idempotent in-place column/index upgrades. Called during application startup.
    """
    from sqlalchemy import text
    from backend.database.models import Base
    import logging
    logger = logging.getLogger("amphive.db")

    logger.info("Initializing database tables...")
    async with engine.begin() as conn:
        # Create all tables if they don't exist
        await conn.run_sync(Base.metadata.create_all)
        # Reconcile columns/indexes added to existing tables after their
        # initial create_all (no-ops on a fresh DB and on repeat runs).
        for stmt in _INPLACE_UPGRADES:
            await conn.execute(text(stmt))
    logger.info("Database tables initialized.")
