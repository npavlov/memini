"""Sync one tracked ref: from its pinned commit to the commit the ref names now.

:func:`sync_stream` brings the records of one stream (a tracked ref) and their chunks to the commit
the ref names in the mirror, and then moves the stream's pin there. A file is a ``code_file`` record
chunked by :func:`~memini.chunking.chunk_file`, or a Markdown ``document`` record chunked by
:func:`~memini.chunking.chunk_markdown` (:func:`~memini.gitsync.rules.file_kind`).

What changed
    With a pin whose rules are the current ones, the files are the ones that differ from the
    pinned commit to the new one (:meth:`~memini.git.MirrorManager.diff_names`): added, changed,
    deleted, or changed in type. Otherwise the ref is listed in full
    (:meth:`~memini.git.MirrorManager.ls_tree`) and compared with every stored record of the ref:
    on the first sync, when the pinned commit is no longer in the mirror (a force push, then
    ``gc``), when the rules changed (globs, the size limit or the kinds of record), and after a
    sync that failed past its first transaction (the pin says ``partial``). Records of the ref that
    the listing does not select become tombstones, records of a kind a file no longer has among
    them: a Markdown file synced as code before Markdown files were documents.

    Each file is then compared with its stored record. A file whose record already holds its blob
    id is left alone: it is not read, chunked or embedded. The others are written; files that are
    deleted, or are no longer selected (:mod:`memini.gitsync.rules`), turn their live records into
    tombstones. A file whose record was forgotten by hand (:func:`memini.removal.forget_record`)
    is skipped (``files_skipped_forgotten``): forget holds the source's lock, so it never falls
    between a sync's plan and its writes.

Transactions
    Files are written in batches of at most :attr:`SyncLimits.batch_files` files and
    :attr:`SyncLimits.batch_bytes` bytes. A batch is read from the mirror (one ``cat-file
    --batch``) and chunked before its transaction starts; the transaction upserts each record and
    writes its chunks through the index writer (:func:`~memini.index.index_record`), which embeds
    only texts that have no vector yet. The last transaction also writes the tombstones, deletes
    their chunks (:func:`~memini.index.delete_record_chunks`) and moves the pin and
    ``checked_until`` (principle 7). So a small change, one batch, is one transaction: all of it
    or nothing. A larger one (a first sync) commits batch by batch, and its first transaction marks
    the pin ``partial``: if the sync then fails, the pin stays where it was, every record agrees
    with its chunks, and the next sync lists the ref in full and writes only what still differs,
    embedding nothing twice. A record and its chunks always change in one transaction, so no file
    is ever half written and no tombstone keeps chunks.

    A full listing (a first sync, or one after the rules changed) also commits a transaction once
    it has written for :attr:`SyncLimits.commit_seconds`, with the rest of its batch in the next
    one. On a computer without a GPU a batch of a hundred files is embedded for half an hour, and
    a sync stopped before its commit (an edit of the source, a restart) lost all of it; now it
    loses at most that many seconds, since the next sync lists the ref and skips what committed.
    A diff keeps one transaction per batch, so a small change stays all or nothing.

    The pin moves by compare-and-set on the cursor the sync started from; a stream changed by
    anyone else in between rolls the last transaction back.

Times
    A file written gets as ``occurred_at`` the committer time of the last commit that changed it
    on the ref, from one walk of the ref's first-parent history
    (:meth:`~memini.git.MirrorManager.change_times`): a diff walks the commits after the pin, a
    listing the newest :attr:`SyncLimits.history_commits` (1000). A merge counts with its changes
    against its first parent, so a change merged from another branch has the time it reached the
    ref. A file the walk did not reach changed earlier: it gets the time of the oldest commit
    walked when the walk was cut short (on a first sync of a long history), else that of the
    commit synced. Measured on a synthetic history, a diff walk costs one process of about 10 to
    16 ms; asking ``git log -1 -- <path>`` per file cost about 11 ms per file, 3 s for 300 files
    and over a minute for a first sync of 5000. Records of files not written keep their time.
    Reading the history is not worth failing the sync for: if it fails, the files take the time of
    the commit.

Untracked refs
    The last transaction also turns into tombstones the live records of the source whose ref is no
    longer tracked: removing a ref deletes its stream and leaves its records without one.

A ref that is gone
    A tracked ref that the fetched mirror does not hold (deleted at the remote, or never there) is
    not an error to retry: in one transaction the sync turns every live record of the ref into a
    tombstone, deletes their chunks, and replaces the pin with the marker
    :func:`~memini.gitsync.model.missing_cursor` writes, moving ``checked_until``. The run report
    counts ``refs_missing``. When the ref comes back, the stream has no pin, so the next sync lists
    it in full and brings the records back.
"""

import logging
import time
from collections import Counter
from collections.abc import AsyncGenerator, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import anyio
from psycopg import AsyncConnection, pq
from psycopg.abc import QueryNoTemplate
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from memini.chunking import FileChunks, chunk_file, chunk_markdown
from memini.embeddings import Embedder
from memini.git import GitError, Mirror, MirrorManager, RevisionNotFound
from memini.gitsync.model import (
    Pin,
    code_external_id,
    file_record_id,
    missing_cursor,
    read_pin,
)
from memini.gitsync.rules import FileKind, FileRules, SkipReason, file_kind, storable_path
from memini.index import IndexCounts, delete_record_chunks, index_record
from memini.removal import forgotten_record_ids
from memini.runs import current_run
from memini.sources import GitConfig

__all__ = [
    "DEFAULT_LIMITS",
    "GitSource",
    "GitStream",
    "StreamChangedError",
    "StreamSyncResult",
    "SyncLimits",
    "sync_stream",
]

log = logging.getLogger(__name__)

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True, slots=True)
class SyncLimits:
    """How many files, and how many bytes of them, one transaction writes at most; how many
    commits back a sync looks for the time each file last changed."""

    batch_files: int = 100
    batch_bytes: int = 8 * 1024**2
    history_commits: int = 1000
    commit_seconds: float = 30.0
    """In a full listing, how long one transaction writes before it commits with files of its
    batch left: a first sync on a computer without a GPU embeds a batch for half an hour, and a
    stop before its commit lost all of it."""

    def __post_init__(self) -> None:
        if self.batch_files < 1 or self.batch_bytes < 1:
            raise ValueError("a batch holds at least one file and one byte")
        if self.history_commits < 1:
            raise ValueError("a sync looks at least one commit back")
        if self.commit_seconds < 0:
            raise ValueError("a transaction writes for at least 0 seconds")


DEFAULT_LIMITS: Final = SyncLimits()


@dataclass(frozen=True, slots=True)
class GitSource:
    """A repository as a sync needs it."""

    id: UUID
    workspace_id: UUID
    owner_id: UUID
    config: GitConfig


@dataclass(frozen=True, slots=True)
class GitStream:
    """A stream of a repository: one tracked ref, and its cursor as stored."""

    id: UUID
    key: str
    """The full ref name, e.g. ``refs/heads/main``."""
    cursor: object
    """``stream.cursor`` as read: a :class:`~memini.gitsync.model.Pin` in JSON, or ``None``."""


@dataclass(frozen=True, slots=True)
class StreamSyncResult:
    """What :func:`sync_stream` did."""

    stream_id: UUID
    ref: str
    previous: str | None
    """The commit the stream was pinned at before; ``None`` on the first sync."""
    commit: str | None
    """The commit it is pinned at now; ``None`` when the ref is gone (``missing``)."""
    listed: bool
    """The ref was listed in full instead of diffed."""
    added: int
    """Records written that had no live record before."""
    changed: int
    """Live records written with new content."""
    deleted: int
    """Live records turned into tombstones: files deleted, no longer selected, or of untracked
    refs."""
    unchanged: int
    """Files whose record already held their content: neither read nor written."""
    skipped: Mapping[SkipReason, int]
    """Files not indexed, by reason, among those the sync looked at."""
    index: IndexCounts
    """What the index writer did."""
    missing: bool = False
    """The ref is not in the fetched mirror: its records are tombstones, the stream has no pin."""


class StreamChangedError(Exception):
    """The stream's cursor changed while it synced; the last transaction was rolled back."""


# --- Planning ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Entry:
    """A path of the new commit (``mode`` set) or one the diff deleted (``mode`` None)."""

    path: str
    mode: str | None
    oid: str | None
    size: int | None


@dataclass(frozen=True, slots=True)
class _Stored:
    content_hash: str
    deleted: bool
    stream_id: UUID | None


@dataclass(frozen=True, slots=True)
class _File:
    """A file to write."""

    record_id: str
    kind: FileKind
    path: str
    oid: str
    size: int
    live: bool
    """Its record is live now: writing it is a change, not an addition."""


@dataclass(slots=True)
class _Plan:
    writes: list[_File] = field(default_factory=list[_File])
    tombstones: dict[str, None] = field(default_factory=dict[str, None])
    relinks: list[str] = field(default_factory=list[str])
    unchanged: int = 0
    skipped: Counter[SkipReason] = field(default_factory=Counter[SkipReason])


def _plan(
    entries: Iterable[_Entry],
    stored: Mapping[str, _Stored],
    *,
    source_id: UUID,
    stream: GitStream,
    rules: FileRules,
    full: bool,
    forgotten: Collection[str] = (),
) -> _Plan:
    plan = _Plan()
    selected: set[str] = set()
    for entry in entries:
        if entry.mode is None:  # deleted by the diff
            if storable_path(entry.path):
                plan.tombstones[file_record_id(source_id, stream.key, entry.path)] = None
            continue
        reason = rules.skip_reason(entry.path, entry.mode, entry.size)
        if reason is not None:
            plan.skipped[reason] += 1
            if reason != "bad_path":
                plan.tombstones[file_record_id(source_id, stream.key, entry.path)] = None
            continue
        if entry.oid is None or entry.size is None:  # a selected file has both
            raise RuntimeError(f"no blob id or size for {entry.path!r}")
        rid = file_record_id(source_id, stream.key, entry.path)
        if rid in forgotten:
            plan.skipped["forgotten"] += 1
            continue
        selected.add(rid)
        record = stored.get(rid)
        live = record is not None and not record.deleted
        if record is None or record.deleted or record.content_hash != entry.oid:
            kind = file_kind(entry.path)
            plan.writes.append(_File(rid, kind, entry.path, entry.oid, entry.size, live))
        else:
            plan.unchanged += 1
            if record.stream_id != stream.id:
                plan.relinks.append(rid)
    if full:  # the records of the ref no file selects, of whatever kind
        for rid in stored:
            if rid not in selected:
                plan.tombstones[rid] = None
    # Only live records become tombstones.
    plan.tombstones = {
        rid: None
        for rid in plan.tombstones
        if (record := stored.get(rid)) is not None and not record.deleted
    }
    plan.writes.sort(key=lambda file: file.path)
    return plan


async def _stored_by_id(conn: Conn, record_ids: Sequence[str]) -> dict[str, _Stored]:
    if not record_ids:
        return {}
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT id, content_hash, deleted_at IS NOT NULL, stream_id FROM record"
            " WHERE id = ANY(%s)",
            (list(record_ids),),
        )
        return {row[0]: _Stored(row[1], row[2], row[3]) for row in await cur.fetchall()}


async def _forgotten(conn: Conn, record_ids: Collection[str]) -> set[str]:
    if not record_ids:
        return set()
    async with conn.transaction():
        return await forgotten_record_ids(conn, record_ids)


async def _stored_of_ref(conn: Conn, source_id: UUID, ref: str) -> dict[str, _Stored]:
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT id, content_hash, deleted_at IS NOT NULL, stream_id FROM record"
            " WHERE source_id = %s AND kind IN ('code_file', 'document')"
            " AND starts_with(external_id, %s)",
            (source_id, f"{ref}:"),
        )
        return {row[0]: _Stored(row[1], row[2], row[3]) for row in await cur.fetchall()}


async def _plan_diff(
    conn: Conn,
    mirrors: MirrorManager,
    mirror: Mirror,
    source: GitSource,
    stream: GitStream,
    rules: FileRules,
    old: str,
    new: str,
) -> _Plan:
    """The files that differ from the pinned commit ``old`` to ``new``, against their records."""
    changes = await mirrors.diff_names(mirror, old, new)
    sizes = await mirrors.blob_sizes(
        mirror, sorted({change.new_oid for change in changes if change.new_oid is not None})
    )
    entries = [
        _Entry(
            change.path,
            change.new_mode,
            change.new_oid,
            None if change.new_oid is None else sizes.get(change.new_oid),
        )
        for change in changes
    ]
    record_ids = [
        file_record_id(source.id, stream.key, entry.path)
        for entry in entries
        if storable_path(entry.path)
    ]
    stored = await _stored_by_id(conn, record_ids)
    return _plan(
        entries,
        stored,
        source_id=source.id,
        stream=stream,
        rules=rules,
        full=False,
        forgotten=await _forgotten(conn, record_ids),
    )


async def _plan_listing(
    conn: Conn,
    mirrors: MirrorManager,
    mirror: Mirror,
    source: GitSource,
    stream: GitStream,
    rules: FileRules,
    commit: str,
) -> _Plan:
    """Every file of ``commit``, against every record of the ref."""
    entries = [
        _Entry(entry.path, entry.mode, entry.oid, entry.size)
        for entry in await mirrors.ls_tree(mirror, commit)
    ]
    stored = await _stored_of_ref(conn, source.id, stream.key)
    forgotten = await _forgotten(
        conn,
        [
            file_record_id(source.id, stream.key, entry.path)
            for entry in entries
            if storable_path(entry.path)
        ],
    )
    return _plan(
        entries,
        stored,
        source_id=source.id,
        stream=stream,
        rules=rules,
        full=True,
        forgotten=forgotten,
    )


# --- Writing -------------------------------------------------------------------------------------

_UPSERT_RECORD: Final = (
    "INSERT INTO record (id, workspace_id, owner_id, source_id, stream_id, kind, external_id,"
    " content_hash, occurred_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (id) DO UPDATE SET stream_id = EXCLUDED.stream_id,"
    " content_hash = EXCLUDED.content_hash, occurred_at = EXCLUDED.occurred_at,"
    " deleted_at = NULL, updated_at = now()"
    " WHERE record.content_hash <> EXCLUDED.content_hash OR record.deleted_at IS NOT NULL"
    " OR record.stream_id IS DISTINCT FROM EXCLUDED.stream_id"
)

_TOMBSTONE: Final = (
    "UPDATE record SET deleted_at = now(), updated_at = now()"
    " WHERE id = ANY(%s) AND deleted_at IS NULL RETURNING id"
)

# Every live file record of one ref of the source: its external ids start with "<ref>:".
_TOMBSTONE_REF: Final = (
    "UPDATE record SET deleted_at = now(), updated_at = now()"
    " WHERE source_id = %s AND kind IN ('code_file', 'document')"
    " AND starts_with(external_id, %s) AND deleted_at IS NULL RETURNING id"
)

# Live file records of the source on a ref that no stream tracks. Removing a ref deletes its stream,
# which sets the records' stream_id to NULL; a stream added again for the same ref adopts them.
_UNTRACKED: Final = (
    "UPDATE record r SET deleted_at = now(), updated_at = now()"
    " WHERE r.source_id = %s AND r.kind IN ('code_file', 'document') AND r.stream_id IS NULL"
    " AND r.deleted_at IS NULL AND NOT EXISTS ("
    "   SELECT 1 FROM stream s WHERE s.source_id = r.source_id"
    "   AND starts_with(r.external_id, s.key || ':'))"
    " RETURNING r.id"
)

_MOVE_CURSOR: Final = (
    "UPDATE stream SET cursor = %s WHERE id = %s AND cursor IS NOT DISTINCT FROM %s::jsonb"
)

_MOVE_PIN: Final = (
    "UPDATE stream SET cursor = %s, checked_until = greatest(checked_until, %s)"
    " WHERE id = %s AND cursor IS NOT DISTINCT FROM %s::jsonb"
)


def _batches(files: Sequence[_File], limits: SyncLimits) -> Iterator[list[_File]]:
    batch: list[_File] = []
    size = 0
    for file in files:
        if batch and (len(batch) >= limits.batch_files or size + file.size > limits.batch_bytes):
            yield batch
            batch, size = [], 0
        batch.append(file)
        size += file.size
    if batch:
        yield batch


def _chunk(file: _File, content: bytes) -> FileChunks:
    if file.kind == "document":
        return chunk_markdown(file.record_id, file.path, content)
    return chunk_file(file.record_id, file.path, content)


def _chunk_all(files: Sequence[tuple[_File, bytes]]) -> list[FileChunks]:
    return [_chunk(file, content) for file, content in files]


async def _prepare(
    mirrors: MirrorManager, mirror: Mirror, batch: Sequence[_File]
) -> list[tuple[_File, FileChunks]]:
    """Read the batch from the mirror and chunk it, outside any transaction."""
    blobs = await mirrors.read_blobs(mirror, sorted({file.oid for file in batch}))
    contents: list[tuple[_File, bytes]] = []
    for file in batch:
        content = blobs.get(file.oid)
        if content is None:
            raise RuntimeError(
                f"the mirror of source {mirror.source_id} lacks blob {file.oid} of {file.path!r}"
            )
        contents.append((file, content))
    chunks = await anyio.to_thread.run_sync(_chunk_all, contents)
    return [(file, file_chunks) for (file, _), file_chunks in zip(contents, chunks, strict=True)]


async def _tombstone(
    conn: Conn, query: QueryNoTemplate, params: Sequence[object]
) -> tuple[int, IndexCounts]:
    """Turn live records into tombstones with ``query`` (an UPDATE that returns their ids), and
    delete their chunks and symbols, in the caller's transaction. Returns how many records, and
    the chunks and symbols deleted (not reported yet)."""
    cur = await conn.execute(query, params)
    record_ids: list[str] = [row[0] for row in await cur.fetchall()]
    counts = IndexCounts()
    for rid in record_ids:
        counts += await delete_record_chunks(conn, record_id=rid)
    return len(record_ids), counts


@dataclass(slots=True)
class _Tally:
    added: int = 0
    changed: int = 0
    deleted: int = 0
    binary: int = 0
    index: IndexCounts = field(default_factory=IndexCounts)
    binary_live: list[str] = field(default_factory=list[str])
    """Live records of files that turned out binary: they become tombstones in the same
    transaction."""

    def tombstoned(self, done: tuple[int, IndexCounts]) -> None:
        records, counts = done
        self.deleted += records
        self.index += counts


class _Progress:
    """The ``progress`` detail of the run report while the files are written: how many files the
    sync writes, how many it has read, chunked and embedded (``files_processed``), and how many of
    those have committed (``files_committed``; a batch that rolls back takes its files out of
    ``files_processed`` again). It is saved through the run's own connection, outside the sync's
    transactions, at most every few seconds (:meth:`memini.runs.Run.save_progress_if_due`)."""

    def __init__(self, total: int, batches: int) -> None:
        self.run = current_run()
        self.value: dict[str, JsonValue] = {
            "files_total": total,
            "files_processed": 0,
            "files_committed": 0,
            "batches": batches,
            "batch": 0,
            "started_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

    def _set(self, **changes: int) -> None:
        self.value.update(changes)
        if self.run is not None:
            self.run.detail("progress", self.value)

    async def start(self) -> None:
        self._set()
        if self.run is not None:
            await self.run.save_progress()

    async def batch(self, number: int) -> None:
        self._set(batch=number)

    async def file_done(self) -> None:
        self._set(files_processed=_int(self.value["files_processed"]) + 1)
        if self.run is not None:
            await self.run.save_progress_if_due()

    async def committed(self, files: int) -> None:
        self._set(
            files_committed=_int(self.value["files_committed"]) + files,
            files_processed=_int(self.value["files_committed"]) + files,
        )
        if self.run is not None:
            await self.run.save_progress_if_due()

    async def rolled_back(self) -> None:
        self._set(files_processed=_int(self.value["files_committed"]))


def _int(value: JsonValue) -> int:
    return value if isinstance(value, int) else 0


async def _write_files(
    conn: Conn,
    embedder: Embedder,
    *,
    source: GitSource,
    stream: GitStream,
    files: Sequence[tuple[_File, FileChunks]],
    occurred_at: Mapping[str, datetime],
    step: _Tally,
    progress: "_Progress",
    deadline: float | None,
) -> int:
    """Write files of ``files`` in the caller's transaction, in order, until all are written or
    ``deadline`` (a :func:`time.monotonic` time) has passed with files left. Returns how many were
    taken; binary files among them are only counted, and their live records returned in
    ``step``'s tally through the caller."""
    taken = 0
    for file, chunks in files:
        taken += 1
        if chunks.fallback == "binary":
            step.binary += 1
            if file.live:
                step.binary_live.append(file.record_id)
        else:
            await conn.execute(
                _UPSERT_RECORD,
                (
                    file.record_id,
                    source.workspace_id,
                    source.owner_id,
                    source.id,
                    stream.id,
                    file.kind,
                    code_external_id(stream.key, file.path),
                    file.oid,
                    occurred_at[file.path],
                ),
            )
            step.index += await index_record(
                conn, embedder, record_id=file.record_id, path=file.path, chunks=chunks
            )
            if file.live:
                step.changed += 1
            else:
                step.added += 1
        await progress.file_done()
        if deadline is not None and time.monotonic() >= deadline and taken < len(files):
            break
    return taken


async def _apply(
    conn: Conn,
    mirrors: MirrorManager,
    mirror: Mirror,
    embedder: Embedder,
    *,
    source: GitSource,
    stream: GitStream,
    plan: _Plan,
    pin: Pin,
    occurred_at: Mapping[str, datetime],
    checked_until: datetime,
    limits: SyncLimits,
    early_commits: bool,
) -> _Tally:
    """Write the plan batch by batch, each batch in transactions of its own; the last one moves
    the pin. With ``early_commits`` (a full listing: a first sync, or one after the rules
    changed), a transaction also commits once ``limits.commit_seconds`` of writing have passed,
    and the rest of its batch goes to the next one, so a long first sync keeps its work if it
    stops (see the module)."""
    tally = _Tally()
    run = current_run()
    batches = list(_batches(plan.writes, limits)) or [[]]
    expected = None if stream.cursor is None else Jsonb(stream.cursor)
    previous = read_pin(stream.cursor)
    progress = _Progress(len(plan.writes), len(batches) if plan.writes else 0)
    await progress.start()
    for number, batch in enumerate(batches, start=1):
        last_batch = number == len(batches)
        if batch:
            await progress.batch(number)
        prepared = await _prepare(mirrors, mirror, batch) if batch else []
        done = 0
        while True:
            step = _Tally()
            deadline = time.monotonic() + limits.commit_seconds if early_commits else None
            try:
                async with conn.transaction():
                    taken = await _write_files(
                        conn,
                        embedder,
                        source=source,
                        stream=stream,
                        files=prepared[done:],
                        occurred_at=occurred_at,
                        step=step,
                        progress=progress,
                        deadline=deadline,
                    )
                    final = last_batch and done + taken == len(prepared)
                    if step.binary_live:
                        step.tombstoned(await _tombstone(conn, _TOMBSTONE, (step.binary_live,)))
                    if final:
                        if plan.tombstones:
                            gone = list(plan.tombstones)
                            step.tombstoned(await _tombstone(conn, _TOMBSTONE, (gone,)))
                        step.tombstoned(await _tombstone(conn, _UNTRACKED, (source.id,)))
                        if plan.relinks:
                            await conn.execute(
                                "UPDATE record SET stream_id = %s WHERE id = ANY(%s)",
                                (stream.id, plan.relinks),
                            )
                        cur = await conn.execute(
                            _MOVE_PIN, (Jsonb(pin.to_json()), checked_until, stream.id, expected)
                        )
                        if cur.rowcount != 1:
                            raise StreamChangedError(f"stream {stream.id} changed while it synced")
                    elif previous is not None and not previous.partial:
                        # Records past the pin are about to commit: if the sync stops before the
                        # pin moves, the next one must compare everything, not diff from the pin.
                        marked = Pin(previous.commit, previous.rules, partial=True).to_json()
                        cur = await conn.execute(_MOVE_CURSOR, (Jsonb(marked), stream.id, expected))
                        if cur.rowcount != 1:
                            raise StreamChangedError(f"stream {stream.id} changed while it synced")
                        expected, previous = Jsonb(marked), read_pin(marked)
            except BaseException:
                await progress.rolled_back()
                raise
            done += taken
            await progress.committed(taken)
            # The transaction has committed: only now do its counts go to the run report.
            tally.added += step.added
            tally.changed += step.changed
            tally.deleted += step.deleted
            tally.binary += step.binary
            tally.index += step.index
            step.index.report()
            if run is not None:
                run.count("files_added", step.added)
                run.count("files_changed", step.changed)
                run.count("files_deleted", step.deleted)
                if step.binary:
                    run.count("files_skipped", step.binary)
                    run.count("files_skipped_binary", step.binary)
            if done >= len(prepared):
                break
    return tally


# --- The sync ------------------------------------------------------------------------------------


@asynccontextmanager
async def _step(name: str) -> AsyncGenerator[None]:
    run = current_run()
    if run is None:
        yield
        return
    async with run.step(name):
        yield


async def _change_times(
    mirrors: MirrorManager,
    mirror: Mirror,
    writes: Sequence[_File],
    *,
    commit: str,
    since: str | None,
    limits: SyncLimits,
) -> dict[str, datetime]:
    """The ``occurred_at`` of each file to write, by path: when it last changed on the ref (see
    the module). Two git processes, none when there is nothing to write."""
    if not writes:
        return {}
    committed = await mirrors.committed_at(mirror, commit)
    try:
        walk = await mirrors.change_times(
            mirror, commit, since=since, max_commits=limits.history_commits
        )
    except GitError as error:  # the times are worth a warning, not the sync
        log.warning(
            "the history of %s in the mirror of source %s could not be read (%s); its files take"
            " the time of the commit",
            commit,
            mirror.source_id,
            error,
        )
        return {file.path: committed for file in writes}
    fallback = committed if walk.complete or walk.oldest is None else walk.oldest
    return {file.path: walk.times.get(file.path, fallback) for file in writes}


async def _sync_missing(
    conn: Conn, *, source: GitSource, stream: GitStream, checked_until: datetime
) -> StreamSyncResult:
    """The ref of ``stream`` is not in the fetched mirror: tombstone its records, mark the stream
    (see the module)."""
    expected = None if stream.cursor is None else Jsonb(stream.cursor)
    marker = missing_cursor(stream.cursor)
    step = _Tally()
    async with _step("tombstone"), conn.transaction():
        of_ref = await _tombstone(conn, _TOMBSTONE_REF, (source.id, f"{stream.key}:"))
        step.tombstoned(of_ref)
        step.tombstoned(await _tombstone(conn, _UNTRACKED, (source.id,)))
        cur = await conn.execute(_MOVE_PIN, (Jsonb(marker), checked_until, stream.id, expected))
        if cur.rowcount != 1:
            raise StreamChangedError(f"stream {stream.id} changed while it synced")
    step.index.report()
    run = current_run()
    if run is not None:
        run.count("refs_missing")
        run.count("files_deleted", step.deleted)
    previous = read_pin(stream.cursor)
    log.warning(
        "%s of source %s is not at the remote; its %d live records are tombstones until it is back",
        stream.key,
        source.id,
        of_ref[0],
        extra={"source_id": str(source.id), "stream_id": str(stream.id)},
    )
    return StreamSyncResult(
        stream_id=stream.id,
        ref=stream.key,
        previous=None if previous is None else previous.commit,
        commit=None,
        listed=False,
        added=0,
        changed=0,
        deleted=step.deleted,
        unchanged=0,
        skipped={},
        index=step.index,
        missing=True,
    )


async def sync_stream(
    conn: Conn,
    mirrors: MirrorManager,
    mirror: Mirror,
    embedder: Embedder,
    *,
    source: GitSource,
    stream: GitStream,
    checked_until: datetime,
    limits: SyncLimits = DEFAULT_LIMITS,
) -> StreamSyncResult:
    """Bring the records and chunks of ``stream`` to the commit its ref names in ``mirror``, then
    pin it there. See the module for how.

    ``conn`` is a connection of the caller's that is idle (no transaction open): the sync commits
    its own transactions. ``mirror`` is the source's mirror, fetched by the caller.
    ``checked_until`` is the database time before that fetch. The run report of the job, if one is
    recorded, gets the steps (``diff`` or ``list``, ``index``) and the counts ``files_added``,
    ``files_changed``, ``files_deleted``, ``files_unchanged``, ``files_skipped`` and
    ``files_skipped_<reason>``, and those of the index writer (``chunks_inserted``, ...), each
    batch's once it has committed: a batch that rolls back adds none. A ref that the mirror does
    not hold is handled as the module says (the step ``tombstone``, the count ``refs_missing``).

    Raises:
        ValueError: ``conn`` has a transaction open.
        RevisionNotFound: the ref is in the mirror but names no commit (a tag of a tree or blob).
        IndexMismatchError: the workspace's index is out of date; see :func:`index_record`.
        StreamChangedError: the stream's cursor changed meanwhile.
        GitError, EmbeddingError: reading or embedding failed. The pin stays where it was.
    """
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("sync_stream commits its own transactions: pass an idle connection")
    run = current_run()
    try:
        commit = await mirrors.resolve(mirror, stream.key)
    except RevisionNotFound:
        if stream.key in await mirrors.refs(mirror):
            raise
        return await _sync_missing(conn, source=source, stream=stream, checked_until=checked_until)
    rules = FileRules(source.config, mirrors.options.max_blob_size)
    previous = read_pin(stream.cursor)

    plan: _Plan | None = None
    if previous is not None and previous.rules == rules.digest and not previous.partial:
        try:
            async with _step("diff"):
                plan = await _plan_diff(
                    conn, mirrors, mirror, source, stream, rules, previous.commit, commit
                )
        except RevisionNotFound:
            log.warning(
                "the pinned commit %s of %s is no longer in the mirror of source %s; listing the"
                " ref in full",
                previous.commit,
                stream.key,
                source.id,
            )
    listed = plan is None
    if plan is None:
        async with _step("list"):
            plan = await _plan_listing(conn, mirrors, mirror, source, stream, rules, commit)

    if run is not None:
        run.count("files_unchanged", plan.unchanged)
        run.count("files_skipped", sum(plan.skipped.values()))
        for reason, count in plan.skipped.items():
            run.count(f"files_skipped_{reason}", count)

    async with _step("index"):
        occurred_at = await _change_times(
            mirrors,
            mirror,
            plan.writes,
            commit=commit,
            since=None if listed or previous is None else previous.commit,
            limits=limits,
        )
        tally = await _apply(
            conn,
            mirrors,
            mirror,
            embedder,
            source=source,
            stream=stream,
            plan=plan,
            pin=Pin(commit, rules.digest),
            occurred_at=occurred_at,
            checked_until=checked_until,
            limits=limits,
            early_commits=listed,
        )
    skipped = Counter(plan.skipped)
    if tally.binary:
        skipped["binary"] += tally.binary
    result = StreamSyncResult(
        stream_id=stream.id,
        ref=stream.key,
        previous=None if previous is None else previous.commit,
        commit=commit,
        listed=listed,
        added=tally.added,
        changed=tally.changed,
        deleted=tally.deleted,
        unchanged=plan.unchanged,
        skipped=dict(skipped),
        index=tally.index,
    )
    log.info(
        "synced %s of source %s at %s: %d added, %d changed, %d deleted, %d unchanged, %d skipped",
        stream.key,
        source.id,
        commit,
        result.added,
        result.changed,
        result.deleted,
        result.unchanged,
        sum(skipped.values()),
        extra={"source_id": str(source.id), "stream_id": str(stream.id), "commit": commit},
    )
    return result
