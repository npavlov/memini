"""The agent executor's runs in the database: start, next call, a posted response, finish. See
:mod:`memini.executors.agent` for the rules."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, cast
from uuid import UUID

from psycopg import AsyncConnection, errors, pq, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import JsonValue

from memini.auth import WriteAccess
from memini.executors.agent.model import (
    MAX_ATTEMPTS,
    RUN_KIND,
    AgentRun,
    AgentRunClosedError,
    AgentRunOpenError,
    AgentWindow,
    CallMismatchError,
    CallNotHandedOutError,
    FinishResult,
    HandedOutCall,
    NextStep,
    Receipt,
    RecipeChangedError,
    ServerMismatchError,
    SourceNotReadyError,
    WindowOutcome,
    WindowStatus,
    mcp_result,
    source_recipe,
    version_matches,
    window_overrides,
)
from memini.ingest import DEFAULT_LIMITS, IngestLimits, RecordIndexer, canonical_json
from memini.ingest.model import InvalidBatchError
from memini.intake import PLAN_DETAIL, plan_stream, resolve_settings
from memini.jobs import source_lock_key
from memini.recipes import (
    CallKind,
    Check,
    MissingCall,
    ParserNotFoundError,
    Recipe,
    WindowInput,
    WindowParser,
    get_parser,
    missing_calls,
    recipe_digest,
)
from memini.redaction import RedactionCollisionError, redact_json
from memini.responses import (
    InvalidResponseError,
    NotAnMcpSourceError,
    ServerInfo,
    WindowNotFoundError,
    begin_window,
    call_key,
    ingest_window,
    load_window,
    store_response,
)
from memini.responses.store import MAX_RESPONSE_SIZE
from memini.runs import record_run
from memini.sources import SourceBusyError
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "AGENT_RUN_DETAIL",
    "finish_run",
    "get_agent_run",
    "next_call",
    "start_run",
    "submit_response",
]

type Conn = AsyncConnection[TupleRow]
type _Database = AsyncConnectionPool[Any] | AsyncConnection[Any]

log = logging.getLogger(__name__)

AGENT_RUN_DETAIL: Final = "agent_run"
"""The name, in the run report's ``details``, of the agent run: its id, pinned time and server."""


def _require_idle(conn: Conn) -> None:
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("the agent executor commits its own transactions: pass an idle connection")


# --- Sources and runs ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Source:
    workspace_id: UUID
    workspace: str
    id: UUID
    name: str
    config: Mapping[str, Any]


async def _source(conn: Conn, access: WriteAccess, workspace: str, source: str) -> _Source:
    """The mcp source ``source`` of ``workspace``, if ``access`` may change it."""
    async with conn.transaction():
        found = await get_writable_workspace(conn, access, workspace)
        query = sql.SQL(
            "SELECT s.id, s.kind, s.config FROM source s"
            " WHERE s.workspace_id = %s AND s.name = %s AND {}"
        ).format(
            access.where(
                workspace_id=sql.Identifier("s", "workspace_id"),
                owner_id=sql.Identifier("s", "owner_id"),
                source_id=sql.Identifier("s", "id"),
            )
        )
        cur = await conn.execute(query, (found.id, source))
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError("source")
    if row[1] != "mcp":
        raise NotAnMcpSourceError(row[1])
    return _Source(found.id, found.name, row[0], source, cast(Mapping[str, Any], row[2]))


def _recipe_and_parser(found: _Source) -> tuple[Recipe, WindowParser]:
    recipe = source_recipe(found.config)
    try:
        parser = get_parser(recipe.parser.name)
    except ParserNotFoundError as error:
        raise SourceNotReadyError(str(error)) from None
    return recipe, parser


_RUN: Final = """
SELECT r.id, r.workspace_id, r.owner_id, r.source_id, r.stream_id, st.key, r.as_of,
       r.recipe_digest, r.server_name, r.server_version, r.plan, r.counts, r.started_at,
       r.closed_at, r.finished_at, r.run_id
FROM agent_run r JOIN stream st ON st.id = r.stream_id
WHERE r.id = %s AND r.source_id = %s
"""

_WINDOWS: Final = """
SELECT position, kind, window_from, window_to, status, window_id, complete
FROM agent_run_window WHERE agent_run_id = %s ORDER BY position
"""


async def _load_run(conn: Conn, found: _Source, run_id: UUID, *, lock: bool = False) -> AgentRun:
    """The agent run ``run_id`` of the source, its row locked with ``lock``. Inside a
    transaction."""
    cur = await conn.execute(_RUN + " FOR UPDATE OF r" if lock else _RUN, (run_id, found.id))
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("agent run")
    cur = await conn.execute(_WINDOWS, (run_id,))
    windows = tuple(
        AgentWindow(
            position=w[0],
            kind=w[1],
            start=w[2],
            end=w[3],
            status=w[4],
            window_id=w[5],
            complete=w[6],
        )
        for w in await cur.fetchall()
    )
    return AgentRun(
        id=row[0],
        workspace_id=row[1],
        owner_id=row[2],
        source_id=row[3],
        stream_id=row[4],
        workspace=found.workspace,
        source=found.name,
        stream=row[5],
        as_of=row[6],
        recipe_digest=row[7],
        server=ServerInfo(name=row[8], version=row[9]),
        plan=row[10],
        counts=row[11],
        started_at=row[12],
        closed_at=row[13],
        finished_at=row[14],
        run_id=row[15],
        windows=windows,
    )


# Adds each count of a JSON object to the run's counts, in one statement.
_ADD_COUNTS: Final = """
UPDATE agent_run SET counts = (
    SELECT jsonb_object_agg(k.key, coalesce((counts ->> k.key)::bigint, 0)
                                   + coalesce((%(add)s::jsonb ->> k.key)::bigint, 0))
    FROM (SELECT jsonb_object_keys(counts) UNION SELECT jsonb_object_keys(%(add)s::jsonb)) AS k(key)
)
WHERE id = %(id)s
"""


async def _add_counts(conn: Conn, run_id: UUID, counts: Mapping[str, int]) -> None:
    """Add ``counts`` to the session counts of the run."""
    found = {name: value for name, value in counts.items() if value}
    if found:
        await conn.execute(_ADD_COUNTS, {"add": Jsonb(found), "id": run_id})


async def _set_window(
    conn: Conn,
    run_id: UUID,
    position: int,
    status: WindowStatus,
    *,
    complete: bool | None = None,
) -> None:
    await conn.execute(
        "UPDATE agent_run_window SET status = %s, complete = coalesce(%s, complete)"
        " WHERE agent_run_id = %s AND position = %s",
        (status, complete, run_id, position),
    )


async def get_agent_run(
    conn: Conn, access: WriteAccess, workspace: str, source: str, run_id: UUID
) -> AgentRun:
    """The agent run ``run_id`` of the source.

    Raises:
        NotFoundError: no such workspace, source or run that ``access`` may change.
        NotAnMcpSourceError: the source is not an mcp source.
    """
    found = await _source(conn, access, workspace, source)
    async with conn.transaction():
        return await _load_run(conn, found, run_id)


# --- Start ---------------------------------------------------------------------------------------


def _server(recipe: Recipe, server: ServerInfo | None) -> tuple[ServerInfo, bool]:
    """The server the run records, and whether its version is one the recipe was checked
    against (true when it is not known)."""
    if server is None:
        return ServerInfo(name=recipe.server.name), True
    if server.name != recipe.server.name:
        raise ServerMismatchError(
            f"the session's server is not the one of the recipe {recipe.name}"
            f" ({recipe.server.name})"
        )
    return server, server.version is None or version_matches(server.version, recipe.server)


async def _open_window(conn: Conn, run: AgentRun, window: AgentWindow) -> AgentWindow:
    """Begin the raw window of ``window`` and mark it open. Inside the transaction that locked
    the run."""
    raw = await begin_window(
        conn,
        workspace_id=run.workspace_id,
        owner_id=run.owner_id,
        source_id=run.source_id,
        stream_id=run.stream_id,
        window=window.planned,
        recipe_digest=run.recipe_digest,
        server=run.server,
    )
    await conn.execute(
        "UPDATE agent_run_window SET status = 'open', window_id = %s"
        " WHERE agent_run_id = %s AND position = %s",
        (raw.id, run.id, window.position),
    )
    return replace(window, status="open", window_id=raw.id)


async def start_run(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    source: str,
    stream: str,
    *,
    server: ServerInfo | None = None,
) -> AgentRun:
    """Start an agent run of the stream ``stream``: pin the database time, plan the windows
    (:func:`memini.intake.plan_stream`) and open the first one.

    ``server`` is what the session's server reported, if the agent can tell; the recipe's server
    name otherwise. ``conn`` is idle: each step commits on its own.

    Raises:
        NotFoundError: no such workspace, source or stream that ``access`` may change.
        NotAnMcpSourceError: the source is not an mcp source.
        SourceNotReadyError: no valid recipe, no parser of its name, window settings that are not
            valid.
        ServerMismatchError: ``server`` names another server than the recipe.
        AgentRunOpenError: the stream has a run that is not finished.
    """
    _require_idle(conn)
    found = await _source(conn, access, workspace, source)
    recipe, _ = _recipe_and_parser(found)
    session_server, known_version = _server(recipe, server)
    try:
        settings = resolve_settings(recipe.window, window_overrides(found.config))
    except ValueError as error:
        raise SourceNotReadyError(f"the source's window settings are not valid: {error}") from None

    async with conn.transaction():
        cur = await conn.execute(
            "SELECT id, now() FROM stream WHERE source_id = %s AND key = %s", (found.id, stream)
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError("stream")
    stream_id: UUID = row[0]
    as_of: datetime = row[1]  # the database clock, pinned for the run
    plan = await plan_stream(conn, stream_id, settings, as_of)

    try:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO agent_run (workspace_id, owner_id, source_id, stream_id, as_of,"
                " recipe_digest, server_name, server_version, plan, counts)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    found.workspace_id,
                    access.owner_id,
                    found.id,
                    stream_id,
                    as_of,
                    recipe_digest(recipe),
                    session_server.name,
                    session_server.version,
                    Jsonb(plan.to_json()),
                    Jsonb({} if known_version else {"server_version_unchecked": 1}),
                ),
            )
            inserted = await cur.fetchone()
            if inserted is None:  # pragma: no cover - INSERT … RETURNING returns the row
                raise RuntimeError("the agent run row was not returned")
            run_id: UUID = inserted[0]
            for position, window in enumerate(plan.windows):
                await conn.execute(
                    "INSERT INTO agent_run_window (workspace_id, owner_id, source_id,"
                    " agent_run_id, position, kind, window_from, window_to)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        found.workspace_id,
                        access.owner_id,
                        found.id,
                        run_id,
                        position,
                        window.kind,
                        window.start,
                        window.end,
                    ),
                )
            run = await _load_run(conn, found, run_id, lock=True)
            if run.windows:
                await _open_window(conn, run, run.windows[0])
            run = await _load_run(conn, found, run_id)
    except errors.UniqueViolation as error:
        if error.diag.constraint_name != "agent_run_open_idx":
            raise
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id FROM agent_run WHERE stream_id = %s AND finished_at IS NULL",
                (stream_id,),
            )
            open_run = await cur.fetchone()
        raise AgentRunOpenError(None if open_run is None else open_run[0]) from None
    log.info("agent run %s started with %d windows", run.id, len(run.windows))
    return run


# --- Next call -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Issued:
    """A call handed out, as stored."""

    id: UUID
    window_id: UUID
    call: CallKind
    tool: str
    arguments: Mapping[str, Any]
    call_key: str
    page: int
    item_id: str | None
    check: Check
    reason: str

    def handed_out(self, attempt: int) -> HandedOutCall:
        return HandedOutCall(
            id=self.id,
            call=self.call,
            tool=self.tool,
            arguments=self.arguments,
            page=self.page,
            item_id=self.item_id,
            check=self.check,
            reason=self.reason,
            attempt=attempt,
        )


_CALL_COLUMNS: Final = (
    "id, window_id, call, tool, arguments, call_key, page, item_id, missing_check, reason"
)


def _issued(row: Sequence[Any]) -> _Issued:
    return _Issued(
        id=row[0],
        window_id=row[1],
        call=row[2],
        tool=row[3],
        arguments=row[4],
        call_key=row[5],
        page=row[6],
        item_id=row[7],
        check=row[8],
        reason=row[9],
    )


def _identity(call: CallKind, tool: str, arguments: Mapping[str, Any]) -> str:
    """A call's identity in its window, as the raw response store keys it: arguments redacted."""
    return call_key(call, tool, _redacted_arguments(arguments))


def _redacted_arguments(arguments: Mapping[str, Any]) -> bytes:
    """The canonical JSON of ``arguments`` as the store keeps them: secrets redacted."""
    try:
        canonical_json(arguments, field="arguments")
        return canonical_json(redact_json(arguments).value, field="arguments")
    except InvalidBatchError as error:
        raise InvalidResponseError(error.field, error.reason) from None
    except RedactionCollisionError:
        raise InvalidResponseError(
            "arguments", "two keys of an object are the same once secrets are redacted from them"
        ) from None


async def _attempts(conn: Conn, run_id: UUID, window_id: UUID, key: str) -> int:
    """How often the call ``key`` of the window was handed out in the run."""
    cur = await conn.execute(
        "SELECT coalesce(sum(handouts), 0) FROM agent_call"
        " WHERE agent_run_id = %s AND window_id = %s AND call_key = %s",
        (run_id, window_id, key),
    )
    row = await cur.fetchone()
    return 0 if row is None else int(row[0])


async def _handouts(conn: Conn, run_id: UUID) -> int:
    """How many calls the run handed out, again or not."""
    cur = await conn.execute(
        "SELECT coalesce(sum(handouts), 0) FROM agent_call WHERE agent_run_id = %s", (run_id,)
    )
    row = await cur.fetchone()
    return 0 if row is None else int(row[0])


def _window_of(run: AgentRun, window_id: UUID) -> AgentWindow:
    for window in run.windows:
        if window.window_id == window_id:
            return window
    raise WindowNotFoundError(f"agent run {run.id} has no raw window {window_id}")


async def _issue(
    conn: Conn, run: AgentRun, window_id: UUID, call: MissingCall, key: str
) -> _Issued:
    cur = await conn.execute(
        "INSERT INTO agent_call (workspace_id, owner_id, source_id, agent_run_id, window_id, call,"  # noqa: S608 - the columns are constant
        " tool, arguments, call_key, page, item_id, missing_check, reason)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        f" RETURNING {_CALL_COLUMNS}",
        (
            run.workspace_id,
            run.owner_id,
            run.source_id,
            run.id,
            window_id,
            call.call,
            call.tool,
            Jsonb(dict(call.arguments)),
            key,
            call.page,
            call.item_id,
            call.check,
            call.reason,
        ),
    )
    row = await cur.fetchone()
    if row is None:  # pragma: no cover - INSERT … RETURNING returns the row
        raise RuntimeError("the call row was not returned")
    return _issued(row)


async def _missing(
    conn: Conn, recipe: Recipe, run: AgentRun, window: AgentWindow
) -> tuple[MissingCall, ...] | None:
    """The calls the window misses, from its stored responses; ``None`` when it is gone."""
    if window.window_id is None:
        return None
    try:
        _, responses = await load_window(conn, window.window_id)
    except WindowNotFoundError:
        return None
    return missing_calls(WindowInput(recipe, run.stream, window.start, window.end, responses))


async def next_call(
    conn: Conn, access: WriteAccess, workspace: str, source: str, run_id: UUID
) -> NextStep:
    """The next call of the agent run ``run_id``, or what to do instead.

    A call handed out that got no response is handed out again under the same id, as long as that
    call of the window was handed out fewer than
    :data:`~memini.executors.agent.MAX_ATTEMPTS` times; then it is given up. Otherwise the current
    window's missing calls (:func:`memini.recipes.missing_calls` over its stored responses) give
    the next one: the first that is not past ``max_pages`` and was not handed out
    :data:`~memini.executors.agent.MAX_ATTEMPTS` times. A window with none left is done, and when
    it is not complete the rest of its chain is skipped. The next call after a done window opens
    the following one; with none left, the run is done. So is a run that handed out
    ``rate.max_calls`` calls.

    Raises:
        NotFoundError: no such workspace, source or run that ``access`` may change.
        NotAnMcpSourceError: the source is not an mcp source.
        SourceNotReadyError: the source's recipe or parser is gone.
        RecipeChangedError: the source's recipe is not the one the run was planned from.
        AgentRunClosedError: the run is finishing or finished.
    """
    _require_idle(conn)
    found = await _source(conn, access, workspace, source)
    recipe, _ = _recipe_and_parser(found)
    async with conn.transaction():
        run = await _load_run(conn, found, run_id, lock=True)
        if run.closed_at is not None:
            raise AgentRunClosedError(run.finished_at is not None)
        if recipe_digest(recipe) != run.recipe_digest:
            raise RecipeChangedError(
                "the source's recipe changed since the run started: finish it and start another"
            )
        return await _next(conn, found, recipe, run)


async def _next(conn: Conn, found: _Source, recipe: Recipe, run: AgentRun) -> NextStep:
    # A call handed out without a response: again, or given up.
    cur = await conn.execute(
        f"SELECT {_CALL_COLUMNS} FROM agent_call"  # noqa: S608 - the columns are constant
        " WHERE agent_run_id = %s AND answered_at IS NULL AND abandoned_at IS NULL",
        (run.id,),
    )
    row = await cur.fetchone()
    if row is not None:
        waiting = _issued(row)
        attempts = await _attempts(conn, run.id, waiting.window_id, waiting.call_key)
        if attempts < MAX_ATTEMPTS:
            await conn.execute(
                "UPDATE agent_call SET handouts = handouts + 1 WHERE id = %s", (waiting.id,)
            )
            await _add_counts(conn, run.id, {"calls_handed_out": 1, "calls_handed_out_again": 1})
            return NextStep(
                status="call",
                window=_window_of(run, waiting.window_id),
                call=waiting.handed_out(attempts + 1),
            )
        await conn.execute(
            "UPDATE agent_call SET abandoned_at = now() WHERE id = %s", (waiting.id,)
        )
        await _add_counts(conn, run.id, {"calls_abandoned": 1})

    window = next((w for w in run.windows if w.status == "open"), None)
    if window is None:
        waiting_window = next((w for w in run.windows if w.status == "pending"), None)
        if waiting_window is None:
            return NextStep(status="run_done", reason=_done_reason(run))
        window = await _open_window(conn, run, waiting_window)
        run = await _load_run(conn, found, run.id)

    if await _handouts(conn, run.id) >= recipe.rate.max_calls:
        await conn.execute(
            "UPDATE agent_run_window SET status = CASE status WHEN 'open' THEN 'called'"
            " ELSE 'skipped' END WHERE agent_run_id = %s AND status IN ('open', 'pending')",
            (run.id,),
        )
        await _add_counts(conn, run.id, {"budget_spent": 1})
        return NextStep(
            status="run_done",
            reason="the recipe's call budget (rate.max_calls) is spent; finish the run",
        )

    missing = await _missing(conn, recipe, run, window)
    if missing is not None and window.window_id is not None:
        for call in missing:
            if call.check == "max_pages":
                continue  # past the recipe's cap: the window stays incomplete
            key = _identity(call.call, call.tool, call.arguments)
            attempts = await _attempts(conn, run.id, window.window_id, key)
            if attempts >= MAX_ATTEMPTS:
                continue
            issued = await _issue(conn, run, window.window_id, call, key)
            await _add_counts(
                conn, run.id, {"calls_handed_out": 1, "calls_handed_out_again": int(attempts > 0)}
            )
            return NextStep(status="call", window=window, call=issued.handed_out(attempts + 1))

    # Nothing left to hand out: the window is done.
    complete = missing is not None and not missing
    await _set_window(conn, run.id, window.position, "called")
    if not complete:
        later = [
            w.position
            for w in run.windows
            if w.position > window.position and w.chain == window.chain and w.status == "pending"
        ]
        for position in later:
            await _set_window(conn, run.id, position, "skipped")
    await _add_counts(conn, run.id, {"windows_called": 1, "windows_gone": int(missing is None)})
    return NextStep(
        status="window_done",
        window=replace(window, status="called"),
        complete=complete,
        left=0 if missing is None else len(missing),
    )


def _done_reason(run: AgentRun) -> str:
    if run.windows:
        return "every window of the plan was called; finish the run"
    plan = run.plan
    why = plan.get("forward_skipped") or plan.get("backfill_skipped") or "nothing to load"
    return f"the plan has no window ({why}); finish the run"


# --- A posted response ---------------------------------------------------------------------------


async def submit_response(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    source: str,
    run_id: UUID,
    call_id: UUID,
    *,
    tool: str,
    arguments: Mapping[str, Any],
    result: object = None,
    error: str | None = None,
    max_size: int = MAX_RESPONSE_SIZE,
) -> Receipt:
    """Store the raw result of the call ``call_id`` the run handed out, as a capture hook posts it.

    ``tool`` and ``arguments`` are the call as the agent made it: they must be those of the call
    handed out under ``call_id`` (the arguments compared as the store keeps them, redacted).
    ``result`` is the tool's result as the hook got it, or ``error`` the message of a call that
    failed (:func:`~memini.executors.agent.mcp_result`). It is stored with
    :func:`memini.responses.store_response`, redacted and otherwise verbatim; the same result
    posted again writes nothing, another one replaces it (a retry).

    Raises:
        NotFoundError: no such workspace, source or run that ``access`` may change.
        NotAnMcpSourceError: the source is not an mcp source.
        AgentRunClosedError: the run is finishing or finished.
        CallNotHandedOutError: the run handed out no call ``call_id``.
        CallMismatchError: the tool or the arguments are not those of the call handed out.
        InvalidResultError: the result is not an MCP tool result.
        InvalidResponseError: the arguments or the result cannot be stored;
            :class:`~memini.responses.ResponseTooLargeError` when it is larger than ``max_size``.
        WindowClosedError: the call's window was parsed already.
    """
    _require_idle(conn)
    result_object = mcp_result(result, error)
    sent = _redacted_arguments(arguments)
    found = await _source(conn, access, workspace, source)
    async with conn.transaction():
        run = await _load_run(conn, found, run_id, lock=True)
        if run.closed_at is not None:
            raise AgentRunClosedError(run.finished_at is not None)
        cur = await conn.execute(
            f"SELECT {_CALL_COLUMNS} FROM agent_call"  # noqa: S608 - the columns are constant
            " WHERE id = %s AND agent_run_id = %s",
            (call_id, run.id),
        )
        row = await cur.fetchone()
        if row is None:
            raise CallNotHandedOutError("the agent run handed out no call with this id")
        issued = _issued(row)
        if tool != issued.tool:
            raise CallMismatchError("tool")
        if sent != _redacted_arguments(issued.arguments):
            raise CallMismatchError("arguments")
        stored = await store_response(
            conn,
            issued.window_id,
            call=issued.call,
            tool=issued.tool,
            arguments=issued.arguments,
            result=result_object,
            page=issued.page,
            item_id=issued.item_id,
            max_size=max_size,
        )
        await conn.execute(
            "UPDATE agent_call SET answered_at = coalesce(answered_at, now()) WHERE id = %s",
            (issued.id,),
        )
        cur = await conn.execute(
            "SELECT count(*) FROM raw_response WHERE window_id = %s", (issued.window_id,)
        )
        counted = await cur.fetchone()
        is_error = result_object.get("isError") is True
        counts: dict[str, int] = {
            "responses_stored" if stored.stored else "responses_unchanged": 1,
            "responses_failed": int(is_error and stored.stored),
            "raw_bodies_stored": int(stored.body_stored),
        }
        for kind, value in stored.redactions.items():
            counts[f"raw_redacted_{kind}"] = value
        await _add_counts(conn, run.id, counts)
    return Receipt(
        call_id=issued.id,
        window=_window_of(run, issued.window_id),
        position=stored.position,
        stored=stored.stored,
        is_error=is_error,
        responses=0 if counted is None else int(counted[0]),
        redacted=sum(stored.redactions.values()),
    )


# --- Finish --------------------------------------------------------------------------------------


async def _lock_source(conn: Conn, source_id: UUID, name: str) -> None:
    """The source's job lock for the session (a worker holds it while it runs a job of the
    source), taken in a transaction of its own so that the lock outlives it."""
    async with conn.transaction():
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s, %s)", source_lock_key(source_id))
        row = await cur.fetchone()
    if row is None or not row[0]:
        raise SourceBusyError(f"a job of source {name} is running; finish the run when it is done")


async def _unlock_source(conn: Conn, source_id: UUID) -> None:
    if conn.closed:
        return
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_unlock(%s, %s)", source_lock_key(source_id))
    except Exception:  # the session ends with the connection, and the lock with it
        log.warning("could not release the lock of the source after an agent run", exc_info=True)


async def finish_run(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    source: str,
    run_id: UUID,
    *,
    runs: _Database,
    limits: IngestLimits = DEFAULT_LIMITS,
    indexer: RecordIndexer | None = None,
) -> FinishResult:
    """Finish the agent run ``run_id``: close it to calls, then parse and ingest its windows in the
    order of the plan (:func:`memini.responses.ingest_window`), each chain stopping at its first
    incomplete window, and write the run report (kind ``agent_sync``) to ``runs``.

    The windows are parsed with the source's recipe as it is now, as replay does. A window never
    opened, skipped, or gone is not ingested. The run report holds the plan, the session's counts,
    a step per window ingested, and the incomplete windows with the calls that would complete them;
    it is ``partial`` unless every window of the plan was ingested complete. The source's job lock
    is held meanwhile. A finish that failed (a stale index, a model that failed) can be sent again:
    the windows it ingested are not ingested twice.

    Raises:
        NotFoundError: no such workspace, source or run that ``access`` may change.
        NotAnMcpSourceError: the source is not an mcp source.
        AgentRunClosedError: the run is finished already.
        SourceBusyError: a job of the source is running.
        SourceNotReadyError: the source's recipe or parser is gone.
        Exception: whatever :func:`memini.responses.ingest_window` raises.
    """
    _require_idle(conn)
    found = await _source(conn, access, workspace, source)
    recipe, parser = _recipe_and_parser(found)
    await _lock_source(conn, found.id, found.name)
    try:
        async with conn.transaction():
            run = await _load_run(conn, found, run_id, lock=True)
            if run.finished_at is not None:
                raise AgentRunClosedError(True)
            await conn.execute(
                "UPDATE agent_run SET closed_at = coalesce(closed_at, now()) WHERE id = %s",
                (run.id,),
            )
            run = await _load_run(conn, found, run_id)
        return await _finish(conn, access, found, run, recipe, parser, runs, limits, indexer)
    finally:
        await _unlock_source(conn, found.id)


async def _finish(
    conn: Conn,
    access: WriteAccess,
    found: _Source,
    run: AgentRun,
    recipe: Recipe,
    parser: WindowParser,
    runs: _Database,
    limits: IngestLimits,
    indexer: RecordIndexer | None,
) -> FinishResult:
    outcomes: list[WindowOutcome] = []
    async with record_run(
        runs,
        workspace_id=run.workspace_id,
        owner_id=run.owner_id,
        kind=RUN_KIND,
        source_id=run.source_id,
        stream_id=run.stream_id,
        progress_interval=None,
    ) as report:
        report.detail(PLAN_DETAIL, cast(JsonValue, dict(run.plan)))
        report.detail(
            AGENT_RUN_DETAIL,
            {
                "id": str(run.id),
                "as_of": run.as_of.isoformat(),
                "server": run.server.name,
                "server_version": run.server.version,
                "recipe_digest": run.recipe_digest,
            },
        )
        for name, value in sorted(run.counts.items()):
            report.count(name, value)
        opened = [w.window_id for w in run.windows if w.window_id is not None]
        if opened:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE raw_window SET run_id = %s WHERE source_id = %s AND id = ANY(%s)",
                    (report.id, run.source_id, opened),
                )

        stopped: set[str] = set()
        for window in run.windows:
            if window.status == "ingested":  # by a finish that failed later
                outcomes.append(WindowOutcome(window, None))
                if not window.complete:
                    stopped.add(window.chain)
                continue
            raw_id = window.window_id
            if window.status in ("pending", "skipped") or raw_id is None or window.chain in stopped:
                if raw_id is None and window.status in ("open", "called"):
                    report.count("windows_gone")  # deleted meanwhile
                else:
                    report.count("windows_not_run")
                stopped.add(window.chain)  # a later window of the chain would leave a gap
                async with conn.transaction():
                    await _set_window(conn, run.id, window.position, "skipped")
                outcomes.append(WindowOutcome(replace(window, status="skipped"), None))
                continue
            try:
                async with report.step(window.planned.label):
                    done = await ingest_window(
                        conn,
                        access,
                        raw_id,
                        recipe=recipe,
                        parser=parser,
                        limits=limits,
                        indexer=indexer,
                    )
            except WindowNotFoundError:
                report.count("windows_gone")
                stopped.add(window.chain)
                outcomes.append(WindowOutcome(replace(window, status="skipped"), None))
                async with conn.transaction():
                    await _set_window(conn, run.id, window.position, "skipped")
                continue
            async with conn.transaction():
                await _set_window(conn, run.id, window.position, "ingested", complete=done.complete)
            report.count("windows_ingested")
            if done.complete:
                report.count("windows_complete")
            else:
                stopped.add(window.chain)
            outcomes.append(
                WindowOutcome(replace(window, status="ingested", complete=done.complete), done)
            )

        if any(outcome.window.complete is not True for outcome in outcomes):
            report.set_outcome(
                "partial",
                "not every window was ingested complete; the checkpoint stays behind the others",
            )
        async with conn.transaction():
            await conn.execute("DELETE FROM agent_call WHERE agent_run_id = %s", (run.id,))
            await conn.execute(
                "UPDATE agent_run SET finished_at = now(), run_id = %s WHERE id = %s",
                (report.id, run.id),
            )
            cur = await conn.execute(
                "SELECT checked_until FROM stream WHERE id = %s", (run.stream_id,)
            )
            row = await cur.fetchone()
            finished = await _load_run(conn, found, run.id)
    log.info("agent run %s finished", run.id)
    return FinishResult(
        agent_run=finished,
        run_id=report.id,
        windows=tuple(outcomes),
        checked_until=None if row is None else row[0],
    )
