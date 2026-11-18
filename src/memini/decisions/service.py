"""The manual operations on the knowledge journal as functions: from validated arguments to an
item, or a :class:`DecisionError`.

One function per operation, for the MCP tools (:mod:`memini.mcp.decisions`) and the HTTP routes
(:mod:`memini.api.decisions`) alike: :func:`run_record_decision`, :func:`run_supersede`,
:func:`run_close` and :func:`run_retract`. Each takes a connection, the caller's
:class:`~memini.auth.WriteAccess` (the ``ingest`` scope, requirements section 6) and the
arguments, runs in one transaction of its own (a savepoint inside the caller's), and calls
:mod:`memini.knowledge`, which records every item as manual: extraction never supersedes or
answers it (section 5).

Every refusal becomes a :class:`DecisionError` with an HTTP status, a stable code and a message
that never repeats a statement, an external id or an anchor; the only values it names are item ids,
parsed as uuids. The refusals of the append-only journal name the database constraint that
enforces the rule:

| Error | Status | Code |
|---|---|---|
| a field refused: an empty statement, an anchor that does not fit | 422 | ``invalid_item`` |
| evidence by source and external id that names records of two kinds | 422 | ``ambiguous_record`` |
| no such workspace, record, person or item the caller may change | 404 | ``not_found`` |
| the item, or an item it would supersede, is not active | 409 | ``not_active`` |
| a link that breaks a rule: cycle, relation, order, manual | 409 | ``supersession_refused`` |
| any other refusal of the journal's triggers | 409 | ``journal_refused`` |

A token limited to one workspace changes nothing outside it: a workspace, record, person or item
elsewhere is not found, as on every other write route.
"""

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow

from memini import knowledge
from memini.auth import WriteAccess
from memini.decisions.model import DecisionItem, EvidenceArgument, NewDecision
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "NOT_ACTIVE_CONSTRAINT",
    "SUPERSESSION_CONSTRAINTS",
    "DecisionError",
    "decision_errors",
    "run_close",
    "run_record_decision",
    "run_retract",
    "run_supersede",
]

type Conn = AsyncConnection[TupleRow]

NOT_ACTIVE_CONSTRAINT: Final = "knowledge_item_status_transition"
"""The trigger rule a status changes by: once, from active."""
_LINK_ACTIVE_CONSTRAINT: Final = "knowledge_supersession_active_check"
"""The trigger rule a link is stored by: both items are active."""

SUPERSESSION_CONSTRAINTS: Final[dict[knowledge.SupersessionRule, str]] = {
    "cycle": "knowledge_supersession_cycle_check",
    "relation": "knowledge_supersession_relation_check",
    "order": "knowledge_supersession_order_check",
    "manual": "knowledge_supersession_manual_check",
}
"""The constraint that enforces each rule of :data:`~memini.knowledge.SupersessionRule`."""


class DecisionError(Exception):
    """A manual operation that was refused: an HTTP status, a stable code and a message for the
    caller. The message never holds a statement, an external id, an anchor or a secret."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _not_active(error: knowledge.NotActiveError, constraint: str) -> DecisionError:
    return DecisionError(
        409,
        "not_active",
        f"Knowledge item {error.item_id} is {error.status}, not active: the journal is"
        " append-only and a status changes once, from active; only an active item is"
        f" superseded, closed or retracted, or supersedes others (constraint {constraint}).",
    )


@contextmanager
def decision_errors(*, not_active: str = _LINK_ACTIVE_CONSTRAINT) -> Generator[None]:
    """Raise the :class:`DecisionError` for a refusal of :mod:`memini.knowledge` or of the
    journal's triggers. ``not_active`` is the constraint a refusal of an item that is no longer
    active names: the status rule for a close or a retraction, the link rule otherwise."""
    try:
        yield
    except knowledge.InvalidItemError as error:
        raise DecisionError(
            422, "invalid_item", f"The item is not valid: {error.field}: {error.reason}."
        ) from None
    except NotFoundError as error:
        raise DecisionError(404, "not_found", f"No such {error.what}.") from None
    except knowledge.NotActiveError as error:
        raise _not_active(error, not_active) from None
    except knowledge.SupersessionError as error:
        constraint = SUPERSESSION_CONSTRAINTS[error.rule]
        raise DecisionError(
            409,
            "supersession_refused",
            f"The supersession link is refused: {error} (constraint {constraint}).",
        ) from None
    except (errors.CheckViolation, errors.UniqueViolation, errors.ForeignKeyViolation) as error:
        # What the library does not check first: a race it lost, or a rule only the database
        # knows. Only the journal's own constraints are refusals for the caller.
        constraint = error.diag.constraint_name or ""
        if not constraint.startswith("knowledge_"):
            raise
        raise DecisionError(
            409,
            "journal_refused",
            f"The knowledge journal refused the change (constraint {constraint}).",
        ) from None


# --- Evidence ------------------------------------------------------------------------------------

_BY_REFERENCE: Final = sql.SQL("""
SELECT e.n, r.id
FROM unnest(%s::int[], %s::text[], %s::text[], %s::text[]) AS e (n, source, external_id, kind)
JOIN source s ON s.workspace_id = %s AND s.name = e.source
JOIN record r ON r.workspace_id = s.workspace_id AND r.source_id = s.id
             AND r.external_id = e.external_id AND (e.kind IS NULL OR r.kind = e.kind)
WHERE {access}
ORDER BY e.n, r.id
""")


async def _resolve_evidence(
    conn: Conn, access: WriteAccess, workspace_id: UUID, evidence: Sequence[EvidenceArgument]
) -> list[knowledge.EvidenceRef]:
    """Each piece of evidence with its record id: the one given, or the record of that source,
    external id (and kind) in the workspace. Whether a record id names a record of the workspace
    is checked when the item is recorded."""
    by_reference = [(n, each) for n, each in enumerate(evidence) if each.record_id is None]
    found: dict[int, list[str]] = {}
    if by_reference:
        query = _BY_REFERENCE.format(
            access=access.where(
                workspace_id=sql.Identifier("r", "workspace_id"),
                owner_id=sql.Identifier("r", "owner_id"),
                source_id=sql.Identifier("r", "source_id"),
            )
        )
        cur = await conn.execute(
            query,
            (
                [n for n, _ in by_reference],
                [each.source for _, each in by_reference],
                [each.external_id for _, each in by_reference],
                [each.record_kind for _, each in by_reference],
                workspace_id,
            ),
        )
        for n, record_id in await cur.fetchall():
            found.setdefault(n, []).append(record_id)
    refs: list[knowledge.EvidenceRef] = []
    for n, each in enumerate(evidence):
        if each.record_id is not None:
            refs.append(knowledge.EvidenceRef(record_id=each.record_id, anchor=each.anchor))
            continue
        record_ids = found.get(n, [])
        if not record_ids:
            raise DecisionError(404, "not_found", f"No such record (evidence {n}).")
        if len(record_ids) > 1:
            raise DecisionError(
                422,
                "ambiguous_record",
                f"The source has records of more than one kind under that external id"
                f" (evidence {n}); name its record_kind.",
            )
        refs.append(knowledge.EvidenceRef(record_id=record_ids[0], anchor=each.anchor))
    return refs


# --- The operations ------------------------------------------------------------------------------


async def run_record_decision(
    conn: Conn, access: WriteAccess, workspace: str, decision: NewDecision
) -> DecisionItem:
    """Record ``decision`` by hand in ``workspace`` (by name), with its evidence and participants,
    and link the items it supersedes. The new item is active and manual."""
    with decision_errors():
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            evidence = await _resolve_evidence(conn, access, found.id, decision.evidence)
            item = await knowledge.record_item(
                conn, access, found.id, decision.to_new_item(evidence)
            )
    return DecisionItem.of(item)


async def _ensure_in_workspace(
    conn: Conn, access: WriteAccess, workspace: str, item_id: UUID
) -> None:
    found = await get_writable_workspace(conn, access, workspace)
    query = sql.SQL("SELECT 1 FROM knowledge_item i WHERE i.id = %s AND i.workspace_id = %s AND {}")
    condition = access.where(
        workspace_id=sql.Identifier("i", "workspace_id"), owner_id=sql.Identifier("i", "owner_id")
    )
    cur = await conn.execute(query.format(condition), (item_id, found.id))
    if await cur.fetchone() is None:
        raise NotFoundError("knowledge item")


async def run_supersede(
    conn: Conn,
    access: WriteAccess,
    item_id: UUID,
    supersedes: Sequence[knowledge.SupersedesRef],
    *,
    workspace: str | None = None,
) -> DecisionItem:
    """Link the stored item ``item_id`` to the earlier items it replaces or answers; they become
    superseded or closed. With ``workspace``, the item must be in it."""
    with decision_errors():
        async with conn.transaction():
            if workspace is not None:
                await _ensure_in_workspace(conn, access, workspace, item_id)
            item = await knowledge.supersede(conn, access, item_id, supersedes)
    return DecisionItem.of(item)


async def run_close(
    conn: Conn, access: WriteAccess, item_id: UUID, *, workspace: str | None = None
) -> DecisionItem:
    """Close the active item ``item_id`` without a replacement. With ``workspace``, the item must
    be in it."""
    with decision_errors(not_active=NOT_ACTIVE_CONSTRAINT):
        async with conn.transaction():
            if workspace is not None:
                await _ensure_in_workspace(conn, access, workspace, item_id)
            item = await knowledge.close_item(conn, access, item_id)
    return DecisionItem.of(item)


async def run_retract(
    conn: Conn, access: WriteAccess, item_id: UUID, *, workspace: str | None = None
) -> DecisionItem:
    """Retract the active item ``item_id``, which was wrong. With ``workspace``, the item must be
    in it."""
    with decision_errors(not_active=NOT_ACTIVE_CONSTRAINT):
        async with conn.transaction():
            if workspace is not None:
                await _ensure_in_workspace(conn, access, workspace, item_id)
            item = await knowledge.retract_item(conn, access, item_id)
    return DecisionItem.of(item)
