"""The raw content of file records, read from the repository mirrors: what a rebuild chunks again.

Code, and Markdown documents of repositories, are not copied into the database; the mirror of each
repository is its raw store (principle 1, requirements section 3). :class:`MirrorContent` is the
:class:`~memini.index.RawContent` of the records of repositories for :func:`memini.index.reindex`:
code files and documents. It reads the blob ``record.content_hash`` (the file's git blob id) from
the mirror of ``record.source_id`` and takes the path from ``record.external_id``.

- No mirror for the source, or git failing: :class:`~memini.index.RawContentUnavailableError`, so
  the rebuild stops and deletes nothing. A source's mirror is created by its first sync, which runs
  its fetch before it checks the index, so a sync attempt makes the mirror a rebuild needs even
  while the index is out of date.
- A blob the mirror no longer holds: ``None``, nothing to index, and the record's chunks are
  cleared. Fetches never remove objects, so the commit the record was written at was dropped
  upstream (a force push) and then pruned. Its stream cannot diff from that commit either: the
  next sync lists the ref in full and writes the record again, or turns it into a tombstone.
  Refusing instead would deadlock: the sync waits for the rebuild of a stale index.
- A blob over ``git_max_blob_size`` (the limit was lowered): ``None`` likewise; the next sync sees
  the changed rules, lists the ref in full and turns the record into a tombstone.

Reading in batches
    It is a :class:`~memini.index.Prefetcher`: the rebuild tells it each page of records before it
    reads them. When a record of the page is read and its blob is not at hand, it reads a window of
    the page from that record on: the following records of the same source, at most
    ``batch_files`` blobs and ``batch_bytes`` bytes of them. That is two git processes per window
    (sizes with ``cat-file --batch-check``, contents with ``cat-file --batch``) instead of one per
    record. Only the last window is held in memory: at most ``batch_bytes``, or one blob larger
    than that (up to ``git_max_blob_size``). A record it was not told about, or whose version
    changed since, is read on its own.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from memini.git import BlobTooLarge, GitError, InvalidRevision, Mirror, MirrorManager
from memini.gitsync.model import split_code_external_id
from memini.index import RawContentUnavailableError, RawDocument, RecordRef

__all__ = ["DEFAULT_BATCH_BYTES", "DEFAULT_BATCH_FILES", "MirrorContent"]

log = logging.getLogger(__name__)

DEFAULT_BATCH_FILES: Final = 500
"""Blobs read by one git process at most: a page of the rebuild."""
DEFAULT_BATCH_BYTES: Final = 8 * 1024**2
"""Bytes of blobs held in memory at once, like a batch of the sync."""

_OID: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FILE_KINDS: Final = ("code_file", "document")


@dataclass(slots=True)
class _Window:
    """What the last read ahead found, by blob id."""

    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])
    absent: set[str] = field(default_factory=set[str])
    """Not in the mirror."""
    too_large: dict[str, int] = field(default_factory=dict[str, int])
    """Over the read limit, with their sizes."""

    def __contains__(self, oid: str) -> bool:
        return oid in self.blobs or oid in self.absent or oid in self.too_large


class MirrorContent:
    """Reads the records of repositories from the mirrors under one
    :class:`~memini.git.MirrorManager`, in batches when told what comes next."""

    def __init__(
        self,
        mirrors: MirrorManager,
        *,
        batch_files: int = DEFAULT_BATCH_FILES,
        batch_bytes: int = DEFAULT_BATCH_BYTES,
    ) -> None:
        if batch_files < 1 or batch_bytes < 1:
            raise ValueError("a batch holds at least one file and one byte")
        self._mirrors = mirrors
        self._batch_files = batch_files
        self._batch_bytes = batch_bytes
        self._plan: list[RecordRef] = []
        self._position: dict[str, int] = {}
        self._window = _Window()

    async def prefetch(self, records: Sequence[RecordRef]) -> None:
        """Plan to read ``records`` that are live files of sources with a mirror, in this order.
        Reads nothing yet."""
        mirrored: dict[UUID, bool] = {}
        plan: list[RecordRef] = []
        for record in records:
            if (
                record.kind not in _FILE_KINDS
                or record.deleted
                or _OID.fullmatch(record.content_hash) is None
            ):
                continue
            has_mirror = mirrored.get(record.source_id)
            if has_mirror is None:
                has_mirror = mirrored[record.source_id] = (
                    self._mirrors.find(record.source_id) is not None
                )
            if has_mirror:
                plan.append(record)
        self._plan = plan
        self._position = {record.id: index for index, record in enumerate(plan)}
        self._window = _Window()

    async def read(self, record: RecordRef) -> RawDocument | None:
        if record.kind not in _FILE_KINDS:
            raise ValueError(f"record {record.id} is a {record.kind}, not a file of a repository")
        if record.deleted:
            return None
        noun = "code" if record.kind == "code_file" else "document"
        mirror = self._mirrors.find(record.source_id)
        if mirror is None:
            raise RawContentUnavailableError(
                f"{noun} record {record.id} cannot be read: source {record.source_id} has no"
                " repository mirror yet; its next sync creates it"
            )
        try:
            _, path = split_code_external_id(record.external_id)
        except ValueError:
            log.warning(
                "%s record %s has an external id of another form; nothing to index",
                noun,
                record.id,
            )
            return None
        try:
            planned, content = await self._read_planned(mirror, record)
            if not planned:
                blobs = await self._mirrors.read_blobs(mirror, [record.content_hash])
                content = blobs.get(record.content_hash)
        except (BlobTooLarge, InvalidRevision) as error:
            log.warning("%s record %s is not indexed: %s", noun, record.id, error)
            return None
        except GitError as error:
            raise RawContentUnavailableError(
                f"{noun} record {record.id} cannot be read from the mirror of source"
                f" {record.source_id}: {error}"
            ) from error
        if content is None:
            log.warning(
                "the mirror of source %s no longer holds blob %s of %s record %s; its chunks"
                " are cleared until the next sync writes it again",
                record.source_id,
                record.content_hash,
                noun,
                record.id,
            )
            return None
        return RawDocument(content=content, path=path)

    async def _read_planned(self, mirror: Mirror, record: RecordRef) -> tuple[bool, bytes | None]:
        """``(True, content)`` for a record of the plan, read in a window (``None``: not in the
        mirror); ``(False, None)`` for one to read on its own.

        Raises:
            BlobTooLarge: the blob is over the read limit.
            GitError: reading the window failed.
        """
        oid = record.content_hash
        if oid not in self._window:
            index = self._position.get(record.id)
            if index is None or self._plan[index].content_hash != oid:
                return False, None
            await self._read_window(mirror, index)
        window = self._window
        size = window.too_large.get(oid)
        if size is not None:
            raise BlobTooLarge(oid, size, self._mirrors.options.max_blob_size)
        return True, window.blobs.get(oid)

    async def _read_window(self, mirror: Mirror, index: int) -> None:
        """Read the blobs of the plan from ``index`` on, of the same source, within the batch
        limits; replace the last window with them."""
        self._window = window = _Window()
        source_id = self._plan[index].source_id
        wanted: dict[str, None] = {}
        for record in self._plan[index:]:
            if record.source_id != source_id or len(wanted) >= self._batch_files:
                break
            wanted[record.content_hash] = None
        sizes = await self._mirrors.blob_sizes(mirror, list(wanted))
        limit = self._mirrors.options.max_blob_size
        chosen: list[str] = []
        total = 0
        for oid in wanted:
            size = sizes.get(oid)
            if size is None:
                window.absent.add(oid)
            elif size > limit:
                window.too_large[oid] = size
            elif chosen and total + size > self._batch_bytes:
                break
            else:
                chosen.append(oid)
                total += size
        if chosen:
            window.blobs = await self._mirrors.read_blobs(mirror, chosen)
            window.absent.update(oid for oid in chosen if oid not in window.blobs)
