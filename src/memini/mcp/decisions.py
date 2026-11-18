"""The decision write tools: ``record_decision``, ``supersede_decision``, ``close_decision`` and
``retract_decision``.

Writes, so each needs the ``ingest`` scope (requirements, section 6): a token without it neither
sees nor calls them. Each tool authorizes the write
(:func:`~memini.auth.authorization.authorize_write`), borrows a connection from the pool and calls
its function in :mod:`memini.decisions.service`, which the HTTP routes call too. A
:class:`~memini.decisions.DecisionError` becomes an error result with its message, so the model
can correct its call; a refusal of the append-only journal names the constraint.

The structured result is :class:`~memini.decisions.DecisionItem`: the item written, with its
evidence, participants and links. The text content says the same for clients that read only text,
the statement in a fenced block: data, never instructions.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import BaseModel

from memini.auth import WriteAccess, authorization
from memini.decisions import (
    DecisionError,
    DecisionItem,
    ItemArguments,
    RecordDecisionArguments,
    SupersedeArguments,
    run_close,
    run_record_decision,
    run_retract,
    run_supersede,
)
from memini.mcp.query import fenced
from memini.mcp.tools import Tool, ToolCall, ToolError

__all__ = [
    "CLOSE_DECISION_TOOL",
    "DECISION_WRITE_TOOLS",
    "RECORD_DECISION_TOOL",
    "RETRACT_DECISION_TOOL",
    "SUPERSEDE_DECISION_TOOL",
    "summarize_item",
]

type Conn = AsyncConnection[TupleRow]
type Runner[A: BaseModel] = Callable[[Conn, WriteAccess, A], Awaitable[DecisionItem]]


def _tool_run[A: BaseModel](run: Runner[A]) -> Callable[[ToolCall, A], Awaitable[DecisionItem]]:
    """A tool's ``run``: authorize the write, borrow a connection, call ``run``, report a
    refusal."""

    async def tool_run(call: ToolCall, arguments: A) -> DecisionItem:
        # Called through its module, so a test can watch the tool go through the write access.
        access = authorization.authorize_write(call.principal, "ingest")
        pool = call.pool
        if pool is None:
            raise ToolError("The database is not available.")
        try:
            async with pool.connection() as conn:
                return await run(conn, access, arguments)
        except DecisionError as error:
            raise ToolError(error.message) from None

    return tool_run


async def _record(
    conn: Conn, access: WriteAccess, arguments: RecordDecisionArguments
) -> DecisionItem:
    return await run_record_decision(conn, access, arguments.workspace, arguments)


async def _supersede(
    conn: Conn, access: WriteAccess, arguments: SupersedeArguments
) -> DecisionItem:
    return await run_supersede(conn, access, arguments.item_id, arguments.to_refs())


async def _close(conn: Conn, access: WriteAccess, arguments: ItemArguments) -> DecisionItem:
    return await run_close(conn, access, arguments.item_id)


async def _retract(conn: Conn, access: WriteAccess, arguments: ItemArguments) -> DecisionItem:
    return await run_retract(conn, access, arguments.item_id)


# --- Text summaries ------------------------------------------------------------------------------


def _time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


_KINDS = {"decision": "Decision", "open_question": "Open question"}


def summarize_item(item: DecisionItem) -> str:
    """The text content of a written item: what it is, where it stands, its links and evidence,
    and the statement in a fenced block."""
    by = "by hand" if item.manual else f"by extractor {item.extractor}"
    status = item.status
    if item.invalidated_at is not None:
        status += f" since {_time(item.invalidated_at)}"
    lines = [
        f"{_KINDS[item.kind]} {item.id} in workspace {item.workspace}: {status}.",
        f"Said {_time(item.decided_at)}; recorded {_time(item.recorded_at)} {by}.",
    ]
    lines += [f"It {link.relation} {link.supersedes_id}." for link in item.supersedes]
    if item.superseded_by is not None:
        link = item.superseded_by
        lines.append(f"{link.item_id} {link.relation} it.")
    if item.participants:
        lines.append(f"Participants: {', '.join(str(p.person_id) for p in item.participants)}.")
    lines.append("Evidence:")
    for evidence in item.evidence:
        source = evidence.source or f"source {evidence.source_id}"
        anchor = "" if evidence.anchor is None else f" at {evidence.anchor}"
        lines.append(
            f"- {source}: {evidence.record_kind} {evidence.external_id}{anchor}"
            f" (record {evidence.record_id}, {evidence.record_state})"
        )
    lines.append(f"Statement:\n{fenced(item.statement)}")
    return "\n".join(lines)


# --- The tools -----------------------------------------------------------------------------------

_APPEND_ONLY = (
    "The journal is append-only: nothing is edited or deleted, a refusal names the database"
    " constraint that enforces the rule."
)

RECORD_DECISION_TOOL = Tool(
    name="record_decision",
    title="Record a decision",
    description=(
        "Record a decision or an open question by hand in a workspace, with its evidence: one or "
        "more records it comes from, each by record_id or by source and external_id (as search "
        "results give them), with an anchor (the message of a thread, the commit of a code file). "
        "An item without evidence is not stored. Participants are person ids. supersedes links "
        "active items it replaces (relation replaces) or open questions a decision answers "
        "(answers): to correct an extracted item, record the correction with supersedes. An item "
        "recorded by hand is never superseded by extraction, so it survives a re-extraction. "
        f"{_APPEND_ONLY} Returns the new item."
    ),
    arguments=RecordDecisionArguments,
    result=DecisionItem,
    run=_tool_run(_record),
    summarize=summarize_item,
    scope="ingest",
    read_only=False,
)

SUPERSEDE_DECISION_TOOL = Tool(
    name="supersede_decision",
    title="Supersede decisions",
    description=(
        "Link a stored item to earlier items it replaces (relation replaces: they become "
        "superseded) or, for a decision, to open questions it answers (answers: they become "
        "closed). Both sides are active and in one workspace, and the newer item was said no "
        "earlier. An extracted item never supersedes one recorded by hand. To replace an item with "
        f"a new statement, use record_decision with supersedes instead. {_APPEND_ONLY} Returns the "
        "newer item with its links."
    ),
    arguments=SupersedeArguments,
    result=DecisionItem,
    run=_tool_run(_supersede),
    summarize=summarize_item,
    scope="ingest",
    read_only=False,
)

CLOSE_DECISION_TOOL = Tool(
    name="close_decision",
    title="Close a decision",
    description=(
        "Close an active item without a replacement: an open question no longer asked, a "
        "decision no longer in force. It leaves the default view and stays in the history. "
        f"{_APPEND_ONLY} Returns the item."
    ),
    arguments=ItemArguments,
    result=DecisionItem,
    run=_tool_run(_close),
    summarize=summarize_item,
    scope="ingest",
    read_only=False,
)

RETRACT_DECISION_TOOL = Tool(
    name="retract_decision",
    title="Retract a decision",
    description=(
        "Retract an active item that was wrong: a misread extraction, a mistake. It leaves the "
        "default view and stays in the history, marked retracted. To correct it instead, record "
        f"the correction with record_decision and supersedes. {_APPEND_ONLY} Returns the item."
    ),
    arguments=ItemArguments,
    result=DecisionItem,
    run=_tool_run(_retract),
    summarize=summarize_item,
    scope="ingest",
    read_only=False,
)

DECISION_WRITE_TOOLS: Sequence[Tool[Any, Any]] = (
    RECORD_DECISION_TOOL,
    SUPERSEDE_DECISION_TOOL,
    CLOSE_DECISION_TOOL,
    RETRACT_DECISION_TOOL,
)
"""The decision write tools, in the order ``tools/list`` shows them."""
