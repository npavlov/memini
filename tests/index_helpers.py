"""Shared by the index tests: a synthetic repository file, records to index it into, and a fake raw
store. Every value here is synthetic."""

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.abc import QueryNoTemplate
from psycopg.rows import TupleRow

from memini.chunking import FileChunks, chunk_file
from memini.ids import RecordKind, record_id
from memini.index import RawDocument, RecordRef
from memini.workspaces import create_workspace, ensure_principal

type Conn = AsyncConnection[TupleRow]

PATH = "src/greenhouse.py"

WATER = '''def water(plant, litres):
    """Give a plant some water, within the limit."""
    amount = min(litres, LIMIT)
    plant.soil += amount
    if plant.soil > 100:
        plant.soil = 100
    plant.watered = True
    return amount
'''

WATER_EDITED = '''def water(plant, litres):
    """Give a plant some water, within the limit."""
    amount = min(litres, LIMIT // 2)
    plant.soil += amount
    if plant.soil > 100:
        plant.soil = 100
    plant.watered = True
    return amount
'''

PRUNE = '''def prune(plant, leaves):
    """Remove some leaves, keeping at least one."""
    removed = min(leaves, plant.leaves - 1)
    plant.leaves -= removed
    if plant.leaves < 1:
        plant.leaves = 1
    plant.pruned = True
    return removed
'''

LAMP = '''class Lamp:
    """A lamp over the plants."""

    def on(self, hours):
        total = math.floor(hours)
        self.hours += total
        if self.hours > 16:
            self.hours = 16
        self.lit = True
        return total
'''

HEAD = '''"""Greenhouse controls for a synthetic test."""

import math

LIMIT = 40
'''


def source(*parts: str, head: str = HEAD) -> bytes:
    """A Python file of ``head`` and ``parts``, two blank lines apart."""
    return "\n\n".join((head, *parts)).encode()


FILE_V1 = source(WATER, PRUNE, LAMP)
"""Four chunks (the head, water, prune, Lamp); five symbols (LIMIT, water, prune, Lamp, Lamp.on)."""


def blob_id(content: bytes) -> str:
    """Stands in for a git blob id: the record's content hash."""
    return hashlib.sha256(b"blob\0" + content).hexdigest()


@dataclass(frozen=True)
class Scope:
    owner: uuid.UUID
    acme: uuid.UUID
    other: uuid.UUID
    repo: uuid.UUID
    """A git source in acme."""
    fork: uuid.UUID
    """Another git source in acme."""
    other_repo: uuid.UUID
    """A git source in other."""
    notes: uuid.UUID
    """A push source in acme."""


async def make_scope(conn: Conn) -> Scope:
    owner = await ensure_principal(conn, "owner")
    acme = await create_workspace(conn, owner_id=owner, name="acme")
    other = await create_workspace(conn, owner_id=owner, name="other")

    async def add_source(workspace: uuid.UUID, name: str, kind: str) -> uuid.UUID:
        cur = await conn.execute(
            "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, %s, %s)"
            " RETURNING id",
            (workspace, owner, name, kind),
        )
        row = await cur.fetchone()
        assert row is not None
        source_id: uuid.UUID = row[0]
        return source_id

    return Scope(
        owner=owner,
        acme=acme.id,
        other=other.id,
        repo=await add_source(acme.id, "greenhouse", "repository"),
        fork=await add_source(acme.id, "greenhouse-fork", "repository"),
        other_repo=await add_source(other.id, "greenhouse", "repository"),
        notes=await add_source(acme.id, "notes", "push"),
    )


async def add_record(
    conn: Conn,
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    source_id: uuid.UUID,
    external_id: str,
    content: bytes,
    kind: RecordKind = "code_file",
) -> str:
    """Store a record (or a new version of it) and return its id. Code keeps no payload."""
    rid = record_id(source_id, kind, external_id)
    payload = None if kind == "code_file" else content
    await conn.execute(
        "INSERT INTO record (id, workspace_id, owner_id, source_id, kind, external_id,"
        " content_hash, occurred_at, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET content_hash = EXCLUDED.content_hash,"
        " payload = EXCLUDED.payload, updated_at = now()",
        (
            rid,
            workspace_id,
            owner_id,
            source_id,
            kind,
            external_id,
            blob_id(content),
            datetime(2026, 1, 2, tzinfo=UTC),
            payload,
        ),
    )
    return rid


def chunks_of(rid: str, content: bytes, path: str = PATH) -> FileChunks:
    return chunk_file(rid, path, content)


class FakeRaw:
    """An in-memory raw store: the content of each record by id, at the version stored."""

    def __init__(self) -> None:
        self.files: dict[str, RawDocument] = {}
        self.reads: list[str] = []

    def put(self, rid: str, content: bytes, path: str | None = PATH) -> None:
        self.files[rid] = RawDocument(content=content, path=path)

    async def read(self, record: RecordRef) -> RawDocument | None:
        self.reads.append(record.id)
        return self.files.get(record.id)


async def rows(conn: Conn, query: QueryNoTemplate, params: Any = None) -> list[tuple[Any, ...]]:
    """The rows of ``query``, in binary format: a ``real[]`` arrives as exact float32 values."""
    cur = await conn.execute(query, params, binary=True)
    return [tuple(row) for row in await cur.fetchall()]


async def versions(conn: Conn) -> Mapping[str, str]:
    """``xmin`` of every chunk and symbol row: it changes exactly when a row is written."""
    found = await rows(
        conn,
        "SELECT 'chunk:' || id, xmin::text FROM chunk"
        " UNION ALL SELECT 'symbol:' || record_id || ':' || qualified_name || '#' || ordinal,"
        " xmin::text FROM symbol",
    )
    return dict(found)
