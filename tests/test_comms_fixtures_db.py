"""The synthetic comms fixtures (``eval/comms``) through the ingest library into a real Postgres.
Acceptance of memini-dh2, and the first part of the M2 check (docs/plan.md): the chat, mail and
meeting fixtures ingest, batch by batch, into the workspace harbor; each batch sent again changes
nothing (no row written: the same xmin), and neither does a second sync of the sources as they
stand. The cases of the timeline (late reply, edit, tombstone, incomplete window, snapshot) do
what labels/timeline.json says. Vectors come from the FakeEmbedder."""

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, LiteralString

import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from comms_fixtures import (
    WORKSPACE,
    RecordRef,
    checked_ranges,
    create_sources,
    deliveries,
    final_records,
    load,
    record_id_of,
    second_sync,
    send,
    timeline_labels,
)
from memini.auth import WriteAccess
from memini.db import connect
from memini.embeddings import FakeEmbedder
from memini.ingest import IngestResult, PayloadIndexer, canonical_json, ingest
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True)
class Env:
    db: Conn
    runs: Conn
    access: WriteAccess
    sources: dict[str, uuid.UUID]
    embedder: FakeEmbedder
    indexer: PayloadIndexer


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        connect(migrated_database_url, autocommit=True) as runs,
    ):
        owner = await ensure_principal(db, "owner")
        access = WriteAccess.for_owner(owner)
        sources = await create_sources(db, access)
        embedder = FakeEmbedder()
        yield Env(db, runs, access, sources, embedder, PayloadIndexer(embedder))


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def versions(db: Conn) -> dict[str, str]:
    """xmin of every record, stream and chunk row: it changes exactly when a row is written."""
    found = await rows(
        db,
        "SELECT 'record:' || id, xmin::text FROM record"
        " UNION ALL SELECT 'stream:' || id, xmin::text FROM stream"
        " UNION ALL SELECT 'chunk:' || id, xmin::text FROM chunk",
    )
    return {key: value for key, value in found}


def writes(result: IngestResult) -> tuple[int, int, int, int, int]:
    """inserted, updated, revived, deleted, reconciled: everything but unchanged."""
    return (result.inserted, result.updated, result.revived, result.deleted, result.reconciled)


def counts(result: IngestResult) -> dict[str, int]:
    return {
        name: value
        for name, value in (
            ("inserted", result.inserted),
            ("updated", result.updated),
            ("revived", result.revived),
            ("deleted", result.deleted),
            ("unchanged", result.unchanged),
            ("reconciled", result.reconciled),
        )
        if value
    }


async def test_the_timeline_ingests_batch_by_batch_and_each_batch_again_changes_nothing(
    env: Env,
) -> None:
    results: dict[str, IngestResult] = {}
    for each in deliveries():
        results[each.name] = await send(
            env.db, env.access, each, indexer=env.indexer, runs=env.runs
        )
        before = await versions(env.db)
        env.embedder.reset()

        again = await send(env.db, env.access, each, indexer=env.indexer, runs=env.runs)

        assert writes(again) == (0, 0, 0, 0, 0), each.name
        assert again.unchanged == len(each.items), each.name
        assert not again.checkpoint_moved, each.name
        assert again.checkpoint == results[each.name].checkpoint, each.name
        assert env.embedder.texts == [], each.name
        assert await versions(env.db) == before, each.name

    cases = timeline_labels()["scenarios"]
    # A late reply writes the thread again.
    assert counts(results[cases["late_reply"]["again"]]) == {"updated": 1}
    # The edit, a new thread and the tombstone of another arrive in one batch.
    assert cases["edited_message"]["again"] == cases["deleted_thread"]["tombstone"]
    assert counts(results[cases["deleted_thread"]["tombstone"]]) == {
        "inserted": 1,
        "updated": 1,
        "deleted": 1,
    }
    # The incomplete window stores its records and leaves the checkpoint; its retry moves it.
    partial = results[cases["incomplete_window"]["delivery"]]
    assert partial.window_complete is False
    assert not partial.checkpoint_moved
    assert partial.checkpoint is not None
    assert partial.checkpoint.checked_until == datetime.fromisoformat("2026-09-21T00:00:00Z")
    assert counts(partial) == {"inserted": 1, "updated": 1}
    (status,) = (await rows(env.db, "SELECT status FROM run WHERE id = %s", partial.run_id))[0]
    assert status == "partial"
    retry = results[cases["incomplete_window"]["retried_by"]]
    assert retry.checkpoint_moved
    assert counts(retry) == {"inserted": 2, "unchanged": 2}
    # The snapshot leaves out the notes of the deleted call.
    assert counts(results[cases["snapshot_deletion"]["snapshot"]]) == {
        "inserted": 1,
        "reconciled": 1,
    }

    # What the store holds is the last version of every record, the deleted ones deleted.
    stored = {
        row[0]: row[1:]
        for row in await rows(
            env.db, "SELECT id, content_hash, occurred_at, deleted_at IS NOT NULL FROM record"
        )
    }
    expected = {
        record_id_of(ref, env.sources): (
            hashlib.sha256(canonical_json(record.payload)).hexdigest(),
            record.occurred_at,
            record.deleted,
        )
        for ref, record in final_records().items()
    }
    assert stored == expected

    # Live records have chunks, deleted ones none.
    chunked = {row[0] for row in await rows(env.db, "SELECT DISTINCT record_id FROM chunk")}
    assert chunked == {
        record_id_of(ref, env.sources)
        for ref, record in final_records().items()
        if not record.deleted
    }

    streams = await rows(
        env.db,
        "SELECT s.name, t.key, t.checked_from, t.checked_until FROM stream t"
        " JOIN source s ON s.id = t.source_id",
    )
    assert {(row[0], row[1]): (row[2], row[3]) for row in streams} == checked_ranges()


async def test_a_second_sync_of_the_sources_as_they_stand_changes_nothing(env: Env) -> None:
    await load(env.db, env.access, indexer=env.indexer)
    before = await versions(env.db)
    env.embedder.reset()

    for source, batch in second_sync():
        result = await ingest(env.db, env.access, WORKSPACE, source, batch, indexer=env.indexer)
        assert writes(result) == (0, 0, 0, 0, 0), (source, batch.stream)
        assert result.unchanged == len(batch.records)
        assert not result.checkpoint_moved

    assert env.embedder.texts == []
    assert await versions(env.db) == before


async def test_the_labels_name_records_the_store_holds(env: Env) -> None:
    """The references of the labels resolve to stored rows by the ids the decision tools use."""
    await load(env.db, env.access)
    stored = {
        row[0]: row[1]
        for row in await rows(env.db, "SELECT id, deleted_at IS NOT NULL FROM record")
    }
    for ref, record in final_records().items():
        assert stored[record_id_of(ref, env.sources)] is record.deleted
    gone = [RecordRef.of(entry) for entry in timeline_labels()["final"]["deleted"]]
    assert all(stored[record_id_of(ref, env.sources)] for ref in gone)
