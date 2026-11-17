"""
Migration integrity tests — need a real PostgreSQL (CI's postgres:15 service,
exported as TEST_DATABASE_URL). Skipped locally: this repo's dev boxes run no
database by policy.

What's proven here:
1. `alembic upgrade head` builds a schema on an empty database, and
2. that schema matches backend/database/models.py — i.e. the frozen baseline
   DDL hasn't drifted from the models. Any model change without a matching
   revision fails this test.
   Every native enum's pg_enum labels also equal what SQLAlchemy sends for
   the model (values_callable honoured), so an ALTER TYPE ... ADD VALUE
   migration that drifts from its Python enum fails here.
3. The pre-Alembic bootstrap: a database built the old way (create_all) gets
   stamped, not re-migrated, by init_db().
"""
import asyncio
import os

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]

# Diff kinds that mean the baseline and the models disagree structurally.
# Name-only constraint/index variations are tolerated (inline UNIQUE vs named
# constraint reflect differently); missing tables/columns/indexes are not.
STRUCTURAL_KINDS = {
    "add_table", "remove_table",
    "add_column", "remove_column",
    "add_index", "remove_index",
}


def _make_config(url: str):
    from backend.database import db as db_module

    cfg = db_module.alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _wipe(engine):
    """Drop everything the baseline (or create_all) may have left behind."""
    from sqlalchemy import text

    from backend.database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))


def _diff_against_models(sync_conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from backend.database.models import Base

    ctx = MigrationContext.configure(sync_conn)
    return compare_metadata(ctx, Base.metadata)


def _structural(diffs):
    flat = []
    for d in diffs:
        # compare_metadata mixes tuples and lists of tuples
        items = d if isinstance(d, list) else [d]
        for item in items:
            kind = item[0] if isinstance(item, tuple) else str(item)
            if kind in STRUCTURAL_KINDS:
                flat.append(item)
    return flat


def _model_enum_labels():
    """{pg enum type name: labels SQLAlchemy sends} for every named Enum column
    in the models (`type.enums` honours values_callable)."""
    from sqlalchemy import Enum

    from backend.database.models import Base

    out = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, Enum) and column.type.name:
                out[column.type.name] = set(column.type.enums)
    return out


def _db_enum_labels(sync_conn):
    from sqlalchemy import text

    rows = sync_conn.execute(
        text("SELECT t.typname, e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid")
    )
    out = {}
    for typname, label in rows:
        out.setdefault(typname, set()).add(label)
    return out


@pytest.mark.asyncio
async def test_upgrade_head_matches_models():
    from alembic import command
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        await _wipe(engine)

        cfg = _make_config(TEST_DATABASE_URL)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
            diffs = await conn.run_sync(_diff_against_models)
            db_enums = await conn.run_sync(_db_enum_labels)

        model_enums = _model_enum_labels()
        assert "user_role" in model_enums, "enum collection found nothing; the check proves nothing"
        for name, labels in model_enums.items():
            assert db_enums.get(name) == labels, (
                f"native enum {name!r} drifted from the model after `alembic upgrade head`: "
                f"db={sorted(db_enums.get(name, []))} model={sorted(labels)}"
            )

        for expected in ["users", "plugs", "charging_sessions", "ledger_transactions",
                         "telemetry_readings", "gateways", "tenants",
                         "charger_groups", "group_memberships"]:
            assert expected in tables, f"baseline did not create {expected}"
        assert "alembic_version" in tables

        structural = _structural(diffs)
        assert not structural, (
            "baseline schema has drifted from the models — write a new revision. "
            f"Diffs: {structural}"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pre_alembic_database_is_stamped_not_migrated(monkeypatch):
    """A create_all-built DB (no alembic_version) must be stamped at baseline
    by init_db(), and end at head without the baseline DDL re-running (it
    would explode on the existing tables if it did)."""
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database import db as db_module
    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        await _wipe(engine)
        # Simulate the legacy path: schema exists, no alembic_version.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(db_module, "engine", engine)
        monkeypatch.setattr(db_module, "DATABASE_URL", TEST_DATABASE_URL)
        await db_module.init_db()

        async with engine.connect() as conn:
            rev = await conn.run_sync(
                lambda c: MigrationContext.configure(c).get_current_revision()
            )
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())

        assert rev is not None, "init_db did not stamp the pre-Alembic database"
        assert "users" in tables  # nothing got dropped/re-created
    finally:
        await engine.dispose()
