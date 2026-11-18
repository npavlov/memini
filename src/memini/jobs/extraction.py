"""The ``extract`` job: decision extraction of one thread or meeting (requirements, section 5).

Ingest enqueues one in the transaction that writes a new or changed thread or meeting
(:func:`memini.ingest.ingest` with ``extract``), so the job becomes visible with the record, or not
at all. The handler (:mod:`memini.extraction.job`) reads the record as it is when the job runs,
extracts its decisions and open questions through the LLM gate, runs the supersession check and
stores what it found with :func:`memini.knowledge.record_extraction`, which is idempotent per
record content and extractor.

A job carries its record's source, so it takes turns with the source's other jobs (a sync, a
retention pass) and goes when the source is deleted. Its dedupe key is the record id and the
content hash the write left: an extraction of that content is queued or running at most once,
and a change that arrives while an extraction runs gets a job of its own.

Here only what ingest needs: the kind, the key and :func:`enqueue_extraction`. Nothing of the
LLM, so ingest does not import it.
"""

import uuid
from typing import Any, Final

from psycopg import AsyncConnection

from memini.jobs.queue import Enqueued, enqueue

__all__ = ["EXTRACT_KIND", "enqueue_extraction", "extraction_dedupe_key"]

EXTRACT_KIND: Final = "extract"
"""Job kind of the decision extraction of one thread or meeting (memini.extraction.job)."""


def extraction_dedupe_key(record_id: str, content_hash: str) -> str:
    """The dedupe key of the extraction of one content of one record."""
    return f"{record_id}:{content_hash}"


async def enqueue_extraction(
    conn: AsyncConnection[Any],
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    source_id: uuid.UUID,
    record_id: str,
    content_hash: str,
) -> Enqueued:
    """Enqueue the extraction of the record ``record_id`` as its content ``content_hash`` stands.

    Runs in the caller's transaction, if one is open. A queued or running job of the same record
    and content is returned instead of a new one.
    """
    return await enqueue(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        kind=EXTRACT_KIND,
        source_id=source_id,
        args={"record_id": record_id, "content_hash": content_hash},
        dedupe_key=extraction_dedupe_key(record_id, content_hash),
    )
