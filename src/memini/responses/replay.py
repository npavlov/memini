"""From stored responses to records: :func:`ingest_window` for the executors, once a window's
calls are stored, and :func:`replay` over windows stored before. See :mod:`memini.responses`."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID

import anyio
from psycopg import AsyncConnection, pq, sql
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from memini.auth import WriteAccess
from memini.ids import RecordKind, record_id
from memini.ingest import (
    DEFAULT_LIMITS,
    Batch,
    IngestLimits,
    IngestResult,
    RecordIn,
    RecordIndexer,
    WindowClaim,
    WindowGapError,
    ingest,
    prepare_batch,
)
from memini.jobs import source_lock_key
from memini.recipes.model import Recipe
from memini.recipes.parsing import (
    ParsedRecord,
    ParsedWindow,
    ParserError,
    RawResponse,
    WindowInput,
    WindowParser,
    parse_window,
)
from memini.responses.store import (
    RawWindow,
    load_window,
    record_parse,
    stored_record_ids,
)
from memini.runs import current_run, record_run
from memini.sources import SourceBusyError
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "RUN_KIND",
    "ReplayError",
    "ReplayResult",
    "WindowIngest",
    "ingest_window",
    "replay",
]

type Conn = AsyncConnection[TupleRow]

log = logging.getLogger(__name__)

RUN_KIND: Final = "replay"
"""The kind of the run a replay records."""


class ReplayError(Exception):
    """The stored responses cannot be read with this recipe and parser: a window was fetched from
    another server, the parser failed, or it built records the ingest contract refuses. A replay
    that raises it has written nothing. The message names the window, never a value."""


@dataclass(frozen=True, slots=True)
class WindowIngest:
    """What one window's records did when they were ingested."""

    window_id: UUID
    complete: bool
    """The window's claim: every check passed, and forget pruned none of its responses."""
    incomplete: tuple[str, ...]
    """Why it is not complete."""
    records: int
    """Records the parser built."""
    sent: int
    """Records sent to ingest (replay leaves some out: see :func:`replay`)."""
    inserted: int = 0
    updated: int = 0
    revived: int = 0
    deleted: int = 0
    unchanged: int = 0
    reconciled: int = 0
    checkpoint_moved: bool = False
    attribution_changed: int = 0
    """Responses whose recorded records changed with this parse."""


# --- One window ----------------------------------------------------------------------------------


def _check_server(window: RawWindow, recipe: Recipe) -> None:
    if window.server.name != recipe.server.name:
        raise ReplayError(
            f"window {window.id} was fetched from the server {window.server.name}; the recipe"
            f" {recipe.name} is for {recipe.server.name}"
        )


async def _parse(
    recipe: Recipe, parser: WindowParser, window: RawWindow, responses: tuple[RawResponse, ...]
) -> ParsedWindow:
    """The parser over the window, in a worker thread: parsing is CPU work."""
    _check_server(window, recipe)
    data = WindowInput(
        recipe=recipe,
        stream=window.stream,
        start=window.start,
        end=window.end,
        responses=responses,
    )
    return await anyio.to_thread.run_sync(parse_window, parser, data)


def _rid(source_id: UUID, record: RecordIn) -> str:
    return record_id(source_id, cast(RecordKind, record.kind), record.external_id)


async def _send(
    conn: Conn,
    access: WriteAccess,
    window: RawWindow,
    records: Sequence[RecordIn],
    claim: WindowClaim,
    *,
    limits: IngestLimits,
    runs: AsyncConnectionPool[Any] | AsyncConnection[Any] | None,
    indexer: RecordIndexer | None,
    gap_as_incomplete: bool,
) -> list[IngestResult]:
    """Ingest ``records`` in batches of at most ``limits.max_records``, the last one claiming the
    window, so the checkpoint moves only after every record of the window is stored."""
    size = max(1, limits.max_records)
    chunks = [list(records[i : i + size]) for i in range(0, len(records), size)] or [[]]
    results: list[IngestResult] = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        batch = Batch(records=chunk, stream=window.stream, window=claim if last else None)
        try:
            result = await ingest(
                conn,
                access,
                window.workspace,
                window.source,
                batch,
                limits=limits,
                runs=runs,
                indexer=indexer,
            )
        except WindowGapError:
            if not gap_as_incomplete:
                raise
            # The window is complete now, but an earlier one of the stream still is not: its
            # records are stored, and the checkpoint waits for the earlier window.
            log.info("window %s starts after the checkpoint; claimed incomplete", window.id)
            run = current_run()
            if run is not None:
                run.count("windows_gap")
            batch = replace(batch, window=replace(claim, complete=False, snapshot=False))
            result = await ingest(
                conn,
                access,
                window.workspace,
                window.source,
                batch,
                limits=limits,
                runs=runs,
                indexer=indexer,
            )
        results.append(result)
    return results


def _tally(
    window: RawWindow,
    parsed: ParsedWindow,
    complete: bool,
    sent: int,
    results: Sequence[IngestResult],
    changed: int,
) -> WindowIngest:
    return WindowIngest(
        window_id=window.id,
        complete=complete,
        incomplete=parsed.incomplete
        + (("forget removed responses of the window",) if window.pruned_at else ()),
        records=len(parsed.records),
        sent=sent,
        inserted=sum(r.inserted for r in results),
        updated=sum(r.updated for r in results),
        revived=sum(r.revived for r in results),
        deleted=sum(r.deleted for r in results),
        unchanged=sum(r.unchanged for r in results),
        reconciled=sum(r.reconciled for r in results),
        checkpoint_moved=any(r.checkpoint_moved for r in results),
        attribution_changed=changed,
    )


async def ingest_window(
    conn: Conn,
    access: WriteAccess,
    window_id: UUID,
    *,
    recipe: Recipe,
    parser: WindowParser,
    limits: IngestLimits = DEFAULT_LIMITS,
    runs: AsyncConnectionPool[Any] | AsyncConnection[Any] | None = None,
    indexer: RecordIndexer | None = None,
) -> WindowIngest:
    """Parse the stored responses of a window and ingest its records, claiming the window: what
    an executor does once the window's calls are stored (``store_response``).

    The parser runs over the window's responses (:func:`memini.recipes.parsing.parse_window`), its
    attribution is recorded (:func:`~memini.responses.record_parse`), then every record goes to
    ingest in batches of at most ``limits.max_records``, the last one claiming the window:
    complete when every check passed, a snapshot when the recipe's ``stream.snapshot`` says so.
    ``conn`` is idle: each step commits its own transaction.

    Raises:
        WindowNotFoundError: no such window.
        ReplayError: the window was fetched from another server than the recipe's.
        ParserError: the parser failed or broke the protocol.
        Exception: whatever :func:`memini.ingest.ingest` raises.
    """
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("ingest_window commits its own transactions: pass an idle connection")
    window, responses = await load_window(conn, window_id)
    parsed = await _parse(recipe, parser, window, responses)
    complete = parsed.complete and window.pruned_at is None
    changed = await record_parse(conn, window, parsed, responses, complete=complete)
    claim = window.planned.claim(complete=complete, snapshot=recipe.stream.snapshot)
    records = [item.record for item in parsed.records]
    results = await _send(
        conn,
        access,
        window,
        records,
        claim,
        limits=limits,
        runs=runs,
        indexer=indexer,
        gap_as_incomplete=False,
    )
    return _tally(window, parsed, complete, len(records), results, changed)


# --- Replay --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What a replay did. Each record a replayed window's parse built counts once: sent to
    ingest (and then ``inserted``, ``updated``, ``revived``, ``deleted`` or ``unchanged``), or
    left out as ``superseded``, ``kept_deleted``, ``expired`` or ``forgotten``."""

    workspace_id: UUID
    source_id: UUID
    run_id: UUID | None
    windows: int = 0
    """Windows replayed."""
    windows_complete: int = 0
    responses: int = 0
    """Responses the parser read in them."""
    records: int = 0
    inserted: int = 0
    updated: int = 0
    revived: int = 0
    deleted: int = 0
    unchanged: int = 0
    superseded: int = 0
    """Records a later window also built: that window's version is the one stored."""
    kept_deleted: int = 0
    """Records stored as deleted at the source: replay does not bring them back."""
    expired: int = 0
    """Records older than the source's retention."""
    forgotten: int = 0
    """Records of a window forget pruned that are not stored: replay does not insert them."""
    checkpoints_moved: int = 0
    """Windows whose claim moved the stream's checkpoint or frontier."""
    attribution_changed: int = 0


@dataclass(frozen=True, slots=True)
class _Source:
    workspace_id: UUID
    source_id: UUID
    stream_id: UUID | None
    retention_days: int | None


async def _resolve(
    conn: Conn, access: WriteAccess, workspace: str, source: str, stream: str | None
) -> _Source:
    async with conn.transaction():
        found = await get_writable_workspace(conn, access, workspace)
        query = sql.SQL(
            "SELECT s.id, s.retention_days FROM source s"
            " WHERE s.workspace_id = %s AND s.name = %s AND {}"
        ).format(
            access.where(
                workspace_id=sql.Identifier("s", "workspace_id"),
                owner_id=sql.Identifier("s", "owner_id"),
                source_id=sql.Identifier("s", "id"),
            )
        )
        cur = await conn.execute(query, (found.id, source))
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError("source")
        source_id, retention_days = row
        stream_id: UUID | None = None
        if stream is not None:
            cur = await conn.execute(
                "SELECT id FROM stream WHERE source_id = %s AND key = %s", (source_id, stream)
            )
            found_stream = await cur.fetchone()
            if found_stream is None:
                raise NotFoundError("stream")
            stream_id = found_stream[0]
    return _Source(found.id, source_id, stream_id, retention_days)


@dataclass(frozen=True, slots=True)
class _Listed:
    id: UUID
    stream_id: UUID
    run_id: UUID | None


async def _windows(conn: Conn, source_id: UUID) -> list[_Listed]:
    """The parsed windows of the source, in the order they were started."""
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT id, stream_id, run_id FROM raw_window"
            " WHERE source_id = %s AND parsed_at IS NOT NULL ORDER BY started_at, id",
            (source_id,),
        )
        rows = await cur.fetchall()
    return [_Listed(row[0], row[1], row[2]) for row in rows]


async def _now(conn: Conn) -> datetime:
    async with conn.transaction():
        cur = await conn.execute("SELECT now()")
        row = await cur.fetchone()
    if row is None:  # pragma: no cover - SELECT now() returns a row
        raise RuntimeError("SELECT now() returned no row")
    return row[0]


async def _stored(conn: Conn, ids: Sequence[str]) -> dict[str, bool]:
    """The records among ``ids`` that are stored, and whether each is a tombstone."""
    if not ids:
        return {}
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT id, deleted_at IS NOT NULL FROM record WHERE id = ANY(%s::text[])",
            (list(ids),),
        )
        rows = await cur.fetchall()
    return {row[0]: row[1] for row in rows}


def _check_records(window: RawWindow, parsed: ParsedWindow, limits: IngestLimits) -> None:
    """Refuse, before anything is written, records the ingest contract would refuse."""
    records = [item.record for item in parsed.records]
    try:
        prepare_batch(
            Batch(records=records, stream=window.stream),
            replace(limits, max_records=max(1, len(records))),
        )
    except ValueError as error:  # InvalidBatchError names a field, never a value
        raise ReplayError(
            f"window {window.id}: the parser built a record ingest refuses: {error}"
        ) from None


async def _lock(conn: Conn, source_id: UUID, name: str) -> None:
    cur = await conn.execute("SELECT pg_try_advisory_lock(%s, %s)", source_lock_key(source_id))
    row = await cur.fetchone()
    if row is None or not row[0]:
        raise SourceBusyError(f"a job of source {name} is running; replay it when it is done")


async def _unlock(conn: Conn, source_id: UUID) -> None:
    if conn.closed:
        return
    try:
        await conn.execute("SELECT pg_advisory_unlock(%s, %s)", source_lock_key(source_id))
    except Exception:  # the session ends with the connection, and the lock with it
        log.warning("could not release the lock of the source after a replay", exc_info=True)


async def replay(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    source: str,
    *,
    recipe: Recipe,
    parser: WindowParser,
    stream: str | None = None,
    run_id: UUID | None = None,
    limits: IngestLimits = DEFAULT_LIMITS,
    runs: AsyncConnectionPool[Any] | AsyncConnection[Any] | None = None,
    indexer: RecordIndexer | None = None,
) -> ReplayResult:
    """Parse the stored responses of the source again and ingest what the parser builds, without
    calling the source: after a parser fix (see :mod:`memini.responses` for the rules).

    Replays the parsed windows of the source ``source`` of ``workspace``, of one stream (its key,
    ``stream``) or of one run (``run_id``) if given, in the order they were started. ``recipe``
    is the recipe as it is now, ``parser`` its parser. ``conn`` is idle; the source's job lock is
    held on it while the replay runs, so no sync of the source runs meanwhile. With ``runs``, a
    run of kind ``replay`` records the counts, the ingest batches' among them.

    Raises:
        NotFoundError: no such workspace, source or stream that ``access`` may change.
        SourceBusyError: a job of the source is running.
        ReplayError: a window was fetched from another server, the parser failed, or it built a
            record the ingest contract refuses; nothing was written.
        Exception: whatever :func:`memini.ingest.ingest` raises; the windows replayed before it
            stay, and a replay run again is safe.
    """
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("replay commits its own transactions: pass an idle connection")
    target = await _resolve(conn, access, workspace, source, stream)
    await _lock(conn, target.source_id, source)
    try:
        if runs is None:
            return await _replay(
                conn, access, target, recipe, parser, run_id, limits, None, indexer
            )
        async with record_run(
            runs,
            workspace_id=target.workspace_id,
            owner_id=access.owner_id,
            kind=RUN_KIND,
            source_id=target.source_id,
            stream_id=target.stream_id,
            progress_interval=None,
        ) as run:
            result = await _replay(
                conn, access, target, recipe, parser, run_id, limits, run.id, indexer
            )
            if result.windows and result.windows_complete < result.windows:
                run.set_outcome(
                    "partial", "some windows are incomplete; their checkpoints stay where they were"
                )
            return result
    finally:
        await _unlock(conn, target.source_id)


async def _replay(
    conn: Conn,
    access: WriteAccess,
    target: _Source,
    recipe: Recipe,
    parser: WindowParser,
    run_id: UUID | None,
    limits: IngestLimits,
    replay_run: UUID | None,
    indexer: RecordIndexer | None,
) -> ReplayResult:
    listed = await _windows(conn, target.source_id)
    selected = [
        index
        for index, window in enumerate(listed)
        if (target.stream_id is None or window.stream_id == target.stream_id)
        and (run_id is None or window.run_id == run_id)
    ]
    result = ReplayResult(
        workspace_id=target.workspace_id, source_id=target.source_id, run_id=replay_run
    )
    if not selected:
        return result
    chosen = set(selected)

    # Pass 1, before anything is written: parse every chosen window and every window started
    # after the first of them, all streams of the source, and find the last window that holds
    # each record. A window holds a record when its parse builds it now, or when its responses
    # were built into it at the last parse (a response forget emptied still counts: the record's
    # newest content was there).
    last: dict[str, int] = {}
    for index in range(selected[0], len(listed)):
        window, responses = await load_window(conn, listed[index].id)
        try:
            parsed = await _parse(recipe, parser, window, responses)
        except ParserError as error:  # names the parser and the problem, never a value
            raise ReplayError(f"window {window.id}: {error}") from None
        if index in chosen:
            _check_records(window, parsed, limits)
        held = {_rid(window.source_id, item.record) for item in parsed.records}
        held |= await stored_record_ids(conn, window.id)
        for rid in held:
            last[rid] = index

    cutoff: datetime | None = None
    if target.retention_days is not None:
        cutoff = await _now(conn) - timedelta(days=target.retention_days)

    # Pass 2: each chosen window again, in order, with only the records it is the last to hold.
    counts: dict[str, int] = {}

    def add(name: str, value: int) -> None:
        counts[name] = counts.get(name, 0) + value

    for index in selected:
        window, responses = await load_window(conn, listed[index].id)
        parsed = await _parse(recipe, parser, window, responses)
        complete = parsed.complete and window.pruned_at is None
        changed = await record_parse(conn, window, parsed, responses, complete=complete)

        owned: list[ParsedRecord] = []
        for item in parsed.records:
            if last.get(_rid(window.source_id, item.record)) == index:
                owned.append(item)
            else:
                add("superseded", 1)
        state = await _stored(conn, [_rid(window.source_id, item.record) for item in owned])
        records: list[RecordIn] = []
        for item in owned:
            record = item.record
            rid = _rid(window.source_id, record)
            if state.get(rid) is True:
                add("kept_deleted", 1)  # a source's deletion is not undone from older responses
            elif rid not in state and window.pruned_at is not None:
                add("forgotten", 1)  # what forget removed is not brought back
            elif (
                cutoff is not None
                and not record.deleted
                and record.occurred_at is not None
                and record.occurred_at < cutoff
            ):
                add("expired", 1)
            else:
                records.append(record)

        claim = window.planned.claim(complete=complete)  # never a snapshot: see the module
        results = await _send(
            conn,
            access,
            window,
            records,
            claim,
            limits=limits,
            runs=None,
            indexer=indexer,
            gap_as_incomplete=True,
        )
        done = _tally(window, parsed, complete, len(records), results, changed)
        add("windows", 1)
        add("windows_complete", int(complete))
        add("responses", len(responses))
        add("records", done.records)
        add("inserted", done.inserted)
        add("updated", done.updated)
        add("revived", done.revived)
        add("deleted", done.deleted)
        add("unchanged", done.unchanged)
        add("checkpoints_moved", int(done.checkpoint_moved))
        add("attribution_changed", changed)

    run = current_run()
    if run is not None:
        for name in (
            "windows",
            "windows_complete",
            "responses",
            "records",
            "superseded",
            "kept_deleted",
            "expired",
            "forgotten",
            "attribution_changed",
        ):
            run.count(f"replay_{name}", counts.get(name, 0))
    return replace(result, **counts)
