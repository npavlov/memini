"""Raw content for a rebuild: where the bytes of a record come from when its chunks are cut again.

Principle 1: every index is derived and can be rebuilt from the raw records. A rebuild
(:func:`memini.index.reindex`) asks a :class:`RawContent` for each record's bytes and chunks them
again. Where the bytes are depends on where the record came from:

- A thread, a meeting or a document of a push or MCP source keeps its payload in the ``record``
  row, byte for byte (:class:`StoredPayload`): canonical JSON, which the chunker of its kind
  parses (:func:`memini.chunking.chunk_payload`), never the bytes of a file.
- A file of a git source, a code file or a Markdown document, is not copied into the database:
  the service's mirror of the repository is the raw store, and the record's ``content_hash`` is
  the file's git blob id. Reading it is the git connector's part,
  :class:`memini.gitsync.MirrorContent`, which ``memini reindex`` passes as ``code``. Without it,
  :class:`MirrorNotAvailable` refuses, and a rebuild of a workspace with such records stops with a
  message instead of deleting their chunks. A document goes to the one or the other by the kind
  of its source (:class:`BySourceKind`).

A provider returns ``None`` for a record that has nothing to index, so its chunks are deleted; it
raises :class:`RawContentUnavailableError` when it cannot tell right now (a mirror not cloned yet,
a fetch that failed), so the rebuild stops and nothing is lost.

A provider that reads in batches is a :class:`Prefetcher` as well: before each page of records the
rebuild tells it which records it is about to read, in order, and it may read them ahead, in
batches of its choosing (the mirror reader reads many blobs per git process). :class:`ByKind` and
:class:`BySourceKind` pass the page on to the providers they hold.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, get_args, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.ids import RecordKind

__all__ = [
    "RECORD_COLUMNS",
    "ByKind",
    "BySourceKind",
    "MirrorNotAvailable",
    "Prefetcher",
    "RawContent",
    "RawContentUnavailableError",
    "RawDocument",
    "RecordRef",
    "StoredPayload",
    "default_raw_content",
    "record_ref",
]

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True, slots=True)
class RecordRef:
    """A record as the index sees it: what it is, where it belongs, which version of it."""

    id: str
    workspace_id: UUID
    owner_id: UUID
    source_id: UUID
    kind: RecordKind
    external_id: str
    """The id of the record in its source; for a code file, as the git connector forms it."""
    content_hash: str
    """The digest of the content; for a code file, its git blob id."""
    deleted: bool
    """A tombstone: the source deleted the record. It has nothing to index."""


RECORD_COLUMNS: Final = sql.SQL(
    "id, workspace_id, owner_id, source_id, kind, external_id, content_hash, deleted_at IS NOT NULL"
)
"""The columns of ``record`` that :func:`record_ref` reads, in its order."""


def record_ref(row: Sequence[Any]) -> RecordRef:
    """A :class:`RecordRef` from a row of :data:`RECORD_COLUMNS`."""
    return RecordRef(
        id=row[0],
        workspace_id=row[1],
        owner_id=row[2],
        source_id=row[3],
        kind=_record_kind(row[4]),
        external_id=row[5],
        content_hash=row[6],
        deleted=row[7],
    )


_KINDS: Final[tuple[RecordKind, ...]] = get_args(RecordKind)


def _record_kind(value: object) -> RecordKind:
    for kind in _KINDS:
        if value == kind:
            return kind
    raise ValueError(f"unexpected record kind {value!r}")


@dataclass(frozen=True, slots=True)
class RawDocument:
    """The raw bytes of a record, and what a chunker needs besides them."""

    content: bytes
    path: str | None = None
    """For a file of a repository: its path, for chunk headers and symbols."""
    payload: bool = False
    """The content is the record's stored payload (canonical JSON, :mod:`memini.payloads`), not
    the bytes of a file."""


class RawContentUnavailableError(Exception):
    """The raw content of a record cannot be read now. A rebuild stops; nothing is deleted."""


class RawContent(Protocol):
    """Where a rebuild reads the raw bytes of a record."""

    async def read(self, record: RecordRef) -> RawDocument | None:
        """The bytes of ``record`` at the version it names, or ``None`` if it has nothing to index.

        Raises:
            RawContentUnavailableError: the content exists but cannot be read now.
        """
        ...


@runtime_checkable
class Prefetcher(Protocol):
    """A :class:`RawContent` that reads ahead."""

    async def prefetch(self, records: Sequence[RecordRef]) -> None:
        """``records`` are read next, in this order, until the next call; earlier hints are
        forgotten. The records it does not serve are ignored. Reads nothing that cannot wait:
        reading ahead is the provider's choice, within a memory budget of its own."""
        ...


async def _prefetch(providers: Sequence[RawContent], records: Sequence[RecordRef]) -> None:
    """Pass ``records`` to each distinct provider that reads ahead."""
    seen: set[int] = set()
    for provider in providers:
        if id(provider) not in seen and isinstance(provider, Prefetcher):
            seen.add(id(provider))
            await provider.prefetch(records)


class StoredPayload:
    """The payload kept in the ``record`` row: threads, meetings, documents of other sources than
    git.

    It reads the payload as it is now, which may be newer than ``record``: a rebuild compares the
    version again under the record's lock and reads once more if it changed. It reads on ``conn``,
    which may be the connection the rebuild writes on: the two take turns.
    """

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    async def read(self, record: RecordRef) -> RawDocument | None:
        cur = await self._conn.execute("SELECT payload FROM record WHERE id = %s", (record.id,))
        row = await cur.fetchone()
        payload: bytes | None = None if row is None else row[0]
        return None if payload is None else RawDocument(content=bytes(payload), payload=True)


class MirrorNotAvailable:
    """Stands in for the repository mirrors where no reader of them is given.

    The reader, :class:`memini.gitsync.MirrorContent`, needs the git settings, which this package
    does not know. Every read here refuses, so that a rebuild without the reader does not take the
    missing mirrors for deleted files.
    """

    async def read(self, record: RecordRef) -> RawDocument | None:
        raise RawContentUnavailableError(
            f"{_NAMES[record.kind]} record {record.id} cannot be read: no reader for repository"
            " mirrors was given, so files of repositories cannot be rebuilt from raw"
        )


_NAMES: Final[dict[RecordKind, str]] = {
    "code_file": "code",
    "document": "document",
    "thread": "thread",
    "meeting": "meeting",
}


class ByKind:
    """A provider per record kind. A kind without one has nothing to index."""

    def __init__(self, providers: Mapping[RecordKind, RawContent]) -> None:
        self._providers = dict(providers)

    async def read(self, record: RecordRef) -> RawDocument | None:
        provider = self._providers.get(record.kind)
        return None if provider is None else await provider.read(record)

    async def prefetch(self, records: Sequence[RecordRef]) -> None:
        await _prefetch(list(self._providers.values()), records)


class BySourceKind:
    """Records of git sources from ``git``, records of other sources from ``other``.

    It looks up the kind of each source once, on ``conn``; a source's kind does not change. A
    record whose source is gone has nothing to index: its records went with it.
    """

    def __init__(self, conn: Conn, *, git: RawContent, other: RawContent) -> None:
        self._conn = conn
        self._git = git
        self._other = other
        self._kinds: dict[UUID, str] = {}

    async def read(self, record: RecordRef) -> RawDocument | None:
        kind = self._kinds.get(record.source_id)
        if kind is None:
            cur = await self._conn.execute(
                "SELECT kind FROM source WHERE id = %s", (record.source_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            kind = self._kinds[record.source_id] = row[0]
        return await (self._git if kind == "git" else self._other).read(record)

    async def prefetch(self, records: Sequence[RecordRef]) -> None:
        # Each provider picks the records it serves; the mirror reader those of git sources.
        await _prefetch([self._git, self._other], records)


def default_raw_content(conn: Conn, *, code: RawContent | None = None) -> RawContent:
    """Files of git sources from ``code`` (:class:`memini.gitsync.MirrorContent` in
    ``memini reindex``; by default :class:`MirrorNotAvailable`): code files, and documents of git
    sources. Payloads from the ``record`` table for everything else."""
    stored = StoredPayload(conn)
    files = code or MirrorNotAvailable()
    return ByKind(
        {
            "code_file": files,
            "thread": stored,
            "meeting": stored,
            "document": BySourceKind(conn, git=files, other=stored),
        }
    )
