"""
users.email is unique case-insensitively at the DB level (migration 0043).

Needs a real PostgreSQL (CI's postgres service, exported as TEST_DATABASE_URL);
skipped locally. Builds the schema with `alembic upgrade head`, so it exercises
the migration itself, then proves a case-variant duplicate is rejected.
"""
import asyncio
import os

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)


@pytest.mark.asyncio
async def test_case_variant_duplicate_email_is_rejected():
    from alembic import command
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.tests.test_migrations import _make_config, _wipe

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        await _wipe(engine)
        cfg = _make_config(TEST_DATABASE_URL)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, command.upgrade, cfg, "head")

        insert = text(
            "INSERT INTO users (email, hashed_password, full_name, role, coin_balance) "
            "VALUES (:email, 'x', 'Test', 'driver', 0)"
        )
        async with engine.begin() as conn:
            await conn.execute(insert, {"email": "driver@example.com"})

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(insert, {"email": "Driver@Example.com"})
    finally:
        await engine.dispose()
