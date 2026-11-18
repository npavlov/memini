"""The agent executor's routes: an intake of an mcp source driven from an interactive agent session
(:mod:`memini.executors.agent`). Under ``/v1/workspaces/{workspace}/sources/{source}/agent-runs``:

- ``POST`` starts a run of a stream: pins the database time, plans the windows, opens the first
  one. 201 with the run. A stream with a run not finished: 409 ``agent_run_open``, whose message
  names that run, to finish first.
- ``GET /{id}`` reads a run: its state, plan, windows and the counts of the session.
- ``GET /{id}/next`` is the next step: ``call``, a call to make exactly as given (the tool's name
  at the server and its arguments) with the id to post its response under; ``window_done``, the
  current window has nothing more to hand out (the next request opens the following one); or
  ``run_done``, finish the run. A call that got no response is handed out again under the same id.
- ``POST /{id}/responses`` takes the raw result of a call handed out: its id, the tool and the
  arguments as the agent sent them (they must be the call's: 409 ``call_mismatch`` otherwise),
  and the ``result`` as the agent's client got it (an MCP result object, its list of content
  blocks, or a text), or the ``error`` of a call that failed. A call the run did not hand out:
  409 ``call_not_handed_out``. Nothing is stored for a refused response. The answer is a receipt:
  positions and counts, never a value of the response. Its body may be as large as
  ``ingest_max_body_size``, as a batch's.
- ``POST /{id}/finish`` closes the run to calls, parses and ingests its windows, and writes the run
  report (``GET /v1/runs/{run_id}``). A job of the source running: 409 ``source_busy``, send it
  again later.

Every route needs the ``ingest`` scope and keeps a token limited to one workspace inside it: a
workspace, source, stream or run outside it is not found (404). A source that is not an mcp
source: 409 ``not_mcp_source``; one without a valid recipe or a registered parser: 409
``source_not_ready``. Each ``POST`` counts against the token's write budget, ``next`` against its
reads (:mod:`memini.api.limits`).
"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from memini import ingest
from memini.api.auth import IngestAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.api.ingest import BatchRoute, ingest_errors
from memini.config import Settings
from memini.db import Pool
from memini.executors import agent
from memini.query import QueryServices
from memini.recipes import ParserError
from memini.responses import (
    InvalidResponseError,
    NotAnMcpSourceError,
    ReplayError,
    ResponseTooLargeError,
    ServerInfo,
    WindowClosedError,
    WindowNotFoundError,
)
from memini.sources import SourceBusyError
from memini.workspaces import NotFoundError

__all__ = [
    "AgentRunInfo",
    "CallInfo",
    "FinishAnswer",
    "NextAnswer",
    "ReceiptAnswer",
    "ResponseBody",
    "StartBody",
    "WindowInfo",
    "build_agent_runs_router",
]

_MAX_TEXT = 1_000_000


# --- Bodies --------------------------------------------------------------------------------------


class ServerBody(BaseModel):
    """What the session's server reported when it started (``serverInfo``)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256, description="serverInfo.name.")
    version: str | None = Field(
        default=None, min_length=1, max_length=128, description="serverInfo.version."
    )


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream: str = Field(
        min_length=1, max_length=1024, description="The key of one of the source's streams."
    )
    server: ServerBody | None = Field(
        default=None,
        description="The session's server, if the agent can tell: another name than the recipe's"
        " is refused. Left out: the recipe's server, version unknown.",
    )


class ResponseBody(BaseModel):
    """The raw result of a call handed out, as the agent's client got it."""

    model_config = ConfigDict(extra="forbid")

    call_id: UUID = Field(description="The id the call was handed out with.")
    tool: str = Field(
        min_length=1, max_length=128, description="The tool's name at the server, as handed out."
    )
    arguments: dict[str, Any] = Field(description="The arguments the call was made with.")
    result: JsonValue = Field(
        default=None,
        description="The tool's result: an MCP result object (content, structuredContent,"
        " isError), the list of its content blocks, or its text. Stored redacted, otherwise"
        " verbatim.",
    )
    error: str | None = Field(
        default=None,
        max_length=_MAX_TEXT,
        description="Instead of result: the error of a call that failed. The call is then handed"
        " out again, a few times.",
    )


# --- Answers -------------------------------------------------------------------------------------


class WindowInfo(BaseModel):
    position: int = Field(description="The window's place in the plan, from 0.")
    kind: Literal["initial", "forward", "backfill"]
    start: datetime = Field(serialization_alias="from")
    end: datetime = Field(serialization_alias="to")
    status: agent.WindowStatus
    complete: bool | None = Field(description="Its claim once finish ingested it.")

    @classmethod
    def of(cls, window: agent.AgentWindow) -> "WindowInfo":
        return cls(
            position=window.position,
            kind=window.kind,
            start=window.start,
            end=window.end,
            status=window.status,
            complete=window.complete,
        )


class AgentRunInfo(BaseModel):
    id: UUID
    workspace: str
    source: str
    stream: str
    state: Literal["open", "finishing", "finished"]
    as_of: datetime = Field(description="The database time pinned for the run.")
    server: str
    server_version: str | None
    recipe_digest: str
    started_at: datetime
    closed_at: datetime | None
    finished_at: datetime | None
    run_id: UUID | None = Field(description="The run report, once finished: GET /v1/runs/{id}.")
    plan: dict[str, JsonValue]
    windows: list[WindowInfo]
    counts: dict[str, int] = Field(description="Calls handed out, responses stored, and so on.")

    @classmethod
    def of(cls, run: agent.AgentRun) -> "AgentRunInfo":
        return cls(
            id=run.id,
            workspace=run.workspace,
            source=run.source,
            stream=run.stream,
            state=run.state,
            as_of=run.as_of,
            server=run.server.name,
            server_version=run.server.version,
            recipe_digest=run.recipe_digest,
            started_at=run.started_at,
            closed_at=run.closed_at,
            finished_at=run.finished_at,
            run_id=run.run_id,
            plan=dict(run.plan),
            windows=[WindowInfo.of(window) for window in run.windows],
            counts=dict(run.counts),
        )


class CallInfo(BaseModel):
    id: UUID = Field(description="Post the call's response under this id.")
    call: Literal["list", "detail"]
    tool: str = Field(description="The tool's name at the server.")
    arguments: dict[str, Any] = Field(description="Send exactly these, unchanged.")
    page: int
    item_id: str | None
    check: str = Field(description="The completeness check the call makes pass.")
    reason: str = Field(description="Why the call is needed, by position.")
    attempt: int = Field(description="1 the first time this call of the window is handed out.")

    @classmethod
    def of(cls, call: agent.HandedOutCall) -> "CallInfo":
        return cls(
            id=call.id,
            call=call.call,
            tool=call.tool,
            arguments=dict(call.arguments),
            page=call.page,
            item_id=call.item_id,
            check=call.check,
            reason=call.reason,
            attempt=call.attempt,
        )


class NextAnswer(BaseModel):
    status: agent.NextStatus = Field(
        description="call: make the call. window_done: ask again for the next window."
        " run_done: finish the run."
    )
    call: CallInfo | None = None
    window: WindowInfo | None = None
    complete: bool | None = Field(
        default=None,
        description="window_done: whether the window passes every check, as far as the stored"
        " responses tell before the parser runs.",
    )
    left: int = Field(
        default=0, description="window_done: calls the window misses that are not handed out."
    )
    reason: str | None = Field(default=None, description="run_done: why.")

    @classmethod
    def of(cls, step: agent.NextStep) -> "NextAnswer":
        return cls(
            status=step.status,
            call=None if step.call is None else CallInfo.of(step.call),
            window=None if step.window is None else WindowInfo.of(step.window),
            complete=step.complete,
            left=step.left,
            reason=step.reason,
        )


class ReceiptAnswer(BaseModel):
    """What storing the response did; never a value of it."""

    call_id: UUID
    window: WindowInfo
    position: int = Field(description="The response's place in its window, from 1.")
    stored: bool = Field(description="False: the window held the same result for the call.")
    is_error: bool = Field(description="The call failed; it is handed out again, a few times.")
    responses: int = Field(description="Responses the window holds now.")
    redacted: int = Field(description="Values redacted from the result and the arguments.")

    @classmethod
    def of(cls, receipt: agent.Receipt) -> "ReceiptAnswer":
        return cls(
            call_id=receipt.call_id,
            window=WindowInfo.of(receipt.window),
            position=receipt.position,
            stored=receipt.stored,
            is_error=receipt.is_error,
            responses=receipt.responses,
            redacted=receipt.redacted,
        )


class WindowResult(BaseModel):
    window: WindowInfo
    ingested: bool = Field(description="False: never opened, skipped or gone.")
    records: int = 0
    inserted: int = 0
    updated: int = 0
    revived: int = 0
    deleted: int = 0
    unchanged: int = 0
    reconciled: int = 0
    checkpoint_moved: bool = False
    incomplete: list[str] = Field(default_factory=list[str], description="Why it is incomplete.")
    missing_calls: int = Field(default=0, description="Calls that would complete it.")

    @classmethod
    def of(cls, outcome: agent.WindowOutcome) -> "WindowResult":
        done = outcome.ingest
        info = WindowInfo.of(outcome.window)
        if done is None:
            return cls(window=info, ingested=False)
        return cls(
            window=info,
            ingested=True,
            records=done.records,
            inserted=done.inserted,
            updated=done.updated,
            revived=done.revived,
            deleted=done.deleted,
            unchanged=done.unchanged,
            reconciled=done.reconciled,
            checkpoint_moved=done.checkpoint_moved,
            incomplete=list(done.incomplete),
            missing_calls=len(done.missing),
        )


class FinishAnswer(BaseModel):
    agent_run: AgentRunInfo
    run_id: UUID = Field(description="The run report: GET /v1/runs/{id}.")
    windows: list[WindowResult]
    checked_until: datetime | None = Field(description="The stream's checkpoint now.")

    @classmethod
    def of(cls, result: agent.FinishResult) -> "FinishAnswer":
        return cls(
            agent_run=AgentRunInfo.of(result.agent_run),
            run_id=result.run_id,
            windows=[WindowResult.of(outcome) for outcome in result.windows],
            checked_until=result.checked_until,
        )


# --- Errors --------------------------------------------------------------------------------------


@contextmanager
def _agent_errors() -> Generator[None]:
    try:
        yield
    except NotFoundError as error:
        raise ApiError(404, "not_found", f"No such {error.what}.") from None
    except NotAnMcpSourceError:
        raise ApiError(
            409, "not_mcp_source", "Agent runs are for mcp sources; this source is not one."
        ) from None
    except agent.SourceNotReadyError as error:
        raise ApiError(409, "source_not_ready", f"The source cannot run: {error}.") from None
    except agent.ServerMismatchError as error:
        raise ApiError(409, "server_mismatch", f"{_sentence(error)}.") from None
    except agent.AgentRunOpenError as error:
        raise ApiError(409, "agent_run_open", f"{_sentence(error)}.") from None
    except agent.AgentRunClosedError as error:
        code = "agent_run_finished" if error.finished else "agent_run_finishing"
        raise ApiError(409, code, f"{_sentence(error)}.") from None
    except agent.RecipeChangedError as error:
        raise ApiError(409, "recipe_changed", f"{_sentence(error)}.") from None
    except agent.CallNotHandedOutError as error:
        raise ApiError(409, "call_not_handed_out", f"{_sentence(error)}; nothing stored.") from None
    except agent.CallMismatchError as error:
        raise ApiError(409, "call_mismatch", f"{_sentence(error)}; nothing stored.") from None
    except agent.InvalidResultError as error:
        raise ApiError(422, "invalid_result", f"{_sentence(error)}.") from None
    except ResponseTooLargeError as error:
        raise ApiError(413, "response_too_large", f"The response is too large: {error}.") from None
    except InvalidResponseError as error:
        raise ApiError(
            422, "invalid_response", f"The response cannot be stored: {error}."
        ) from None
    except WindowNotFoundError:
        raise ApiError(
            409, "window_gone", "The call's window was deleted; finish the run and start another."
        ) from None
    except WindowClosedError:
        raise ApiError(409, "window_closed", "The call's window was parsed already.") from None
    except SourceBusyError as error:
        raise ApiError(409, "source_busy", f"{_sentence(error)}.") from None
    except ReplayError as error:
        raise ApiError(409, "recipe_mismatch", f"{_sentence(error)}.") from None
    except ParserError as error:
        raise ApiError(409, "parser_failed", f"The parser failed: {error}.") from None


def _sentence(error: Exception) -> str:
    text = str(error)
    return text[:1].upper() + text[1:]


# --- Routes --------------------------------------------------------------------------------------


def build_agent_runs_router() -> APIRouter:
    """The agent executor's routes; see the module."""
    router = APIRouter(
        prefix="/workspaces/{workspace}/sources/{source}/agent-runs",
        tags=["agent runs"],
        route_class=BatchRoute,
    )

    @router.post("", status_code=201, summary="Start an agent run of a stream")
    async def start_agent_run(
        workspace: str, source: str, body: StartBody, access: IngestAccess, conn: Connection
    ) -> AgentRunInfo:
        """Pin the database time, plan the stream's windows and open the first one."""
        server = None if body.server is None else ServerInfo(body.server.name, body.server.version)
        with _agent_errors():
            run = await agent.start_run(conn, access, workspace, source, body.stream, server=server)
        return AgentRunInfo.of(run)

    @router.get("/{run_id}", summary="Read an agent run")
    async def read_agent_run(
        workspace: str, source: str, run_id: UUID, access: IngestAccess, conn: Connection
    ) -> AgentRunInfo:
        with _agent_errors():
            run = await agent.get_agent_run(conn, access, workspace, source, run_id)
        return AgentRunInfo.of(run)

    @router.get("/{run_id}/next", summary="The next call of an agent run")
    async def next_agent_call(
        workspace: str, source: str, run_id: UUID, access: IngestAccess, conn: Connection
    ) -> NextAnswer:
        """The next call to make, or that the window or the run is done."""
        with _agent_errors():
            step = await agent.next_call(conn, access, workspace, source, run_id)
        return NextAnswer.of(step)

    @router.post("/{run_id}/responses", summary="Store the raw response of a call handed out")
    async def store_agent_response(
        workspace: str,
        source: str,
        run_id: UUID,
        body: ResponseBody,
        access: IngestAccess,
        conn: Connection,
    ) -> ReceiptAnswer:
        """Store the result of a call this run handed out, redacted and otherwise verbatim. The
        answer counts; it never repeats the response."""
        with _agent_errors():
            receipt = await agent.submit_response(
                conn,
                access,
                workspace,
                source,
                run_id,
                body.call_id,
                tool=body.tool,
                arguments=body.arguments,
                result=body.result,
                error=body.error,
            )
        return ReceiptAnswer.of(receipt)

    @router.post("/{run_id}/finish", summary="Finish an agent run")
    async def finish_agent_run(
        workspace: str,
        source: str,
        run_id: UUID,
        access: IngestAccess,
        request: Request,
        conn: Connection,
    ) -> FinishAnswer:
        """Close the run to calls, parse and ingest its windows, write the run report."""
        settings = cast(Settings, request.app.state.settings)
        pool = cast(Pool, request.app.state.pool)
        embedder = cast(QueryServices, request.app.state.services).embedder
        indexer = None if embedder is None else ingest.PayloadIndexer(embedder)
        with _agent_errors(), ingest_errors():
            result = await agent.finish_run(
                conn,
                access,
                workspace,
                source,
                run_id,
                runs=pool,
                limits=ingest.IngestLimits.from_settings(settings),
                indexer=indexer,
            )
        return FinishAnswer.of(result)

    return router
