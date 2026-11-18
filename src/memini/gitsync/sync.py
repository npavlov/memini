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
    tombstones.

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

    The pin moves by compare-and-set on the cursor the sync started from; a stream changed by
    anyone else in between rolls the last transaction back.

Untracked refs
    The last transaction also turns into tombstones the live records of the source whose ref is no
    longer tracked: removing a ref deletes its stream and leaves its records without one.
"""

import logging
from collections import Counter
from collections.abc import AsyncGenerator, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID

import anyio
from psycopg import AsyncConnection, pq
from psycopg.abc import QueryNoTemplate
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from memini.chunking import FileChunks, chunk_file, chunk_markdown
from memini.embeddings import Embedder
from memini.git import Mirror, MirrorManager, RevisionNotFound
from memini.gitsync.model import Pin, code_external_id, file_record_id, read_pin
from memini.gitsync.rules import FileKind, FileRules, SkipReason, file_kind, storable_path
from memini.index import IndexCounts, delete_record_chunks, index_record
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
    """How many files, and how many bytes of them, one transaction writes at most."""

    batch_files: int = 100
    batch_bytes: int = 8 * 1024**2

    def __post_init__(self) -> None:
        if self.batch_files < 1 or self.batch_bytes < 1:
            raise ValueError("a batch holds at least one file and one byte")


DEFAULT_LIMITS: Final = SyncLimits()


@dataclass(frozen=True, slots=True)
class GitSource:
    """A git source as a sync needs it."""

    id: UUID
    workspace_id: UUID
    owner_id: UUID
    config: GitConfig


@dataclass(frozen=True, slots=True)
class GitStream:
    """A stream of a git source: one tracked ref, and its cursor as stored."""

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
    commit: str
    """The commit it is pinned at now."""
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
    stored = await _stored_by_id(
        conn,
        [
            file_record_id(source.id, stream.key, entry.path)
            for entry in entries
            if storable_path(entry.path)
        ],
    )
    return _plan(entries, stored, source_id=source.id, stream=stream, rules=rules, full=False)


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
    return _plan(entries, stored, source_id=source.id, stream=stream, rules=rules, full=True)


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


async def _tombstone(conn: Conn, query: QueryNoTemplate, params: Sequence[object]) -> int:
    """Turn live records into tombstones with ``query`` (an UPDATE that returns their ids), and
    delete their chunks and symbols. Returns how many."""
    cur = await conn.execute(query, params)
    record_ids: list[str] = [row[0] for row in await cur.fetchall()]
    for rid in record_ids:
        await delete_record_chunks(conn, record_id=rid)
    return len(record_ids)


@dataclass(slots=True)
class _Tally:
    added: int = 0
    changed: int = 0
    deleted: int = 0
    binary: int = 0
    index: IndexCounts = field(default_factory=IndexCounts)


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
    occurred_at: datetime,
    checked_until: datetime,
    limits: SyncLimits,
) -> _Tally:
    tally = _Tally()
    run = current_run()
    batches = list(_batches(plan.writes, limits)) or [[]]
    expected = None if stream.cursor is None else Jsonb(stream.cursor)
    previous = read_pin(stream.cursor)
    for number, batch in enumerate(batches, start=1):
        last = number == len(batches)
        prepared = await _prepare(mirrors, mirror, batch) if batch else []
        step = _Tally()
        async with conn.transaction():
            binary: list[str] = []
            for file, chunks in prepared:
                if chunks.fallback == "binary":
                    step.binary += 1
                    if file.live:
                        binary.append(file.record_id)
                    continue
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
                        occurred_at,
                    ),
                )
                step.index += await index_record(
                    conn, embedder, record_id=file.record_id, path=file.path, chunks=chunks
                )
                if file.live:
                    step.changed += 1
                else:
                    step.added += 1
            if binary:
                step.deleted += await _tombstone(conn, _TOMBSTONE, (binary,))
            if last:
                if plan.tombstones:
                    step.deleted += await _tombstone(conn, _TOMBSTONE, (list(plan.tombstones),))
                step.deleted += await _tombstone(conn, _UNTRACKED, (source.id,))
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
                # Records past the pin are about to commit: if the sync stops before the pin moves,
                # the next one must compare everything, not diff from the pin.
                marked = Pin(previous.commit, previous.rules, partial=True).to_json()
                cur = await conn.execute(_MOVE_CURSOR, (Jsonb(marked), stream.id, expected))
                if cur.rowcount != 1:
                    raise StreamChangedError(f"stream {stream.id} changed while it synced")
                expected, previous = Jsonb(marked), read_pin(marked)
        tally.added += step.added
        tally.changed += step.changed
        tally.deleted += step.deleted
        tally.binary += step.binary
        tally.index += step.index
        if run is not None:
            run.count("files_added", step.added)
            run.count("files_changed", step.changed)
            run.count("files_deleted", step.deleted)
            if step.binary:
                run.count("files_skipped", step.binary)
                run.count("files_skipped_binary", step.binary)
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
    ``files_skipped_<reason>``; the index writer adds its own.

    Raises:
        ValueError: ``conn`` has a transaction open.
        RevisionNotFound: the ref is not in the mirror (it does not exist at the remote).
        IndexMismatchError: the workspace's index is out of date; see :func:`index_record`.
        StreamChangedError: the stream's cursor changed meanwhile.
        GitError, EmbeddingError: reading or embedding failed. The pin stays where it was.
    """
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("sync_stream commits its own transactions: pass an idle connection")
    run = current_run()
    commit = await mirrors.resolve(mirror, stream.key)
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

    occurred_at = await mirrors.committed_at(mirror, commit)
    async with _step("index"):
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
