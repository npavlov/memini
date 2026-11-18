"""Shared by tests that break things on purpose: does the base still hold together?

:func:`consistency_problems` reads a database (one workspace, or all of them) and lists every place
where derived or bookkeeping data disagrees with the raw records, as one line each, naming record
ids and external ids (synthetic in every test), never content. :func:`assert_consistent` fails a
test with that list. What is checked:

- **Records and their index.** A tombstone has no chunks and no symbols. A live record has exactly
  the chunks and symbols its raw content gives: read as ``memini reindex`` reads it
  (:func:`memini.index.default_raw_content`, files of git sources from their mirror) and cut by
  the chunker of its kind (:data:`memini.index.DEFAULT_CHUNKERS`); each chunk's text hash is that
  of its redacted text, as the index writer stores it. A record of a workspace that has no index
  yet has no chunks. A stored payload hashes to the record's content hash and normalizes to its
  ``normalized_text``.
- **Vectors.** Every chunk carries its workspace's embedding key, and all chunks of one text in a
  workspace carry one vector.
- **Checkpoints and their data** (principle 7). A git stream pinned at a commit, and not marked
  partial, holds exactly the files that commit selects: a live record of the right kind with the
  file's blob id for each, and no other live record of the ref; a ref marked missing holds no live
  record. An mcp stream's checkpoint was moved by a complete window that ends there, and every
  record that window's parse attributed is stored (or was forgotten, or expired).
- **Orphans.** No raw body is left that no raw response references.

:func:`advisory_locks` lists the advisory locks held in the database: after a failure, none may be
left behind (the per-source job lock goes with its session).
"""

import json
from dataclasses import dataclass
from typing import Any, LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.chunking import FileChunks, chunk_file, chunk_markdown
from memini.embeddings import text_hash
from memini.git import MirrorManager
from memini.gitsync import (
    FileRules,
    MirrorContent,
    file_kind,
    file_record_id,
    read_pin,
    ref_missing,
)
from memini.index import DEFAULT_CHUNKERS, RecordRef, default_raw_content
from memini.ingest import content_hash, normalize
from memini.redaction import redact_text
from memini.sources import GitConfig

__all__ = ["advisory_locks", "assert_consistent", "consistency_problems"]

type Conn = AsyncConnection[TupleRow]


async def _rows(conn: Conn, query: LiteralString, *params: object) -> list[tuple[Any, ...]]:
    cur = await conn.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


@dataclass(frozen=True, slots=True)
class _Expected:
    chunks: dict[str, str]
    """chunk id -> text hash."""
    symbols: set[tuple[str, int, str]]
    """(qualified name, ordinal, chunk id)."""


def _expected(chunks: FileChunks | None) -> _Expected:
    if chunks is None:
        return _Expected({}, set())
    hashes = {
        chunk.id: text_hash(
            f"{redact_text(chunk.header).value}\n{redact_text(chunk.content).value}"
        )
        for chunk in chunks.chunks
    }
    symbols = {
        (symbol.qualified_name, symbol.ordinal, symbol.chunk_id) for symbol in chunks.symbols
    }
    return _Expected(hashes, symbols)


async def _record_problems(
    conn: Conn, workspace_id: UUID | None, mirrors: MirrorManager | None
) -> list[str]:
    problems: list[str] = []
    indexed = {row[0] for row in await _rows(conn, "SELECT workspace_id FROM index_state")}
    raw = default_raw_content(conn, code=None if mirrors is None else MirrorContent(mirrors))
    records = await _rows(
        conn,
        "SELECT id, workspace_id, owner_id, source_id, kind, external_id, content_hash,"
        " deleted_at IS NOT NULL, payload, normalized_text FROM record"
        " WHERE %s::uuid IS NULL OR workspace_id = %s ORDER BY id",
        workspace_id,
        workspace_id,
    )
    for row in records:
        ref = RecordRef(
            id=row[0],
            workspace_id=row[1],
            owner_id=row[2],
            source_id=row[3],
            kind=row[4],
            external_id=row[5],
            content_hash=row[6],
            deleted=row[7],
        )
        payload: bytes | None = None if row[8] is None else bytes(row[8])
        name = f"{ref.kind} {ref.external_id} ({ref.id[:12]})"
        stored_chunks = {
            found[0]: found[1]
            for found in await _rows(
                conn, "SELECT id, text_hash FROM chunk WHERE record_id = %s", ref.id
            )
        }
        stored_symbols = {
            (found[0], found[1], found[2])
            for found in await _rows(
                conn,
                "SELECT qualified_name, ordinal, chunk_id FROM symbol WHERE record_id = %s",
                ref.id,
            )
        }
        if ref.deleted:
            if stored_chunks or stored_symbols:
                problems.append(f"{name}: a tombstone with chunks or symbols")
            continue
        if payload is not None:
            if content_hash(payload) != ref.content_hash:
                problems.append(f"{name}: the content hash is not that of the stored payload")
            if normalize(ref.kind, json.loads(payload)) != row[9]:
                problems.append(f"{name}: the normalized text is not that of the payload")
        if ref.workspace_id not in indexed:
            if stored_chunks or stored_symbols:
                problems.append(f"{name}: chunks in a workspace that has no index")
            continue
        document = await raw.read(ref)
        expected = _expected(
            None if document is None else DEFAULT_CHUNKERS[ref.kind](ref, document)
        )
        if stored_chunks != expected.chunks:
            missing = len(expected.chunks.keys() - stored_chunks.keys())
            extra = len(stored_chunks.keys() - expected.chunks.keys())
            changed = sum(
                1
                for chunk_id, hashed in expected.chunks.items()
                if chunk_id in stored_chunks and stored_chunks[chunk_id] != hashed
            )
            problems.append(
                f"{name}: its chunks do not match its content"
                f" ({missing} missing, {extra} extra, {changed} with another text)"
            )
        if stored_symbols != expected.symbols:
            problems.append(f"{name}: its symbols do not match its content")
    return problems


async def _vector_problems(conn: Conn, workspace_id: UUID | None) -> list[str]:
    problems: list[str] = []
    for chunk_id, workspace in await _rows(
        conn,
        "SELECT c.id, c.workspace_id FROM chunk c"
        " LEFT JOIN index_state s ON s.workspace_id = c.workspace_id"
        " WHERE (%s::uuid IS NULL OR c.workspace_id = %s)"
        " AND s.embedding_key IS DISTINCT FROM c.embedding_key",
        workspace_id,
        workspace_id,
    ):
        problems.append(f"chunk {chunk_id[:12]} of workspace {workspace}: another embedding key")
    for hashed, count in await _rows(
        conn,
        "SELECT text_hash, count(DISTINCT embedding::text) FROM chunk"
        " WHERE %s::uuid IS NULL OR workspace_id = %s"
        " GROUP BY workspace_id, embedding_key, text_hash"
        " HAVING count(DISTINCT embedding::text) > 1",
        workspace_id,
        workspace_id,
    ):
        problems.append(f"text {hashed[:12]}: {count} different vectors")
    return problems


def _chunk_file(kind: str, record_id: str, path: str, content: bytes) -> FileChunks:
    if kind == "document":
        return chunk_markdown(record_id, path, content)
    return chunk_file(record_id, path, content)


async def _git_problems(
    conn: Conn,
    mirrors: MirrorManager | None,
    stream: tuple[Any, ...],
) -> list[str]:
    """A git stream: its pin against the live records of its ref."""
    stream_id, key, cursor, checked_until, source_id, config = stream
    name = f"git stream {key} ({str(stream_id)[:8]})"
    live = {
        row[0]: (row[1], row[2])
        for row in await _rows(
            conn,
            "SELECT id, content_hash, kind FROM record WHERE source_id = %s"
            " AND kind IN ('code_file', 'document') AND starts_with(external_id, %s)"
            " AND deleted_at IS NULL",
            source_id,
            f"{key}:",
        )
    }
    if ref_missing(cursor):
        return [f"{name}: marked missing, with {len(live)} live records"] if live else []
    pin = read_pin(cursor)
    if pin is None:
        return []
    problems: list[str] = []
    if checked_until is None:
        problems.append(f"{name}: pinned, with no checked_until")
    if pin.partial:
        return problems  # records may be past the pin; the next sync lists the ref in full
    if mirrors is None:
        raise ValueError("the git streams need the mirrors to be checked: pass mirrors")
    mirror = mirrors.open(source_id)
    rules = FileRules(GitConfig.from_json(config), mirrors.options.max_blob_size)
    if pin.rules != rules.digest:
        problems.append(f"{name}: pinned under other rules than the source's")
    selected: dict[str, tuple[str, str, str]] = {}
    for entry in await mirrors.ls_tree(mirror, pin.commit):
        if rules.skip_reason(entry.path, entry.mode, entry.size) is not None:
            continue  # not a file the rules select (a path that is not UTF-8 among them)
        rid = file_record_id(source_id, key, entry.path)
        selected[rid] = (entry.oid, file_kind(entry.path), entry.path)
    forgotten = {
        row[0]
        for row in await _rows(
            conn,
            "SELECT record_id FROM forgotten_record WHERE record_id = ANY(%s::text[])",
            list(selected),
        )
    }
    blobs = await mirrors.read_blobs(mirror, sorted({oid for oid, _, _ in selected.values()}))
    expected: dict[str, tuple[str, str]] = {}
    for rid, (oid, kind, path) in selected.items():
        if rid in forgotten:
            continue
        if _chunk_file(kind, rid, path, blobs[oid]).fallback == "binary":
            continue  # found binary when it was read: never stored
        expected[rid] = (oid, kind)
    if live != expected:
        missing = len(expected.keys() - live.keys())
        extra = len(live.keys() - expected.keys())
        other = sum(1 for rid in expected.keys() & live.keys() if live[rid] != expected[rid])
        problems.append(
            f"{name}: pinned at {pin.commit[:12]}, but its live records are not the files of that"
            f" commit ({missing} missing, {extra} extra, {other} at another blob or kind)"
        )
    return problems


async def _window_problems(conn: Conn, stream: tuple[Any, ...]) -> list[str]:
    """An mcp stream: the window that moved its checkpoint, and that window's records."""
    stream_id, key, _, checked_until, _, _ = stream
    name = f"mcp stream {key} ({str(stream_id)[:8]})"
    windows = await _rows(conn, "SELECT count(*) FROM raw_window WHERE stream_id = %s", stream_id)
    if checked_until is None or not windows[0][0]:
        return []
    moved = await _rows(
        conn,
        "SELECT id, pruned_at IS NOT NULL FROM raw_window WHERE stream_id = %s AND complete"
        " AND window_to = %s ORDER BY started_at DESC LIMIT 1",
        stream_id,
        checked_until,
    )
    if not moved:
        return [f"{name}: its checkpoint {checked_until} ends no complete window"]
    window_id, pruned = moved[0]
    if pruned:
        return []
    attributed = await _rows(
        conn,
        "SELECT DISTINCT unnest(record_ids) FROM raw_response WHERE window_id = %s",
        window_id,
    )
    ids = [row[0] for row in attributed]
    accounted = await _rows(
        conn,
        "SELECT id FROM record WHERE id = ANY(%s::text[])"
        " UNION SELECT record_id FROM forgotten_record WHERE record_id = ANY(%s::text[])"
        " UNION SELECT record_id FROM expired_record WHERE record_id = ANY(%s::text[])",
        ids,
        ids,
        ids,
    )
    absent = set(ids) - {row[0] for row in accounted}
    if absent:
        return [
            f"{name}: its checkpoint moved with window {str(window_id)[:8]}, but {len(absent)}"
            " records of that window are not stored"
        ]
    return []


async def _stream_problems(
    conn: Conn, workspace_id: UUID | None, mirrors: MirrorManager | None
) -> list[str]:
    problems: list[str] = []
    streams = await _rows(
        conn,
        "SELECT st.id, st.key, st.cursor, st.checked_until, st.source_id, so.config, so.kind,"
        " st.checked_from FROM stream st JOIN source so ON so.id = st.source_id"
        " WHERE %s::uuid IS NULL OR st.workspace_id = %s ORDER BY st.key",
        workspace_id,
        workspace_id,
    )
    for row in streams:
        *stream, kind, checked_from = row
        if checked_from is not None and (row[3] is None or checked_from > row[3]):
            problems.append(f"stream {row[1]}: its frontier is after its checkpoint")
        if kind == "git":
            problems += await _git_problems(conn, mirrors, tuple(stream))
        elif kind == "mcp":
            problems += await _window_problems(conn, tuple(stream))
    return problems


async def _orphan_problems(conn: Conn, workspace_id: UUID | None) -> list[str]:
    orphans = await _rows(
        conn,
        "SELECT count(*) FROM raw_body b WHERE (%s::uuid IS NULL OR b.workspace_id = %s)"
        " AND NOT EXISTS (SELECT 1 FROM raw_response r"
        " WHERE r.source_id = b.source_id AND r.body_hash = b.hash)",
        workspace_id,
        workspace_id,
    )
    count = orphans[0][0]
    return [f"{count} raw bodies that no raw response references"] if count else []


async def consistency_problems(
    conn: Conn, *, workspace_id: UUID | None = None, mirrors: MirrorManager | None = None
) -> list[str]:
    """Every inconsistency of the workspace (or of the whole database); see the module.

    ``conn`` is a connection in autocommit mode with nothing else to do; it is only read.
    ``mirrors`` reads the files of git sources; needed when there are any.
    """
    if not conn.autocommit:
        raise ValueError("pass a connection in autocommit mode")
    return [
        *await _record_problems(conn, workspace_id, mirrors),
        *await _vector_problems(conn, workspace_id),
        *await _stream_problems(conn, workspace_id, mirrors),
        *await _orphan_problems(conn, workspace_id),
    ]


async def assert_consistent(
    conn: Conn, *, workspace_id: UUID | None = None, mirrors: MirrorManager | None = None
) -> None:
    """Fail the test with the list of :func:`consistency_problems`, if there are any."""
    problems = await consistency_problems(conn, workspace_id=workspace_id, mirrors=mirrors)
    assert not problems, "the base is not consistent:\n" + "\n".join(problems)


async def advisory_locks(conn: Conn) -> list[tuple[int, int, int]]:
    """The advisory locks held in ``conn``'s database: (backend pid, first key, second key)."""
    found = await _rows(
        conn,
        "SELECT pid, classid::bigint, objid::bigint FROM pg_locks"
        " WHERE locktype = 'advisory' AND granted"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        " ORDER BY pid",
    )
    return [(row[0], row[1], row[2]) for row in found]
