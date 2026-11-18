"""Removing raw records: forget (by hand) and retention (by age). Requirements, sections 3
(knowledge layer), 4 (Scheduling and hygiene) and 6 (HTTP API); the tables are described in the
forget and retention migration (0013).

Both delete a record with everything that holds its text, in one transaction
(:func:`delete_records`): the record row (payload and normalized text), its chunks and symbols,
its links to persons (by the foreign keys' cascade), and the raw responses it was parsed from once
the raw response store exists (:func:`delete_raw_responses`, the hook). Run reports and job
arguments hold counts and ids, never a record's text. Knowledge items that cite the record stay:
the journal is append-only (principle 4), and evidence keeps the record's reference (source,
kind, external id and URL), never a quote, so nothing of the record's content outlives it. The
evidence then reads as ``forgotten`` or ``expired`` (:attr:`memini.knowledge.Evidence.
record_state`). Persons seen in the record stay too: a person is shared by every record that
names them. The database files keep a deleted row until Postgres reuses its space, and a backup
taken before keeps it as it was.

**Forget** (:func:`forget_record`): one record, live or a tombstone, named by its id or by its
source's name and external id, deleted at once in the request, under the source's advisory lock
so that no sync of the source writes it back meanwhile. The audit entry, ``forgotten_record``,
says who (principal and token) and when, with the record's id, source and kind: no external id,
which can itself be private (a mail subject, a meeting title). The entry is also why the record
does not come back: the next sync of its source would otherwise bring back an item that still
exists upstream, and principle 11 asks that what the user removed stays removed. Ingest leaves
out a record whose id has an entry (counted ``forgotten``) and a git sync skips the file
(``files_skipped_forgotten``); both compute the id from the source, kind and external id, so the
entry needs nothing else. A file of a repository stays in the service's mirror, a copy of the
remote, and ``read_source`` reads it there; to keep a file out of the service, exclude it from
the source. Forget is an administrative route and a CLI command, not an MCP tool: MCP tools are
called by agents that act on retrieved text, which is data and never instructions (principle 11),
and an irreversible hard delete is the user's own decision, made with an admin token.

**Retention** (:func:`expire_source`): a source with ``retention_days`` loses the records older
than that, in batches. For a push or mcp source a record expires by its ``occurred_at``; for a
git source only tombstones expire, by the time they were deleted: a file of the tree at the pinned
commit is current whatever the time of its last change, and dropping it would leave the index
short of what the source holds. A ``retention`` job (:func:`retention_handler`) runs the pass for
each source with a retention every ``retention_interval``, enqueued by the scheduler; a git sync
ends with a pass over its source too, as an mcp sync will (M3), so a source that syncs never
shows records past its retention for longer than its schedule. Retention writes no audit entry
per record; its run report counts what it deleted. A record retention removes while knowledge
evidence cites it gets an ``expired_record`` row, so that the evidence reads as expired.
"""

from memini.removal.core import RemovalCounts, delete_raw_responses, delete_records
from memini.removal.forget import (
    AmbiguousRecordError,
    ForgetTarget,
    Forgotten,
    forget_record,
    forgotten_record_ids,
)
from memini.removal.retention import (
    DEFAULT_RETENTION_BATCH,
    RETENTION,
    RetentionResult,
    expire_source,
    retention_handler,
)

__all__ = [
    "DEFAULT_RETENTION_BATCH",
    "RETENTION",
    "AmbiguousRecordError",
    "ForgetTarget",
    "Forgotten",
    "RemovalCounts",
    "RetentionResult",
    "delete_raw_responses",
    "delete_records",
    "expire_source",
    "forget_record",
    "forgotten_record_ids",
    "retention_handler",
]
