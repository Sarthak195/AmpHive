"""
Destructive schema (re)initializer — DROPS ALL TABLES then recreates them.

This wipes every row. It is a development/first-boot convenience, NOT a
migration tool and NEVER something to run against a database with real data.
To prevent an accidental production wipe it refuses to run unless explicitly
confirmed:

    AMPHIVE_CONFIRM_DROP=yes python -m backend.database.reset_db
    # or
    python -m backend.database.reset_db --drop

Normal startup uses db.init_db() (Alembic upgrade), not this script.
"""
import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import create_async_engine
from backend.database.models import Base
from backend.database.db import DATABASE_URL


async def main():
    print(f"Target database: {DATABASE_URL}")
    print("This will DROP ALL TABLES and recreate them — every row will be lost.")
    engine = create_async_engine(DATABASE_URL, echo=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("Database tables dropped and recreated.")


def _confirmed() -> bool:
    return (
        os.getenv("AMPHIVE_CONFIRM_DROP", "").lower() in {"yes", "true", "1"}
        or "--drop" in sys.argv
    )


if __name__ == "__main__":
    if not _confirmed():
        sys.exit(
            "Refusing to drop all tables without confirmation.\n"
            "Re-run with AMPHIVE_CONFIRM_DROP=yes or the --drop flag if you are "
            "certain this database holds no data you need."
        )
    asyncio.run(main())
