"""The raw response store: windows, their calls and the bytes of each result, and the deletions
retention and forget need. See :mod:`memini.responses` for the rules."""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, cast, get_args
from uuid import UUID

from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from memini.ids import RecordKind, hash_fields, record_id
from memini.ingest.model import InvalidBatchError
from memini.ingest.validate import canonical_json
from memini.intake import PlannedWindow, WindowKind
from memini.recipes.parsing import CallKind, ParsedWindow, RawResponse
from memini.redaction import (
    RedactionCollisionError,
    RedactionKind,
    add_counts,
    redact_json,
    redact_text,
)
from memini.runs import current_run
from memini.workspaces import NotFoundError

__all__ = [
    "CALL_KEY_DOMAIN",
    "MAX_RESPONSE_SIZE",
    "InvalidResponseError",
    "NotAnMcpSourceError",
    "RawDeletion",
    "RawWindow",
    "ResponseTooLargeError",
    "ServerInfo",
    "StoredResponse",
    "WindowClosedError",
    "WindowNotFoundError",
    "begin_window",
    "call_key",
    "delete_raw_responses_before",
    "delete_raw_responses_for_records",
    "get_window",
    "load_window",
    "record_parse",
    "store_response",
    "stored_record_ids",
]

type Conn = AsyncConnection[TupleRow]

MAX_RESPONSE_SIZE: Final = 16 * 1024**2
"""Bytes of one stored result, redacted, as canonical JSON. A larger one is refused."""

CALL_KEY_DOMAIN: Final = "memini.raw-call.v1"
"""Domain tag of a call's identity in its window (:func:`memini.ids.hash_fields`)."""

_CALL_KINDS: Final[frozenset[str]] = frozenset(get_args(CallKind))
_WINDOW_KINDS: Final[frozenset[str]] = frozenset(get_args(WindowKind))
_MAX_TOOL_LENGTH: Final = 128
_MAX_ITEM_ID_BYTES: Final = 1024


# --- Values --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """What the server reported when the session started (``serverInfo``)."""

    name: str
    version: str | None = None


@dataclass(frozen=True, slots=True)
class RawWindow:
    """A window of a stream that an executor ran, with the names ingest needs."""

    id: UUID
    workspace_id: UUID
    owner_id: UUID
    source_id: UUID
    stream_id: UUID
    workspace: str
    source: str
    stream: str
    """The stream key."""
    run_id: UUID | None
    kind: WindowKind
    start: datetime
    end: datetime
    recipe_digest: str
    server: ServerInfo
    started_at: datetime
    parsed_at: datetime | None
    """When its responses were first parsed and its records sent to ingest; ``None`` before."""
    complete: bool | None
    """What the last parse found; ``None`` before the first."""
    pruned_at: datetime | None
    """When forget first removed the body of one of its responses."""

    @property
    def planned(self) -> PlannedWindow:
        """The window as the planner made it, for its ingest claim."""
        return PlannedWindow(kind=self.kind, start=self.start, end=self.end)


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """What :func:`store_response` did."""

    id: UUID
    position: int
    """The call's place in its window, from 1."""
    content_hash: str
    """SHA-256 of the stored bytes: the result, redacted, as canonical JSON."""
    stored: bool
    """False when the window held the same call with the same result: nothing was written."""
    body_stored: bool
    """False when the source held these bytes already, from another call or another window."""
    redactions: Mapping[RedactionKind, int] = field(default_factory=dict[RedactionKind, int])
    """Values redacted in the result and the arguments, by kind: counts, never values."""


@dataclass(frozen=True, slots=True)
class RawDeletion:
    """What a deletion removed."""

    windows: int = 0
    """Windows deleted, with all their responses."""
    responses: int = 0
    """Responses deleted with their windows."""
    pruned: int = 0
    """Responses whose body was removed (forget); the call itself stays."""
    bodies: int = 0
    """Bodies no response referenced any more."""

    def __add__(self, other: "RawDeletion") -> "RawDeletion":
        return RawDeletion(
            windows=self.windows + other.windows,
            responses=self.responses + other.responses,
            pruned=self.pruned + other.pruned,
            bodies=self.bodies + other.bodies,
        )


# --- Errors --------------------------------------------------------------------------------------


class InvalidResponseError(ValueError):
    """A call or its result cannot be stored. ``field`` names where (``result``,
    ``arguments``) and ``reason`` why; neither holds a value of the response."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class ResponseTooLargeError(InvalidResponseError):
    """The result, redacted and as canonical JSON, is larger than the limit."""


class NotAnMcpSourceError(Exception):
    """Raw responses belong to the windows of an mcp source; this source is of another kind."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"raw responses belong to mcp sources, not to {kind} sources")
        self.kind = kind


class WindowNotFoundError(LookupError):
    """No such window: never begun, or deleted by retention, by forget (a window never parsed)
    or by a later run of the same window."""


class WindowClosedError(Exception):
    """The window was parsed: its calls are what the parser read, and no call is added to it."""


# --- Windows -------------------------------------------------------------------------------------

_WINDOW_COLUMNS: Final = """
w.id, w.workspace_id, w.owner_id, w.source_id, w.stream_id, ws.name, s.name, st.key, w.run_id,
w.kind, w.window_from, w.window_to, w.recipe_digest, w.server_name, w.server_version,
w.started_at, w.parsed_at, w.complete, w.pruned_at
"""

_GET_WINDOW: Final = (
    "SELECT"
    + _WINDOW_COLUMNS
    + """
FROM raw_window w
JOIN workspace ws ON ws.id = w.workspace_id
JOIN source s ON s.id = w.source_id
JOIN stream st ON st.id = w.stream_id
WHERE w.id = %s
"""
)


def _window(row: Sequence[Any]) -> RawWindow:
    return RawWindow(
        id=row[0],
        workspace_id=row[1],
        owner_id=row[2],
        source_id=row[3],
        stream_id=row[4],
        workspace=row[5],
        source=row[6],
        stream=row[7],
        run_id=row[8],
        kind=row[9],
        start=row[10],
        end=row[11],
        recipe_digest=row[12],
        server=ServerInfo(name=row[13], version=row[14]),
        started_at=row[15],
        parsed_at=row[16],
        complete=row[17],
        pruned_at=row[18],
    )


async def get_window(conn: Conn, window_id: UUID) -> RawWindow:
    """The window ``window_id``.

    Raises:
        WindowNotFoundError: no such window.
    """
    async with conn.transaction():
        cur = await conn.execute(_GET_WINDOW, (window_id,))
        row = await cur.fetchone()
    if row is None:
        raise WindowNotFoundError(f"no raw window {window_id}")
    return _window(row)


async def begin_window(
    conn: Conn,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    source_id: UUID,
    stream_id: UUID,
    window: PlannedWindow,
    recipe_digest: str,
    server: ServerInfo,
    run_id: UUID | None = None,
) -> RawWindow:
    """Start storing the calls of ``window`` of the stream ``stream_id``: before its first call.

    ``recipe_digest`` is :func:`memini.recipes.recipe_digest` of the recipe the calls are
    planned from, ``server`` what the session's server reported, ``run_id`` the executor's run.
    Commits on its own (a savepoint inside the caller's transaction).

    Raises:
        NotFoundError: no such stream of the source in the workspace.
        NotAnMcpSourceError: the source is not an mcp source.
        ValueError: a window without zones, or not from before to, a digest that is not 64
            lowercase hex digits, a server without a name.
    """
    if window.kind not in _WINDOW_KINDS:
        raise ValueError(f"a window kind is one of {sorted(_WINDOW_KINDS)}")
    for moment in (window.start, window.end):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("a window's bounds have a zone")
    if window.start >= window.end:
        raise ValueError("a window starts before it ends")
    if len(recipe_digest) != 64 or any(c not in "0123456789abcdef" for c in recipe_digest):
        raise ValueError("a recipe digest is 64 lowercase hex digits")
    if not server.name:
        raise ValueError("a server has a name")
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT s.kind FROM stream st JOIN source s ON s.id = st.source_id"
            " WHERE st.id = %s AND s.id = %s AND s.workspace_id = %s",
            (stream_id, source_id, workspace_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError("stream")
        if row[0] != "mcp":
            raise NotAnMcpSourceError(row[0])
        cur = await conn.execute(
            "INSERT INTO raw_window (workspace_id, owner_id, source_id, stream_id, run_id, kind,"
            " window_from, window_to, recipe_digest, server_name, server_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                workspace_id,
                owner_id,
                source_id,
                stream_id,
                run_id,
                window.kind,
                window.start,
                window.end,
                recipe_digest,
                server.name,
                server.version,
            ),
        )
        row = await cur.fetchone()
        if row is None:  # pragma: no cover - INSERT … RETURNING returns the row
            raise RuntimeError("the window row was not returned")
        cur = await conn.execute(_GET_WINDOW, (row[0],))
        found = await cur.fetchone()
    if found is None:  # pragma: no cover - inserted just now
        raise RuntimeError("the window row vanished")
    return _window(found)


# --- Calls ---------------------------------------------------------------------------------------


def _redacted_json(
    value: object, field: str
) -> tuple[dict[str, Any], bytes, dict[RedactionKind, int]]:
    """``value`` (a JSON object) with its secrets redacted, its canonical JSON, and the counts."""
    if not isinstance(value, Mapping):
        raise InvalidResponseError(field, "a JSON object")
    mapping = cast(Mapping[str, Any], value)
    try:
        canonical_json(mapping, field=field)  # refuses what is not JSON before redaction
        redacted = redact_json(mapping)
        data = canonical_json(redacted.value, field=field)
    except InvalidBatchError as error:
        raise InvalidResponseError(error.field, error.reason) from None
    except RedactionCollisionError:
        raise InvalidResponseError(
            field, "two keys of an object are the same once secrets are redacted from them"
        ) from None
    return redacted.value, data, dict(redacted.counts)


def _check_call(call: str, tool: str, page: int, item_id: str | None) -> None:
    if call not in _CALL_KINDS:
        raise InvalidResponseError("call", "list or detail")
    if not tool or len(tool) > _MAX_TOOL_LENGTH or not tool.isprintable():
        raise InvalidResponseError("tool", f"1 to {_MAX_TOOL_LENGTH} printable characters")
    if isinstance(page, bool) or page < 0:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise InvalidResponseError("page", "a whole number of at least 0")
    if (call == "detail") != (item_id is not None):
        raise InvalidResponseError("item_id", "set for a detail call, and only for one")
    if item_id is not None and (
        not item_id or "\0" in item_id or len(item_id.encode()) > _MAX_ITEM_ID_BYTES
    ):
        raise InvalidResponseError(
            "item_id", f"1 to {_MAX_ITEM_ID_BYTES} bytes in UTF-8, without NUL characters"
        )


def call_key(call: CallKind, tool: str, arguments: bytes) -> str:
    """The identity of a call in its window: a hash of its kind, its tool and the canonical JSON
    of its arguments as stored."""
    return hash_fields(CALL_KEY_DOMAIN, call.encode(), tool.encode(), arguments)


async def store_response(
    conn: Conn,
    window_id: UUID,
    *,
    call: CallKind,
    tool: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    page: int = 0,
    item_id: str | None = None,
    max_size: int = MAX_RESPONSE_SIZE,
) -> StoredResponse:
    """Store one call of the window ``window_id`` and its result, before anything parses it.

    ``result`` is the MCP tool result as the server returned it, a JSON object: ``content``
    blocks, ``structuredContent``, ``isError`` and whatever else it holds. Its secrets and card
    numbers are redacted (:func:`memini.redaction.redact_json`: every string, and every value
    under a key that names a secret), and nothing else is changed; it is stored as canonical
    JSON, hashed, and its bytes kept once per source. The arguments are redacted the same way, and
    the listed item's id as text. A call already in the window (the same call kind, tool and
    arguments) with the same result writes nothing; with another result (a retry) its result is
    replaced and its attribution cleared. Counts go to the current run (:func:`memini.runs.
    current_run`), if any: ``raw_responses_stored``, ``raw_responses_unchanged``,
    ``raw_bodies_stored`` and ``raw_redacted_<kind>``.

    Commits on its own (a savepoint inside the caller's transaction).

    Raises:
        InvalidResponseError: a call, a tool, a page or an item id that is not valid, a result or
            arguments that are not a JSON object; :class:`ResponseTooLargeError` for a result
            larger than ``max_size`` bytes as stored.
        WindowNotFoundError: no such window (deleted by retention, by forget, or by a later run
            of the same window).
        WindowClosedError: the window was parsed already.
    """
    _check_call(call, tool, page, item_id)
    stored_arguments, argument_bytes, found_in_arguments = _redacted_json(arguments, "arguments")
    stored_result, body, found_in_result = _redacted_json(result, "result")
    if len(body) > max_size:
        raise ResponseTooLargeError("result", f"at most {max_size} bytes as stored")
    redactions = add_counts(found_in_arguments, found_in_result)
    if item_id is not None:
        redacted_item = redact_text(item_id)
        item_id = redacted_item.value
        redactions = add_counts(redactions, redacted_item.counts)
    content_hash = hashlib.sha256(body).hexdigest()
    key = call_key(call, tool, argument_bytes)
    is_error = stored_result.get("isError") is True

    async with conn.transaction():
        cur = await conn.execute(
            "SELECT workspace_id, owner_id, source_id, parsed_at FROM raw_window WHERE id = %s"
            " FOR NO KEY UPDATE",
            (window_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise WindowNotFoundError(f"no raw window {window_id}")
        workspace_id, owner_id, source_id, parsed_at = row
        if parsed_at is not None:
            raise WindowClosedError("the window was parsed; no call is added to it")

        body_stored = False
        cur = await conn.execute(
            "SELECT 1 FROM raw_body WHERE source_id = %s AND hash = %s", (source_id, content_hash)
        )
        if await cur.fetchone() is None:  # the bytes go over the wire only when they are new
            cur = await conn.execute(
                "INSERT INTO raw_body (workspace_id, owner_id, source_id, hash, body)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (source_id, hash) DO NOTHING"
                " RETURNING hash",
                (workspace_id, owner_id, source_id, content_hash, body),
            )
            body_stored = await cur.fetchone() is not None

        cur = await conn.execute(
            "SELECT id, seq, body_hash FROM raw_response WHERE window_id = %s AND call_key = %s",
            (window_id, key),
        )
        existing = await cur.fetchone()
        stored = True
        if existing is None:
            cur = await conn.execute(
                "INSERT INTO raw_response (workspace_id, owner_id, source_id, window_id, seq,"
                " call, tool, arguments, call_key, page, item_id, body_hash, is_error)"
                " SELECT %s, %s, %s, %s, coalesce(max(seq), 0) + 1, %s, %s, %s, %s, %s, %s, %s, %s"
                " FROM raw_response WHERE window_id = %s"
                " RETURNING id, seq",
                (
                    workspace_id,
                    owner_id,
                    source_id,
                    window_id,
                    call,
                    tool,
                    Jsonb(stored_arguments),
                    key,
                    page,
                    item_id,
                    content_hash,
                    is_error,
                    window_id,
                ),
            )
            inserted = await cur.fetchone()
            if inserted is None:  # pragma: no cover - INSERT … RETURNING returns the row
                raise RuntimeError("the response row was not returned")
            response_id, position = inserted
        else:
            response_id, position, old_hash = existing
            if old_hash == content_hash:
                stored = False
            else:
                # A retry of the call, or a body forget removed: the newer result replaces it.
                await conn.execute(
                    "UPDATE raw_response SET body_hash = %s, is_error = %s, received_at = now(),"
                    " page = %s, item_id = %s, record_ids = NULL WHERE id = %s",
                    (content_hash, is_error, page, item_id, response_id),
                )
                if old_hash is not None:
                    await _delete_orphan_bodies(conn, source_id, [old_hash])

    run = current_run()
    if run is not None:
        run.count("raw_responses_stored" if stored else "raw_responses_unchanged")
        if body_stored:
            run.count("raw_bodies_stored")
        for kind, count in sorted(redactions.items()):
            run.count(f"raw_redacted_{kind}", count)
    return StoredResponse(
        id=response_id,
        position=position,
        content_hash=content_hash,
        stored=stored,
        body_stored=body_stored,
        redactions=redactions,
    )


async def load_window(conn: Conn, window_id: UUID) -> tuple[RawWindow, tuple[RawResponse, ...]]:
    """The window and its responses in the order the calls were made, as a parser reads them.
    A response whose body forget removed is left out.

    Raises:
        WindowNotFoundError: no such window.
    """
    async with conn.transaction():
        window = await get_window(conn, window_id)
        cur = await conn.execute(
            "SELECT r.seq, r.call, r.tool, r.arguments, r.page, r.item_id, b.body"
            " FROM raw_response r"
            " JOIN raw_body b ON b.source_id = r.source_id AND b.hash = r.body_hash"
            " WHERE r.window_id = %s ORDER BY r.seq",
            (window_id,),
        )
        rows = await cur.fetchall()
    responses = tuple(
        RawResponse(
            position=row[0],
            call=row[1],
            tool=row[2],
            arguments=row[3],
            page=row[4],
            item_id=row[5],
            result=json.loads(bytes(row[6])),
        )
        for row in rows
    )
    return window, responses


async def stored_record_ids(conn: Conn, window_id: UUID) -> set[str]:
    """The ids of the records the window's responses were built into at its last parse, the
    responses whose body forget removed included."""
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT DISTINCT unnest(record_ids) FROM raw_response WHERE window_id = %s",
            (window_id,),
        )
        rows = await cur.fetchall()
    return {row[0] for row in rows}


# --- After the parser ----------------------------------------------------------------------------

_ATTRIBUTE: Final = """
UPDATE raw_response r SET record_ids = u.ids
FROM (SELECT x.seq, ARRAY(SELECT jsonb_array_elements_text(x.ids)) AS ids
      FROM jsonb_to_recordset(%(rows)s) AS x (seq integer, ids jsonb)) AS u
WHERE r.window_id = %(window)s AND r.seq = u.seq AND r.record_ids IS DISTINCT FROM u.ids
RETURNING r.seq
"""

# On a window's first parse: the stream's earlier windows that were never parsed (their run died),
# and its earlier windows with the same kind and bounds (an earlier attempt of the same window).
_SUPERSEDED: Final = """
SELECT id FROM raw_window
WHERE stream_id = %(stream)s AND id <> %(id)s AND (started_at, id) < (%(started)s, %(id)s)
  AND (parsed_at IS NULL
       OR (kind = %(kind)s AND window_from = %(from)s AND window_to = %(to)s))
"""


async def record_parse(
    conn: Conn,
    window: RawWindow,
    parsed: ParsedWindow,
    responses: Sequence[RawResponse],
    *,
    complete: bool,
) -> int:
    """Record what a parse of the window found, before its records go to ingest.

    Each of ``responses`` (those the parser read) gets the ids of the records whose
    :attr:`~memini.recipes.parsing.ParsedRecord.responses` name it; the window gets ``complete``
    (the claim its records go with). Rows that already hold these values are not written, so a
    parse that found what the last one found writes nothing. On the window's first parse, the
    stream's earlier windows that were never parsed (their run died) and its earlier attempts at
    the same window (same kind and bounds) are deleted. Returns the number of responses whose
    attribution changed. Commits on its own (a savepoint inside the caller's transaction): the
    attribution is stored before the records, so forget finds every response of a stored record.
    """
    ids: dict[int, set[str]] = {response.position: set() for response in responses}
    for item in parsed.records:
        kind = cast(RecordKind, item.record.kind)  # checked by parse_window
        rid = record_id(window.source_id, kind, item.record.external_id)
        for position in item.responses:
            if position in ids:
                ids[position].add(rid)
    rows = [{"seq": position, "ids": sorted(found)} for position, found in sorted(ids.items())]
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT parsed_at, started_at FROM raw_window WHERE id = %s FOR NO KEY UPDATE",
            (window.id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise WindowNotFoundError(f"no raw window {window.id}")
        first_parse = row[0] is None
        changed = 0
        if rows:
            cur = await conn.execute(_ATTRIBUTE, {"rows": Jsonb(rows), "window": window.id})
            changed = len(await cur.fetchall())
        await conn.execute(
            "UPDATE raw_window SET complete = %s, parsed_at = coalesce(parsed_at, now())"
            " WHERE id = %s AND (parsed_at IS NULL OR complete IS DISTINCT FROM %s)",
            (complete, window.id, complete),
        )
        if first_parse:
            cur = await conn.execute(
                _SUPERSEDED,
                {
                    "stream": window.stream_id,
                    "id": window.id,
                    "started": row[1],
                    "kind": window.kind,
                    "from": window.start,
                    "to": window.end,
                },
            )
            superseded = [found[0] for found in await cur.fetchall()]
            deleted = await _delete_windows(conn, window.source_id, superseded)
            _count_deletion(deleted, "superseded")
    return changed


# --- Deletions -----------------------------------------------------------------------------------


_DELETE_ORPHANS: Final = (
    "DELETE FROM raw_body b WHERE b.source_id = %s AND b.hash = ANY(%s::text[])"
    " AND NOT EXISTS (SELECT 1 FROM raw_response r"
    " WHERE r.source_id = b.source_id AND r.body_hash = b.hash)"
)


async def _delete_orphan_bodies(conn: Conn, source_id: UUID, hashes: Iterable[str | None]) -> int:
    """Delete the bodies among ``hashes`` that no response of the source references.

    A body that a concurrent call references again makes the delete fail on the foreign key at
    the end of the statement; then each body is tried on its own, and the referenced ones stay.
    """
    wanted = sorted({h for h in hashes if h is not None})
    if not wanted:
        return 0
    try:
        async with conn.transaction():
            cur = await conn.execute(_DELETE_ORPHANS, (source_id, wanted))
            return cur.rowcount
    except errors.ForeignKeyViolation:
        deleted = 0
        for one in wanted:
            try:
                async with conn.transaction():
                    cur = await conn.execute(_DELETE_ORPHANS, (source_id, [one]))
                    deleted += cur.rowcount
            except errors.ForeignKeyViolation:
                continue  # referenced again meanwhile: it stays
        return deleted


async def _delete_windows(conn: Conn, source_id: UUID, window_ids: Sequence[UUID]) -> RawDeletion:
    if not window_ids:
        return RawDeletion()
    cur = await conn.execute(
        "SELECT count(*), coalesce(array_agg(DISTINCT body_hash)"
        " FILTER (WHERE body_hash IS NOT NULL), '{}')"
        " FROM raw_response WHERE window_id = ANY(%s::uuid[])",
        (list(window_ids),),
    )
    row = await cur.fetchone()
    responses: int = 0 if row is None else row[0]
    hashes: list[str] = [] if row is None else row[1]
    cur = await conn.execute(
        "DELETE FROM raw_window WHERE source_id = %s AND id = ANY(%s::uuid[])",
        (source_id, list(window_ids)),
    )
    windows = cur.rowcount
    bodies = await _delete_orphan_bodies(conn, source_id, hashes)
    return RawDeletion(windows=windows, responses=responses, bodies=bodies)


def _count_deletion(deleted: RawDeletion, why: str) -> None:
    run = current_run()
    if run is None or deleted == RawDeletion():
        return
    run.count(f"raw_windows_{why}", deleted.windows)
    run.count(f"raw_responses_{why}", deleted.responses + deleted.pruned)
    run.count(f"raw_bodies_{why}", deleted.bodies)


async def delete_raw_responses_before(
    conn: Conn, *, source_id: UUID, before: datetime
) -> RawDeletion:
    """Retention: delete the windows of the source that ended at or before ``before``, with their
    responses, and the bodies no response references any more.

    Every item such a window listed is older than ``before``, so ``before`` is the cutoff of the
    source's retention (the time ``retention_days`` back). A record built from them may outlive
    them (a thread whose replies go on); it is then no longer replayed. Counts go to the current
    run, if any (``raw_windows_expired``, ``raw_responses_expired``, ``raw_bodies_expired``).
    Commits on its own (a savepoint inside the caller's transaction).
    """
    if before.tzinfo is None or before.utcoffset() is None:
        raise ValueError("before has a zone")
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT id FROM raw_window WHERE source_id = %s AND window_to <= %s ORDER BY id"
            " FOR UPDATE",
            (source_id, before),
        )
        ids = [row[0] for row in await cur.fetchall()]
        deleted = await _delete_windows(conn, source_id, ids)
    _count_deletion(deleted, "expired")
    return deleted


async def delete_raw_responses_for_records(
    conn: Conn, *, source_id: UUID, record_ids: Sequence[str]
) -> RawDeletion:
    """Forget: remove every stored response that holds content of the records ``record_ids`` of
    the source ``source_id``.

    The responses whose attribution (:func:`record_parse`) names one of the records lose their
    body; their calls stay, their windows are marked pruned (never claimed complete again, and
    replay never inserts a record from them), and the bodies no response references any more are
    deleted. A window that was never parsed has no attribution, so every such window of the
    source is deleted whole: a run that died, or one whose calls are being made (its executor
    then finds the window gone, and the window is fetched again by a later run). Call it in the
    transaction that deletes the records. Counts go to the current run, if any
    (``raw_windows_forgotten``, ``raw_responses_forgotten``, ``raw_bodies_forgotten``).
    """
    ids = sorted(set(record_ids))
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE raw_response SET body_hash = NULL"
            " WHERE source_id = %s AND record_ids && %s::text[] AND body_hash IS NOT NULL"
            " RETURNING window_id, old.body_hash",
            (source_id, ids),
        )
        pruned = await cur.fetchall()
        windows = sorted({row[0] for row in pruned})
        if windows:
            await conn.execute(
                "UPDATE raw_window SET pruned_at = now()"
                " WHERE id = ANY(%s::uuid[]) AND pruned_at IS NULL",
                (windows,),
            )
        bodies = await _delete_orphan_bodies(conn, source_id, [row[1] for row in pruned])
        cur = await conn.execute(
            "SELECT id FROM raw_window WHERE source_id = %s AND parsed_at IS NULL ORDER BY id"
            " FOR UPDATE",
            (source_id,),
        )
        unparsed = await _delete_windows(conn, source_id, [row[0] for row in await cur.fetchall()])
    deleted = RawDeletion(pruned=len(pruned), bodies=bodies) + unparsed
    _count_deletion(deleted, "forgotten")
    return deleted
