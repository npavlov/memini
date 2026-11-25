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
| a project or domain that is not a name, or more domains than allowed | 422 | ``invalid_place`` |
| a project or domain the workspace's catalog does not hold | 422 | ``unknown_place`` |
| unlinking a domain that is not a link (it came by push, default or code) | 409 | ``not_a_link`` |

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
from memini.decisions import context
from memini.decisions.model import DecisionItem, DomainLinkBody, EvidenceArgument, NewDecision
from memini.knowledge.reader import load_item
from memini.search.catalog import load_code_maps
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "NOT_ACTIVE_CONSTRAINT",
    "SUPERSESSION_CONSTRAINTS",
    "DecisionError",
    "decision_errors",
    "run_close",
    "run_link_domains",
    "run_record_decision",
    "run_retract",
    "run_supersede",
    "run_unlink_domain",
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
    except context.UnknownPlaceError as error:
        raise DecisionError(
            422,
            "unknown_place",
            f"Not in the catalog: {error.field}: {error.reason}; get_catalog lists the names.",
        ) from None
    except context.PlaceError as error:
        raise DecisionError(
            422, "invalid_place", f"Not a project or domain: {error.field}: {error.reason}."
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


def _resolve_evidence(evidence: Sequence[EvidenceArgument]) -> list[knowledge.EvidenceRef]:
    """Each piece of evidence as the journal keeps it. Whether a record id names a record of the
    workspace is checked when the item is recorded."""
    return [
        knowledge.EvidenceRef(record_id=each.record_id, anchor=each.anchor) for each in evidence
    ]


# --- The operations ------------------------------------------------------------------------------


async def _places(
    conn: Conn,
    access: WriteAccess,
    workspace_id: UUID,
    project: str | None,
    domains: Sequence[str],
) -> list[tuple[str, str | None]]:
    """``project`` and ``domains`` as the catalog of the workspace holds them."""
    if project is None and not domains:
        return []
    catalog = (await load_code_maps(conn, access, [workspace_id]))[workspace_id]
    return context.resolve_places(context.CatalogNames.of(catalog), project, domains)


async def _with_context(conn: Conn, access: WriteAccess, item: knowledge.Item) -> DecisionItem:
    found = await context.load_contexts(conn, access, [item.workspace_id], item_ids=[item.id])
    return DecisionItem.of(item, found.context(item.id))


async def run_record_decision(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    decision: NewDecision,
    *,
    token_id: UUID | None = None,
) -> DecisionItem:
    """Record ``decision`` by hand in ``workspace`` (by name), with its evidence and participants,
    and link the items it supersedes. The new item is active and manual. Its ``project`` and
    ``domains`` are linked by hand (origin ``manual``, :mod:`memini.decisions.context`) in the
    same transaction; ``token_id`` is the token of the request, kept with the links."""
    with decision_errors():
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            places = await _places(conn, access, found.id, decision.project, decision.domains)
            evidence = _resolve_evidence(decision.evidence)
            item = await knowledge.record_item(
                conn, access, found.id, decision.to_new_item(evidence)
            )
            await context.add_links(
                conn,
                access,
                workspace_id=found.id,
                item_id=item.id,
                places=places,
                origin="manual",
                token_id=token_id,
            )
            result = await _with_context(conn, access, item)
    return result


async def run_link_domains(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    item_id: UUID,
    body: DomainLinkBody,
    *,
    token_id: UUID | None = None,
) -> DecisionItem:
    """Link the stored item ``item_id`` of ``workspace`` by hand to a project or to domains of
    the catalog. Outside the journal: the item itself does not change, whatever its status."""
    with decision_errors():
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            await _ensure_in_workspace(conn, access, workspace, item_id)
            places = await _places(conn, access, found.id, body.project, body.domains)
            await context.add_links(
                conn,
                access,
                workspace_id=found.id,
                item_id=item_id,
                places=places,
                origin="manual",
                token_id=token_id,
            )
            item = await load_item(conn, access, item_id)
            result = await _with_context(conn, access, item)
    return result


async def run_unlink_domain(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    item_id: UUID,
    *,
    project: str,
    domain: str | None,
) -> DecisionItem:
    """Remove the item's link to ``domain`` of ``project`` (``None``: to the project itself),
    made by hand or by the model. A domain the item has by its record (push, default) or its code
    is not a link: 409 ``not_a_link``."""
    with decision_errors():
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            await _ensure_in_workspace(conn, access, workspace, item_id)
            removed = await context.remove_link(
                conn, access, workspace_id=found.id, item_id=item_id, project=project, domain=domain
            )
            if removed is None:
                raise DecisionError(
                    409,
                    "not_a_link",
                    "The item has no link to that project or domain: what it has by its record"
                    " (push, default) or its code is changed there, not here.",
                )
            item = await load_item(conn, access, item_id)
            result = await _with_context(conn, access, item)
    return result


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
