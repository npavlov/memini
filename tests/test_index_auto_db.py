"""The rebuild after an upgrade (memini.index.auto, memini.index.progress) against a real Postgres.

Acceptance of memini-qpk.27: a service with ``index_auto_rebuild`` enqueues one ``reindex`` job
for each out-of-date workspace; while it waits or runs, search is refused with how far it is; the
job rebuilds the index, reusing vectors, and search passes again. Every value is synthetic;
vectors come from the FakeEmbedder, raw content from an in-memory store.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from index_helpers import FILE_V1, PATH, FakeRaw, Scope, add_record, chunks_of, make_scope, rows
from memini.db import Pool, connect, open_pool
from memini.embeddings import EmbeddingSpec, FakeEmbedder
from memini.index import (
    REINDEX_KIND,
    IndexMismatchError,
    IndexRebuildingError,
    check_index,
    current_fingerprint,
    index_record,
    read_rebuild,
    reindex,
)
from memini.index.auto import enqueue_stale_reindexes, reindex_handler, stale_workspaces
from memini.jobs import Job
from memini.runs import record_run

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

UPGRADED = EmbeddingSpec(provider="openai_compatible", model="fake-embedder-2", dimension=12)
"""What the service is configured for after the upgrade: another model, so another fingerprint."""


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5) as opened:
        yield opened


@pytest.fixture
async def scope(db: Conn) -> Scope:
    return await make_scope(db)


async def indexed_file(db: Conn, scope: Scope, raw: FakeRaw) -> str:
    """A code record of acme, indexed under the FakeEmbedder's default spec, and in ``raw``."""
    fake = FakeEmbedder()
    rid = await add_record(
        db,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=scope.repo,
        external_id=f"main:{PATH}",
        content=FILE_V1,
    )
    await index_record(db, fake, record_id=rid, path=PATH, chunks=chunks_of(rid, FILE_V1))
    raw.put(rid, FILE_V1)
    return rid


def job_of(scope: Scope, job_id: uuid.UUID) -> Job:
    return Job(
        id=job_id,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=None,
        stream_id=None,
        kind=REINDEX_KIND,
        args={},
        attempt=1,
        max_attempts=5,
        worker="test/0",
    )


async def test_an_upgrade_enqueues_one_rebuild_per_out_of_date_workspace(
    db: Conn, pool: Pool, scope: Scope
) -> None:
    await indexed_file(db, scope, FakeRaw())
    upgraded = FakeEmbedder(UPGRADED)

    assert [
        name for _id, _owner, name in await stale_workspaces(db, current_fingerprint(UPGRADED))
    ] == ["acme"]
    # Under the fingerprint it was built with, nothing is out of date.
    assert await stale_workspaces(db, current_fingerprint(FakeEmbedder().spec)) == []

    assert await enqueue_stale_reindexes(pool, upgraded) == ["acme"]
    # A second start keeps the job that waits: one rebuild per workspace.
    assert await enqueue_stale_reindexes(pool, upgraded) == ["acme"]
    jobs = await rows(db, "SELECT workspace_id, kind, status, source_id FROM job")
    assert jobs == [(scope.acme, REINDEX_KIND, "queued", None)]


async def test_search_says_the_rebuild_waits_then_how_far_it_is(
    db: Conn, pool: Pool, scope: Scope
) -> None:
    await indexed_file(db, scope, FakeRaw())
    expected = current_fingerprint(UPGRADED)

    # Out of date and no rebuild: the refusal names the command.
    with pytest.raises(IndexMismatchError) as plain:
        await check_index(db, scope.acme, expected)
    assert not isinstance(plain.value, IndexRebuildingError)
    assert await read_rebuild(db, scope.acme) is None

    await enqueue_stale_reindexes(pool, FakeEmbedder(UPGRADED))
    with pytest.raises(
        IndexRebuildingError, match=r"being rebuilt .*: queued to start\."
    ) as queued:
        await check_index(db, scope.acme, expected)
    assert queued.value.rebuild.state == "queued"
    assert queued.value.command == "memini reindex --workspace acme"

    # A running rebuild whose report says how far it is.
    await db.execute(
        "INSERT INTO run (workspace_id, owner_id, kind, report) VALUES (%s, %s, %s, %s)",
        (
            scope.acme,
            scope.owner,
            REINDEX_KIND,
            Jsonb({"details": {"progress": {"records_total": 1472, "records_done": 312}}}),
        ),
    )
    with pytest.raises(IndexRebuildingError, match=r"312 of 1,472 records \(21%\)") as running:
        await check_index(db, scope.acme, expected)
    progress = running.value.rebuild
    assert (progress.state, progress.records_done, progress.records_total, progress.percent) == (
        "running",
        312,
        1472,
        21,
    )
    assert progress.started_at is not None


async def test_the_rebuild_reports_its_progress_in_the_run(
    db: Conn, migrated_database_url: str, scope: Scope
) -> None:
    raw = FakeRaw()
    await indexed_file(db, scope, raw)

    async with connect(migrated_database_url, autocommit=True) as reports:
        async with record_run(
            reports, workspace_id=scope.acme, owner_id=scope.owner, kind=REINDEX_KIND
        ) as run:
            await reindex(db, FakeEmbedder(UPGRADED), workspace_id=scope.acme, raw=raw)
        report = run.report()

    progress = report.get("details", {})["progress"]
    assert isinstance(progress, dict)
    assert (progress["records_total"], progress["records_done"]) == (1, 1)
    assert isinstance(progress["started_at"], str)


async def test_the_job_rebuilds_the_index_and_search_passes_again(
    db: Conn, pool: Pool, scope: Scope
) -> None:
    raw = FakeRaw()
    await indexed_file(db, scope, raw)
    upgraded = FakeEmbedder(UPGRADED)
    await enqueue_stale_reindexes(pool, upgraded)
    found = await rows(db, "SELECT id FROM job WHERE kind = %s", (REINDEX_KIND,))

    await reindex_handler(pool, upgraded, code=raw)(job_of(scope, found[0][0]))

    state = await check_index(db, scope.acme, current_fingerprint(UPGRADED))
    assert state is not None
    assert await stale_workspaces(db, current_fingerprint(UPGRADED)) == []
    assert await rows(db, "SELECT DISTINCT vector_dims(embedding) FROM chunk") == [(12,)]
    # The pool's connection is given back as it was lent.
    async with pool.connection() as conn:
        assert conn.autocommit is False
