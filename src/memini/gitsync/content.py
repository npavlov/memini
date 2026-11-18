"""The raw content of file records, read from the repository mirrors: what a rebuild chunks again.

Code, and Markdown documents of repositories, are not copied into the database; the mirror of each
git source is its raw store (principle 1, requirements section 3). :class:`MirrorContent` is the
:class:`~memini.index.RawContent` of the records of git sources for :func:`memini.index.reindex`:
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
"""

import logging

from memini.git import BlobTooLarge, GitError, InvalidRevision, MirrorManager
from memini.gitsync.model import split_code_external_id
from memini.index import RawContentUnavailableError, RawDocument, RecordRef

__all__ = ["MirrorContent"]

log = logging.getLogger(__name__)


class MirrorContent:
    """Reads the records of git sources from the mirrors under one
    :class:`~memini.git.MirrorManager`."""

    def __init__(self, mirrors: MirrorManager) -> None:
        self._mirrors = mirrors

    async def read(self, record: RecordRef) -> RawDocument | None:
        if record.kind not in ("code_file", "document"):
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
            blobs = await self._mirrors.read_blobs(mirror, [record.content_hash])
        except (BlobTooLarge, InvalidRevision) as error:
            log.warning("%s record %s is not indexed: %s", noun, record.id, error)
            return None
        except GitError as error:
            raise RawContentUnavailableError(
                f"{noun} record {record.id} cannot be read from the mirror of source"
                f" {record.source_id}: {error}"
            ) from error
        content = blobs.get(record.content_hash)
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
