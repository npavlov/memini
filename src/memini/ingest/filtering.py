"""Source filters at ingest: the step of a batch that drops what the source's rules exclude
(:mod:`memini.filters`), and the stored form of a payload the rules have changed.

The step runs after :func:`~memini.ingest.prepare_batch` and before the batch's transaction, so
it reads each payload in its stored form: redacted and canonical, as prune reads it later
(:mod:`memini.prune`). A rule therefore decides the same at ingest and at prune, and the bytes
of a trimmed thread are the bytes prune would write. What it does to each record:

- **kept**: unchanged;
- **trimmed**, a thread some of whose messages are left out: its payload without them, put in
  its stored form again (:func:`stored_form`), with a new content hash and normalized text;
- **excluded**: it becomes a tombstone of itself. Nothing of it is stored; a live record stored
  before the rule turns into a tombstone, its chunks deleted, as the source had deleted it; and
  a snapshot does not count it among the records it left out. ``memini source prune`` deletes
  such tombstones, and the excluded records stored before a rule, for good.

The batch's run counts ``records_filtered`` (excluded) and ``messages_filtered`` (left out of the
threads, kept or not): counts, never values.
"""

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.filters import SourceFilters
from memini.ingest.model import PreparedBatch, PreparedRecord, PreparedTombstone
from memini.ingest.validate import canonical_json, content_hash
from memini.payloads import normalize
from memini.redaction import redact_text

__all__ = [
    "FILTER_COUNTS",
    "filter_batch",
    "read_source_filters",
    "stored_form",
]

type Conn = AsyncConnection[TupleRow]

FILTER_COUNTS: Final = ("records_filtered", "messages_filtered")
"""The names of the filter counts in a run report."""


async def read_source_filters(conn: Conn, source_id: UUID) -> SourceFilters:
    """The filter rules of the source ``source_id``; none if it has none, or no longer exists
    (the batch then finds it gone)."""
    async with conn.transaction():
        cur = await conn.execute("SELECT config FROM source WHERE id = %s", (source_id,))
        row = await cur.fetchone()
    if row is None or not isinstance(row[0], Mapping):
        return SourceFilters()
    config: Mapping[str, Any] = row[0]  # pyright: ignore[reportUnknownVariableType]
    return SourceFilters.from_json(config.get("filters"))


def stored_form(kind: str, payload: Mapping[str, Any]) -> tuple[bytes, str, str]:
    """A redacted payload of ``kind`` as ingest stores it: its canonical JSON, the content hash
    of those bytes and its normalized text, redacted again, as
    :func:`~memini.ingest.prepare_batch` makes them.

    Raises:
        InvalidBatchError, PayloadError: as :func:`~memini.ingest.prepare_batch` would; never for
            a stored payload trimmed by the filters.
    """
    data = canonical_json(payload)
    return data, content_hash(data), redact_text(normalize(kind, payload)).value


def filter_batch(batch: PreparedBatch, filters: SourceFilters) -> PreparedBatch:
    """``batch`` with the source's rules applied to its records (see the module)."""
    if filters.empty or not batch.records:
        return batch
    records: list[PreparedRecord] = []
    tombstones: list[PreparedTombstone] = list(batch.tombstones)
    excluded = messages = 0
    for record in batch.records:
        payload: dict[str, Any] = json.loads(record.payload)
        verdict = filters.apply(record.kind, payload)
        messages += verdict.messages_excluded
        if not verdict.keep:
            excluded += 1
            tombstones.append(PreparedTombstone(record.index, record.kind, record.external_id))
        elif verdict.payload is None:
            records.append(record)
        else:
            data, digest, text = stored_form(record.kind, verdict.payload)
            records.append(replace(record, payload=data, content_hash=digest, normalized_text=text))
    return replace(
        batch,
        records=tuple(records),
        tombstones=tuple(sorted(tombstones, key=lambda item: item.index)),
        filtered={"records_filtered": excluded, "messages_filtered": messages},
    )
