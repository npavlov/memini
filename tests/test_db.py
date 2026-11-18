"""Migrations and the pool against a real Postgres: run with `make test-integration`."""

import asyncio

import anyio
import pytest
from pgvector import Vector
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.db import (
    Migration,
    MigrationError,
    connect,
    load_migrations,
    migrate,
    open_pool,
    pending_migrations,
)
from memini.db.migrate import MIGRATION_LOCK_KEY

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def _applied(url: str) -> list[tuple[int, str, str]]:
    async with connect(url) as conn:
        cur = await conn.execute(
            "SELECT version, checksum, applied_at::text FROM schema_migration ORDER BY version"
        )
        return [(int(v), str(c), str(a)) for v, c, a in await cur.fetchall()]


async def _lock_waiters(conn: AsyncConnection[TupleRow]) -> int:
    """Sessions of this database waiting for an advisory lock."""
    cur = await conn.execute(
        "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database"
        " WHERE l.locktype = 'advisory' AND NOT l.granted AND d.datname = current_database()"
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_migrations_apply_to_an_empty_database(empty_database_url: str) -> None:
    every = load_migrations()
    async with connect(empty_database_url) as conn:
        assert await pending_migrations(conn) == every

    assert await migrate(empty_database_url) == every

    async with connect(empty_database_url) as conn:
        cur = await conn.execute("SELECT extname FROM pg_extension ORDER BY extname")
        assert {name for (name,) in await cur.fetchall()} >= {"vector", "pg_trgm"}
        assert await pending_migrations(conn) == []
    applied = await _applied(empty_database_url)
    assert [(v, c) for v, c, _ in applied] == [(m.version, m.checksum) for m in every]

    assert await migrate(empty_database_url) == []
    assert await _applied(empty_database_url) == applied


async def test_concurrent_runs_apply_each_migration_once(empty_database_url: str) -> None:
    async with connect(empty_database_url, autocommit=True) as holder:
        await holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        runs = asyncio.gather(migrate(empty_database_url), migrate(empty_database_url))
        with anyio.fail_after(10):
            while await _lock_waiters(holder) < 2:
                await anyio.sleep(0.02)
        await holder.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
        first, second = await runs

    assert sorted(m.version for m in first + second) == [m.version for m in load_migrations()]
    assert len(await _applied(empty_database_url)) == len(load_migrations())


async def test_a_failed_migration_leaves_nothing_behind(empty_database_url: str) -> None:
    broken = Migration(2, "broken", b"CREATE TABLE half_done (id integer);\nSELECT 1 / 0;\n")

    with pytest.raises(MigrationError, match=r"0002_broken\.sql failed"):
        await migrate(empty_database_url, migrations=[*load_migrations(), broken])

    async with connect(empty_database_url) as conn:
        cur = await conn.execute("SELECT to_regclass('half_done') IS NULL")
        assert await cur.fetchone() == (True,)
    assert [v for v, _, _ in await _applied(empty_database_url)] == [1]


async def test_an_edited_migration_is_refused(migrated_database_url: str) -> None:
    first = load_migrations()[0]
    edited = Migration(first.version, first.name, first.sql + b"-- edited\n")

    with pytest.raises(MigrationError, match="changed on disk"):
        await migrate(migrated_database_url, migrations=[edited])


async def test_a_database_from_a_newer_build_is_refused(migrated_database_url: str) -> None:
    newer = [*load_migrations(), Migration(2, "from_a_newer_build", b"SELECT 1;\n")]
    await migrate(migrated_database_url, migrations=newer)

    async with connect(migrated_database_url) as conn:
        with pytest.raises(MigrationError, match="does not know"):
            await pending_migrations(conn)


async def test_the_pool_reads_and_writes_vectors(migrated_database_url: str) -> None:
    async with open_pool(migrated_database_url, max_size=2) as pool, pool.connection() as conn:
        cur = await conn.execute(
            "SELECT %s <-> %s, %s", (Vector([0.0, 0.0]), Vector([3.0, 4.0]), Vector([0.5, 1.5]))
        )
        row = await cur.fetchone()

    assert row is not None
    distance, echoed = row
    assert distance == 5.0
    assert isinstance(echoed, Vector)
    assert echoed.to_list() == [0.5, 1.5]
