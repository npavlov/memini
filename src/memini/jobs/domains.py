"""The ``assign_domains`` job: the domains of the catalog an item belongs to, chosen by a model
when nothing else says (requirements, section 5, **Domain assignment**).

Two forms, one kind. The ``extract`` job enqueues one for a record whose extraction stored items
(:func:`enqueue_record_assignment`): it carries the record's source, so it takes turns with the
source's other jobs and goes when the source is deleted. An admin enqueues one for a whole
workspace (:func:`enqueue_workspace_assignment`), after its catalog changed: it carries no source.
The handler (:mod:`memini.decisions.assign`) asks the model only about items it has not answered
for the domains it would show them now, so either form run again calls nothing.

Here only what enqueueing needs, nothing of the LLM.
"""

import uuid
from typing import Any, Final

from psycopg import AsyncConnection

from memini.jobs.queue import Enqueued, enqueue

__all__ = [
    "ASSIGN_DOMAINS_KIND",
    "enqueue_record_assignment",
    "enqueue_workspace_assignment",
]

ASSIGN_DOMAINS_KIND: Final = "assign_domains"
"""Job kind of the domain assignment of a record's items, or of a workspace's."""


async def enqueue_record_assignment(
    conn: AsyncConnection[Any],
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    source_id: uuid.UUID,
    record_id: str,
) -> Enqueued:
    """Enqueue the assignment of the active items with evidence in ``record_id``. A queued or
    running one of the same record is returned instead."""
    return await enqueue(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        kind=ASSIGN_DOMAINS_KIND,
        source_id=source_id,
        args={"record_id": record_id},
        dedupe_key=f"record:{record_id}",
    )


async def enqueue_workspace_assignment(
    conn: AsyncConnection[Any], *, workspace_id: uuid.UUID, owner_id: uuid.UUID
) -> Enqueued:
    """Enqueue the assignment of every active item of the workspace. A queued or running one is
    returned instead."""
    return await enqueue(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        kind=ASSIGN_DOMAINS_KIND,
        args={"scope": "workspace"},
        dedupe_key="workspace",
    )
