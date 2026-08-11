"""Least-privilege runtime role — LIVE test against a real Postgres (CI's
postgres:15 service via TEST_DATABASE_URL). Skipped locally (this repo runs no
database by policy).

This is the guardrail the 2026-08-04 activation incident lacked. The unit tests
(test_db_privilege_separation.py) only cover URL derivation + identifier
validation; the actual provisioning SQL — `format('ALTER ROLE %I ... %L',
cast(:r AS text), cast(:p AS text))` — broke in prod (asyncpg couldn't infer the
param type) with NO test exercising it, because every other DB-gated test
connects as the owner/superuser. This one:
  1. runs `_provision_runtime_role` against a real Postgres (a regression of the
     cast bug fails HERE, in CI, not at prod boot), and
  2. connects AS the provisioned role and proves it can DML but CANNOT drop.
"""
import os

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

_ROLE = "amphive_app_lpr_test"
_PW = "lpr-test-runtime-pw-4471"
# Scratch table (owner-owned, serial PK → an owned sequence) used to prove the
# least-priv role can run real DML + nextval, not just carry the privilege bits.
_PROBE_TABLE = "_lpr_probe"


async def _drop_role(conn, role):
    from sqlalchemy import text

    exists = (
        await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
    ).scalar()
    if exists:
        # DROP OWNED first: a role holding GRANTs can't be dropped until its
        # privileges (dependencies) are removed.
        await conn.execute(text(f'DROP OWNED BY "{role}"'))
        await conn.execute(text(f'DROP ROLE "{role}"'))


@pytest.mark.asyncio
async def test_provisioned_role_can_dml_but_not_drop():
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database import db as db_module
    from backend.database.models import Base

    owner_url = make_url(TEST_DATABASE_URL)
    owner_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    runtime_url = owner_url.set(username=_ROLE, password=_PW).render_as_string(
        hide_password=False
    )
    runtime_engine = create_async_engine(runtime_url, poolclass=NullPool)
    try:
        # Schema exists (owner-owned), clean slate, then provision the role —
        # this call is the exact SQL that broke at prod activation.
        async with owner_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with owner_engine.begin() as conn:
            await _drop_role(conn, _ROLE)
        async with owner_engine.begin() as conn:
            await db_module._provision_runtime_role(conn, _ROLE, _PW, owner_url.database)

        # As the least-privilege role: it must hold DML on public tables...
        async with runtime_engine.connect() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar() == _ROLE
            assert (await conn.execute(text("SELECT current_setting('is_superuser')"))).scalar() == "off"
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                granted = (
                    await conn.execute(text(f"SELECT has_table_privilege('tenants', '{priv}')"))
                ).scalar()
                assert granted is True, f"runtime role missing {priv} on tenants"
            # ...but NOT ownership-level rights (TRUNCATE requires owner/grant)...
            assert (
                await conn.execute(text("SELECT has_table_privilege('tenants', 'TRUNCATE')"))
            ).scalar() is False

        # ...and an actual DROP must be refused by the server.
        async with runtime_engine.connect() as conn:
            with pytest.raises(Exception) as ei:  # asyncpg InsufficientPrivilege
                await conn.execute(text("DROP TABLE tenants"))
        msg = str(ei.value).lower()
        assert "must be owner" in msg or "permission denied" in msg, msg
    finally:
        await runtime_engine.dispose()
        try:
            async with owner_engine.begin() as conn:
                await _drop_role(conn, _ROLE)
        finally:
            await owner_engine.dispose()


@pytest.mark.asyncio
async def test_provisioned_role_executes_real_dml_and_nextval():
    """Beyond privilege-bit introspection: connect AS the least-priv role and
    actually run INSERT/UPDATE/DELETE plus a literal sequence nextval, proving
    the grants work in practice — the layer has_table_privilege() can't reach.
    The 2026-08-04 incident passed a bare "SELECT 1" boot check while writes were
    in fact broken; a serial-PK INSERT exercises the sequence default (nextval)
    on top of the plain-DML path.
    """
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database import db as db_module

    owner_url = make_url(TEST_DATABASE_URL)
    owner_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    runtime_url = owner_url.set(username=_ROLE, password=_PW).render_as_string(
        hide_password=False
    )
    runtime_engine = create_async_engine(runtime_url, poolclass=NullPool)
    try:
        # Owner creates a scratch table with a serial PK (→ an owned sequence)
        # BEFORE provisioning, so the prod-shaped `GRANT ... ON ALL TABLES` /
        # `... ON ALL SEQUENCES` inside _provision_runtime_role covers it — the
        # same way prod grants over the freshly-migrated schema. (Tables created
        # AFTER provisioning are instead covered by the `ALTER DEFAULT PRIVILEGES`
        # clauses in that same function; this test exercises the pre-provision
        # path, which is what the GRANT-ON-ALL statements target.)
        async with owner_engine.begin() as conn:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{_PROBE_TABLE}"'))
            await conn.execute(
                text(f'CREATE TABLE "{_PROBE_TABLE}" (id bigserial primary key, v text)')
            )
        async with owner_engine.begin() as conn:
            await _drop_role(conn, _ROLE)
        async with owner_engine.begin() as conn:
            await db_module._provision_runtime_role(conn, _ROLE, _PW, owner_url.database)

        # Connect AS the least-priv role and run REAL DML + a literal nextval.
        async with runtime_engine.begin() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar() == _ROLE
            # INSERT exercises the sequence default (nextval) via the serial PK.
            new_id = (
                await conn.execute(
                    text(f'INSERT INTO "{_PROBE_TABLE}" (v) VALUES (:v) RETURNING id'),
                    {"v": "alpha"},
                )
            ).scalar()
            assert new_id is not None
            # UPDATE the row we just inserted.
            assert (
                await conn.execute(
                    text(f'UPDATE "{_PROBE_TABLE}" SET v = :v WHERE id = :id'),
                    {"v": "beta", "id": new_id},
                )
            ).rowcount == 1
            # Explicit nextval on the table's own sequence — proves USAGE on it,
            # independent of the INSERT default path above.
            nv = (
                await conn.execute(
                    text(
                        "SELECT nextval(pg_get_serial_sequence("
                        f"'{_PROBE_TABLE}', 'id'))"
                    )
                )
            ).scalar()
            assert nv is not None and nv > new_id
            # DELETE the row.
            assert (
                await conn.execute(
                    text(f'DELETE FROM "{_PROBE_TABLE}" WHERE id = :id'),
                    {"id": new_id},
                )
            ).rowcount == 1

        # DROP is still refused for the least-priv role (it owns nothing).
        async with runtime_engine.connect() as conn:
            with pytest.raises(Exception) as ei:  # asyncpg InsufficientPrivilege
                await conn.execute(text(f'DROP TABLE "{_PROBE_TABLE}"'))
        msg = str(ei.value).lower()
        assert "must be owner" in msg or "permission denied" in msg, msg
    finally:
        # Idempotent teardown as the owner: drop the role's grants+role, then the
        # scratch table (order matters — DROP OWNED BY revokes the role's grants
        # on the table before the role itself is dropped).
        await runtime_engine.dispose()
        try:
            async with owner_engine.begin() as conn:
                await _drop_role(conn, _ROLE)
                await conn.execute(text(f'DROP TABLE IF EXISTS "{_PROBE_TABLE}"'))
        finally:
            await owner_engine.dispose()
