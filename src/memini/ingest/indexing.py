"""The service's indexing hook: chunks the records a batch wrote and writes them to the index."""

from collections.abc import Sequence

import anyio
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.chunking import chunk_payload
from memini.embeddings import Embedder
from memini.index import IndexCounts, index_record
from memini.ingest.model import StoredRecord

__all__ = ["PayloadIndexer"]

type Conn = AsyncConnection[TupleRow]


class PayloadIndexer:
    """A :class:`~memini.ingest.RecordIndexer` that cuts each record written by its kind
    (:func:`memini.chunking.chunk_payload`) and writes the chunks with
    :func:`memini.index.index_record`, in the batch's transaction.

    Every record whose row was written is chunked again, not only one whose payload changed: the
    writer compares each chunk with its stored row and embeds only texts the workspace has no
    vector for (principle 6), so a record that only moved in time costs no embedding, and one
    stored before the index could be written gets its chunks. Chunking runs in a worker thread.

    Raises what :func:`~memini.index.index_record` raises, so that the batch is rolled back:
    :class:`~memini.index.IndexMismatchError` for a workspace whose index was built with another
    fingerprint (``memini reindex`` first), :class:`~memini.embeddings.EmbeddingError` when the
    model fails.
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    async def __call__(self, conn: Conn, records: Sequence[StoredRecord], /) -> IndexCounts:
        counts = IndexCounts()
        for record in records:
            chunks = await anyio.to_thread.run_sync(
                chunk_payload, record.id, record.kind, record.payload
            )
            counts += await index_record(
                conn, self._embedder, record_id=record.id, path=None, chunks=chunks
            )
        return counts
