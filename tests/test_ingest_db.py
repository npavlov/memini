"""The ingest library against a real Postgres: upsert by id and content hash, window claims and
the checkpoint, tombstones and snapshots, rollback, concurrency. Acceptance of memini-5p1: the same
batch sent twice changes nothing; an incomplete window keeps the checkpoint; a failure rolls back
records and checkpoint together. Every value is synthetic (tests/ingest_helpers.py)."""

import hashlib
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, LiteralString

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from ingest_helpers import DAY, HOUR, T0, document, meeting, thread, tombstone
from memini.auth import WriteAccess
from memini.chunking import chunk_markdown
from memini.db import connect
from memini.embeddings import FakeEmbedder
from memini.ids import record_id
from memini.index import index_record
from memini.ingest import (
    Batch,
    IngestResult,
    InvalidBatchError,
    NotIngestibleError,
    RecordIn,
    StoredRecord,
    WindowClaim,
    WindowGapError,
    canonical_json,
    ingest,
)
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import NotFoundError, create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

STREAM = "chat/general"


@dataclass(frozen=True)
class Env:
    db: Conn
    runs: Conn
    url: str
    owner: uuid.UUID
    access: WriteAccess
    acme: uuid.UUID
    other: uuid.UUID
    chat: uuid.UUID
    """A push source in acme."""


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        connect(migrated_database_url, autocommit=True) as runs,
    ):
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        other = await create_workspace(db, owner_id=owner, name="other")
        access = WriteAccess.for_owner(owner)
        chat = await create_push_source(db, access, "acme", NewPushSource(name="chat"))
        await create_push_source(db, access, "other", NewPushSource(name="chat"))
        yield Env(db, runs, migrated_database_url, owner, access, acme.id, other.id, chat.id)


async def send(env: Env, batch: Batch, *, source: str = "chat", **kwargs: Any) -> IngestResult:
    return await ingest(env.db, env.access, "acme", source, batch, runs=env.runs, **kwargs)


def window(start: datetime, end: datetime, *, complete: bool = True, **kwargs: Any) -> WindowClaim:
    return WindowClaim(start=start, end=end, complete=complete, **kwargs)


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    found = await rows(db, query, *params)
    assert len(found) == 1
    return found[0][0]


async def versions(db: Conn) -> dict[str, str]:
    """xmin of every record and stream row: it changes exactly when a row is written."""
    found = await rows(
        db,
        "SELECT 'record:' || id, xmin::text FROM record"
        " UNION ALL SELECT 'stream:' || id, xmin::text FROM stream",
    )
    return {key: value for key, value in found}


async def checkpoint(env: Env, key: str = STREAM) -> tuple[datetime | None, Any]:
    found = await rows(
        env.db,
        "SELECT checked_until, cursor FROM stream WHERE source_id = %s AND key = %s",
        env.chat,
        key,
    )
    assert len(found) == 1
    return found[0][0], found[0][1]


def rid(env: Env, external_id: str, kind: str = "thread") -> str:
    return record_id(env.chat, kind, external_id)  # pyright: ignore[reportArgumentType]


async def live(env: Env) -> set[str]:
    found = await rows(
        env.db,
        "SELECT external_id FROM record WHERE source_id = %s AND deleted_at IS NULL",
        env.chat,
    )
    return {row[0] for row in found}


def counts(result: IngestResult) -> tuple[int, int, int, int, int, int]:
    return (
        result.inserted,
        result.updated,
        result.revived,
        result.deleted,
        result.unchanged,
        result.reconciled,
    )


# --- The same batch twice ------------------------------------------------------------------------


def first_day() -> Batch:
    return Batch(
        stream=STREAM,
        window=window(T0, T0 + DAY, cursor={"page": "p-2"}),
        records=[thread("general/0001"), thread("general/0002", T0 + 11 * HOUR), meeting("m-1")],
    )


async def test_a_batch_is_stored_and_the_same_batch_again_changes_nothing(env: Env) -> None:
    result = await send(env, first_day())

    assert counts(result) == (3, 0, 0, 0, 0, 0)
    assert result.checkpoint_moved
    assert result.checkpoint is not None
    assert result.checkpoint.checked_until == T0 + DAY
    assert result.checkpoint.cursor == {"page": "p-2"}
    assert result.window_complete is True
    stream_id = await scalar(env.db, "SELECT id FROM stream WHERE key = %s", STREAM)
    assert result.stream_id == stream_id
    assert await checkpoint(env) == (T0 + DAY, {"page": "p-2"})

    stored = await rows(
        env.db,
        "SELECT id, kind, external_id, external_url, occurred_at, payload, content_hash,"
        " stream_id, workspace_id, owner_id, normalized_text, deleted_at"
        " FROM record ORDER BY external_id",
    )
    batch = first_day()
    expected = sorted(batch.records, key=lambda item: item.external_id)
    assert len(stored) == 3
    for row, item in zip(stored, expected, strict=True):
        assert item.payload is not None
        payload = canonical_json(item.payload)
        assert row == (
            rid(env, item.external_id, item.kind),
            item.kind,
            item.external_id,
            item.external_url,
            item.occurred_at,
            payload,
            hashlib.sha256(payload).hexdigest(),
            stream_id,
            env.acme,
            env.owner,
            None,
            None,
        )
    # The stored payload is canonical JSON: sorted keys, no whitespace, UTF-8 as it is.
    assert stored[2][5].startswith(b'{"action_items":[{"owner":"Bo Sample"')

    before = await versions(env.db)
    again = await send(env, first_day())
    assert counts(again) == (0, 0, 0, 0, 3, 0)
    assert not again.checkpoint_moved
    assert again.checkpoint is not None
    assert again.checkpoint.checked_until == T0 + DAY
    assert await versions(env.db) == before

    # The same objects with their keys in another order are the same content.
    reordered = [
        replace(item, payload=dict(reversed(list((item.payload or {}).items()))))
        for item in first_day().records
    ]
    third = await send(env, replace(first_day(), records=reordered))
    assert counts(third) == (0, 0, 0, 0, 3, 0)
    assert await versions(env.db) == before

    runs = await rows(
        env.db,
        "SELECT kind, status, source_id, stream_id, report->'counts', error FROM run"
        " ORDER BY started_at, id",
    )
    assert [run[:4] for run in runs] == [("ingest", "succeeded", env.chat, stream_id)] * 3
    assert runs[0][4]["records_inserted"] == 3
    assert runs[1][4]["records_unchanged"] == 3
    assert runs[1][4]["records_inserted"] == 0


async def test_a_record_that_changed_is_written_and_the_hook_gets_it(env: Env) -> None:
    seen: list[list[StoredRecord]] = []

    async def indexer(conn: Conn, records: Sequence[StoredRecord]) -> None:
        assert conn is env.db
        seen.append(list(records))

    await send(env, first_day(), indexer=indexer)
    assert {record.external_id for record in seen[0]} == {"general/0001", "general/0002", "m-1"}
    assert [record.id for record in seen[0]] == sorted(record.id for record in seen[0])
    assert all(record.content_changed for record in seen[0])
    await env.db.execute("UPDATE record SET normalized_text = 'derived'")
    before = await versions(env.db)

    edited = Batch(
        stream=STREAM,
        records=[
            thread("general/0001", text="Friday it is."),  # another payload
            # The same payload at another time.
            replace(thread("general/0002", T0 + 11 * HOUR), occurred_at=T0 + 12 * HOUR),
            meeting("m-1"),  # the same
        ],
    )
    result = await send(env, edited, indexer=indexer)

    assert counts(result) == (0, 2, 0, 0, 1, 0)
    assert {(r.external_id, r.content_changed) for r in seen[1]} == {
        ("general/0001", True),
        ("general/0002", False),
    }
    after = await versions(env.db)
    assert (
        after[f"record:{rid(env, 'm-1', 'meeting')}"]
        == before[f"record:{rid(env, 'm-1', 'meeting')}"]
    )
    assert (
        after[f"record:{rid(env, 'general/0001')}"] != before[f"record:{rid(env, 'general/0001')}"]
    )
    derived = dict(await rows(env.db, "SELECT external_id, normalized_text FROM record"))
    assert derived == {"general/0001": None, "general/0002": "derived", "m-1": "derived"}
    assert (
        await scalar(
            env.db, "SELECT occurred_at FROM record WHERE id = %s", rid(env, "general/0002")
        )
        == T0 + 12 * HOUR
    )


# --- Windows and the checkpoint ------------------------------------------------------------------


async def test_an_incomplete_window_stores_its_records_and_keeps_the_checkpoint(env: Env) -> None:
    await send(env, Batch(stream=STREAM, window=window(T0, T0 + DAY), records=[]))
    assert await checkpoint(env) == (T0 + DAY, None)

    result = await send(
        env,
        Batch(
            stream=STREAM,
            window=window(T0 + DAY, T0 + 2 * DAY, complete=False, cursor={"page": "p-9"}),
            records=[thread("general/0003", T0 + DAY + HOUR)],
        ),
    )
    assert counts(result) == (1, 0, 0, 0, 0, 0)
    assert result.window_complete is False
    assert not result.checkpoint_moved
    assert await checkpoint(env) == (T0 + DAY, None)
    assert await live(env) == {"general/0003"}

    # An incomplete window may start after the checkpoint: it never moves it.
    gap = await send(
        env,
        Batch(
            stream=STREAM,
            window=window(T0 + 3 * DAY, T0 + 4 * DAY, complete=False),
            records=[thread("general/0004", T0 + 3 * DAY)],
        ),
    )
    assert gap.inserted == 1
    assert await checkpoint(env) == (T0 + DAY, None)

    statuses = await rows(env.db, "SELECT status, error FROM run ORDER BY started_at, id")
    assert statuses[0] == ("succeeded", None)
    assert statuses[1][0] == statuses[2][0] == "partial"
    assert "incomplete" in statuses[1][1]


async def test_the_checkpoint_moves_forward_only_and_never_over_a_gap(env: Env) -> None:
    first = await send(env, Batch(stream=STREAM, window=window(T0 + 5 * DAY, T0 + 6 * DAY)))
    assert first.checkpoint_moved  # the first window may start anywhere
    assert await checkpoint(env) == (T0 + 6 * DAY, None)

    # An overlap behind the checkpoint, the margin for late replies.
    overlap = await send(
        env,
        Batch(
            stream=STREAM,
            window=window(T0 + 5 * DAY + 12 * HOUR, T0 + 7 * DAY, cursor={"after": "x-7"}),
        ),
    )
    assert overlap.checkpoint_moved
    assert await checkpoint(env) == (T0 + 7 * DAY, {"after": "x-7"})

    # Older history, loaded later: stored, and the checkpoint stays (with its cursor).
    backfill = await send(
        env,
        Batch(
            stream=STREAM,
            window=window(T0, T0 + DAY, cursor={"after": "old"}),
            records=[thread("general/0001")],
        ),
    )
    assert (backfill.inserted, backfill.checkpoint_moved) == (1, False)
    assert await checkpoint(env) == (T0 + 7 * DAY, {"after": "x-7"})

    # A complete window that starts after the checkpoint is refused, with nothing stored.
    before = await versions(env.db)
    with pytest.raises(WindowGapError) as gap:
        await send(
            env,
            Batch(
                stream=STREAM,
                window=window(T0 + 8 * DAY, T0 + 9 * DAY),
                records=[thread("general/0009", T0 + 8 * DAY)],
            ),
        )
    assert gap.value.checked_until == T0 + 7 * DAY
    assert await versions(env.db) == before
    assert await checkpoint(env) == (T0 + 7 * DAY, {"after": "x-7"})

    # A window that ends in the future is refused too.
    future = datetime.now().astimezone() + DAY
    with pytest.raises(InvalidBatchError, match=r"window\.to"):
        await send(
            env,
            Batch(
                stream=STREAM,
                window=window(T0 + 7 * DAY, future),
                records=[thread("general/0010", T0 + 8 * DAY)],
            ),
        )
    assert await versions(env.db) == before

    statuses = await rows(env.db, "SELECT status, error FROM run ORDER BY started_at, id")
    assert [status for status, _ in statuses][-2:] == ["failed", "failed"]
    assert "WindowGapError" in statuses[-2][1]


async def test_a_failure_rolls_back_records_and_checkpoint_together(env: Env) -> None:
    await send(env, first_day())
    before = await versions(env.db)
    stored_before = await rows(
        env.db, "SELECT id, deleted_at, content_hash FROM record ORDER BY id"
    )

    async def failing(conn: Conn, records: Sequence[StoredRecord]) -> None:
        assert records  # the rows are written in the transaction by now
        found = await rows(conn, "SELECT count(*) FROM record WHERE external_id = 'general/0005'")
        assert found == [(1,)]
        raise RuntimeError("the embedding model went away")

    with pytest.raises(RuntimeError, match="went away"):
        await send(
            env,
            Batch(
                stream=STREAM,
                window=window(T0 + DAY, T0 + 2 * DAY, snapshot=True),
                records=[
                    thread("general/0005", T0 + DAY + HOUR),
                    thread("general/0001", text="Edited after all."),
                    tombstone("m-1", "meeting"),
                ],
            ),
            indexer=failing,
        )
    assert await versions(env.db) == before
    assert await rows(env.db, "SELECT id, deleted_at, content_hash FROM record ORDER BY id") == (
        stored_before
    )
    assert await checkpoint(env) == (T0 + DAY, {"page": "p-2"})
    status, error = (await rows(env.db, "SELECT status, error FROM run ORDER BY started_at DESC"))[
        0
    ]
    assert status == "failed"
    assert "RuntimeError" in error


# --- Deletions -----------------------------------------------------------------------------------


async def test_a_tombstone_deletes_the_record_and_its_chunks_and_a_record_revives_it(
    env: Env,
) -> None:
    text = "# Greenhouse\n\nWater the plants.\n\n## Lamps\n\nSixteen hours at most.\n"
    await send(env, Batch(records=[document("handbook", text), thread("general/0001")]))
    handbook = rid(env, "handbook", "document")
    fake = FakeEmbedder()
    payload_text = text.encode()
    await index_record(
        env.db,
        fake,
        record_id=handbook,
        path=None,
        chunks=chunk_markdown(handbook, "handbook", payload_text),
    )
    chunks = await scalar(env.db, "SELECT count(*) FROM chunk WHERE record_id = %s", handbook)
    assert chunks > 0

    result = await send(
        env,
        Batch(records=[tombstone("handbook", "document"), tombstone("never-stored")]),
    )
    assert counts(result) == (0, 0, 0, 1, 1, 0)
    assert await scalar(env.db, "SELECT count(*) FROM chunk WHERE record_id = %s", handbook) == 0
    assert await scalar(env.db, "SELECT deleted_at IS NOT NULL FROM record WHERE id = %s", handbook)
    assert await scalar(env.db, "SELECT count(*) FROM record") == 2  # no row for never-stored
    report = await scalar(env.db, "SELECT report FROM run ORDER BY started_at DESC LIMIT 1")
    assert report["counts"]["records_deleted"] == 1
    assert report["counts"]["chunks_deleted"] == chunks

    before = await versions(env.db)
    assert counts(await send(env, Batch(records=[tombstone("handbook", "document")]))) == (
        0,
        0,
        0,
        0,
        1,
        0,
    )
    assert await versions(env.db) == before

    revived = await send(env, Batch(records=[document("handbook", text)]))
    assert counts(revived) == (0, 0, 1, 0, 0, 0)
    assert await scalar(env.db, "SELECT deleted_at FROM record WHERE id = %s", handbook) is None


async def test_a_snapshot_deletes_the_records_of_its_window_it_leaves_out(env: Env) -> None:
    await send(
        env,
        Batch(
            stream=STREAM,
            records=[
                thread("general/a1", T0 + HOUR),
                thread("general/a2", T0 + 2 * HOUR),
                thread("general/a3", T0 + DAY + HOUR),  # after the window
                meeting("m-a", T0 + 3 * HOUR),
            ],
        ),
    )
    await send(env, Batch(stream="chat/random", records=[thread("random/b1", T0 + HOUR)]))
    await send(env, Batch(records=[thread("loose/c1", T0 + HOUR)]))  # through no stream

    result = await send(
        env,
        Batch(
            stream=STREAM,
            window=window(T0, T0 + DAY, snapshot=True),
            records=[thread("general/a1", T0 + HOUR), thread("general/a4", T0 + 4 * HOUR)],
        ),
    )
    assert counts(result) == (1, 0, 0, 0, 1, 2)
    assert await live(env) == {"general/a1", "general/a3", "general/a4", "random/b1", "loose/c1"}
    assert await checkpoint(env) == (T0 + DAY, None)

    # An empty snapshot: nothing of the stream is left in its window.
    empty = await send(env, Batch(stream=STREAM, window=window(T0, T0 + DAY, snapshot=True)))
    assert empty.reconciled == 2
    assert await live(env) == {"general/a3", "random/b1", "loose/c1"}


async def test_a_record_keeps_its_stream_when_a_batch_names_none(env: Env) -> None:
    await send(env, Batch(stream=STREAM, records=[thread("general/0001")]))
    stream_id = await scalar(env.db, "SELECT id FROM stream WHERE key = %s", STREAM)
    before = await versions(env.db)
    assert (await send(env, Batch(records=[thread("general/0001")]))).unchanged == 1
    assert await versions(env.db) == before
    assert await scalar(env.db, "SELECT stream_id FROM record") == stream_id

    moved = await send(env, Batch(stream="chat/random", records=[thread("general/0001")]))
    assert moved.updated == 1
    assert await scalar(
        env.db, "SELECT s.key FROM record r JOIN stream s ON s.id = r.stream_id"
    ) == ("chat/random")


# --- Concurrency ---------------------------------------------------------------------------------


async def test_a_batch_waits_for_the_one_before_it_and_sees_its_checkpoint(env: Env) -> None:
    await send(env, Batch(stream=STREAM, window=window(T0, T0 + DAY)))
    async with (
        connect(env.url, autocommit=True) as holder,
        connect(env.url, autocommit=True) as conn,
    ):
        # Another batch holds the stream: it has stored its window and not committed yet.
        await holder.execute("BEGIN")
        await holder.execute("SELECT 1 FROM stream WHERE key = %s FOR NO KEY UPDATE", (STREAM,))
        await holder.execute(
            "UPDATE stream SET checked_until = %s WHERE key = %s", (T0 + 3 * DAY, STREAM)
        )
        results: list[IngestResult] = []
        errors: list[Exception] = []

        async def second() -> None:
            try:
                results.append(
                    await ingest(
                        conn,
                        env.access,
                        "acme",
                        "chat",
                        Batch(
                            stream=STREAM,
                            window=window(T0 + DAY, T0 + 2 * DAY),
                            records=[thread("general/0002", T0 + DAY + HOUR)],
                        ),
                    )
                )
            except Exception as error:
                errors.append(error)

        async with anyio.create_task_group() as group:
            group.start_soon(second)
            await anyio.sleep(0.3)
            assert not results  # waiting for the stream
            assert not errors
            await holder.execute("COMMIT")
        assert not errors
        # It sees the checkpoint of the batch before it: it neither moves it back nor skips.
        assert results[0].inserted == 1
        assert not results[0].checkpoint_moved
        assert await checkpoint(env) == (T0 + 3 * DAY, None)


async def claim(env: Env, batch: Batch, results: list[IngestResult]) -> None:
    """Send ``batch`` on a connection of its own."""
    async with connect(env.url, autocommit=True) as conn:
        results.append(await ingest(conn, env.access, "acme", "chat", batch))


async def test_concurrent_batches_leave_the_latest_checkpoint(env: Env) -> None:
    await send(env, Batch(stream=STREAM, window=window(T0, T0 + DAY)))
    for round_ in range(3):
        base = T0 + DAY + round_ * 3 * DAY  # the checkpoint the round before left
        ends = [base + DAY, base + 2 * DAY, base + 3 * DAY]
        results: list[IngestResult] = []
        async with anyio.create_task_group() as group:
            for index, end in enumerate(ends):
                batch = Batch(
                    stream=STREAM,
                    window=window(base - HOUR, end),
                    records=[thread(f"general/r{round_}-{index}", base + HOUR)],
                )
                group.start_soon(claim, env, batch, results)
        assert await checkpoint(env) == (ends[-1], None)
        assert any(result.checkpoint_moved for result in results)
    assert len(await live(env)) == 9


# --- Refusals ------------------------------------------------------------------------------------


async def test_a_git_source_and_one_beyond_the_access_take_no_batch(env: Env) -> None:
    await env.db.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, 'repo', 'git')",
        (env.acme, env.owner),
    )
    batch = Batch(stream=STREAM, records=[thread("general/0001")])
    with pytest.raises(NotIngestibleError):
        await send(env, batch, source="repo")
    with pytest.raises(NotFoundError) as missing:
        await send(env, batch, source="nowhere")
    assert missing.value.what == "source"

    limited = WriteAccess(owner_id=env.owner, workspace_ids=frozenset({env.other}))
    with pytest.raises(NotFoundError) as beyond:
        await ingest(env.db, limited, "acme", "chat", batch)
    assert beyond.value.what == "workspace"
    assert await scalar(env.db, "SELECT count(*) FROM record") == 0
    assert await scalar(env.db, "SELECT count(*) FROM stream") == 0
    assert await scalar(env.db, "SELECT count(*) FROM run") == 0

    # Its own workspace it may write.
    result = await ingest(env.db, limited, "other", "chat", batch)
    assert result.inserted == 1

    # An mcp source takes batches: its recipe executors end here.
    await env.db.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, 'mail', 'mcp')",
        (env.acme, env.owner),
    )
    assert (await send(env, batch, source="mail")).inserted == 1


async def test_a_batch_refused_by_its_checks_touches_nothing(env: Env) -> None:
    bad = [
        Batch(records=[RecordIn(kind="thread", external_id="t", occurred_at=T0, payload={})]),
        Batch(stream=STREAM, records=[RecordIn(kind="code_file", external_id="c", deleted=True)]),
        Batch(stream=STREAM, records=[thread("same"), thread("same")]),
    ]
    for batch in bad:
        with pytest.raises(InvalidBatchError):
            await send(env, batch)
    assert await scalar(env.db, "SELECT count(*) FROM stream") == 0
    assert await scalar(env.db, "SELECT count(*) FROM run") == 0


async def test_ingest_wants_an_idle_connection(env: Env) -> None:
    async with connect(env.url) as conn:
        await conn.execute("SELECT 1")  # opens a transaction
        with pytest.raises(ValueError, match="idle"):
            await ingest(conn, env.access, "acme", "chat", Batch(records=[]))
