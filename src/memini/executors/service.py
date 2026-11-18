"""The ``mcp_sync`` job: the service executor of MCP recipes (requirements, section 4, MCP recipes;
docs/recipes.md, How the executors use a recipe).

One job syncs one stream of an mcp source, the job's ``stream_id``, as the scheduler and a triggered
sync enqueue it; a job without a stream syncs every stream of its source. The worker runs one job
per source at a time. The steps, in the job's run report:

1. ``connect``: read the source (its endpoint, its recipe and the recipe's parser,
   :func:`memini.executors.core.source_recipe`), its credential, and open a session
   (:func:`memini.executors.client.open_session`): the egress policy is checked before anything
   connects. The server's name, version and tools are checked against the recipe
   (:func:`memini.executors.core.check_drift`, ``details.drift``): a drift error stops the run
   before any call. ``as_of`` is pinned in the job's args (:func:`memini.intake.pin_as_of`), so a
   retried attempt plans the same windows.
2. One step per window of the plan (``details.plan``, :func:`memini.intake.plan_stream` with the
   source's window overrides): ``begin_window``; the calls :func:`memini.executors.core.
   calls_to_make` names, made up to ``rate.concurrency`` at once, each result stored redacted
   before anything reads it; again, until nothing is left to make; then ``ingest_window``. Each
   chain (forward, history) stops at its first incomplete window, which the report lists with the
   calls that would complete it (``details.incomplete_windows``).
3. ``retention``, for a source with ``retention_days``: its expired records go
   (:func:`memini.removal.expire_source`), as after a git sync.

**Budget and retries.** Every call, a retry included, takes one call of the recipe's ``[rate]``
budget (:class:`~memini.executors.budget.Budget`): at most ``calls_per_minute`` a minute and
``max_calls`` in the run. A call that times out, fails on the network, gets HTTP 429 or a server
error, or answers ``isError``, is tried again after a backoff, up to ``mcp_call_attempts`` tries
in the window; a 429 pauses every call for the ``Retry-After`` the server asked (capped). A call
that is out of tries stays missing: the window is incomplete, its checkpoint stays, and the next
run plans it again. Once ``max_calls`` are spent the run makes no more calls: the window in hand is
ingested incomplete, the windows after it wait for the next run, and the run is ``partial``.

**Credentials.** The source's credential (:mod:`memini.credentials`) is read for the run: an API
key, or OAuth tokens refreshed first when they expire soon (:func:`memini.credentials.fresh_oauth`
with :func:`memini.executors.oauth.token_refresher`). It is sent as a bearer token (HTTP) or in the
environment variable the source names (stdio), never logged. A 401 refreshes OAuth tokens once
(:func:`memini.credentials.refresh_oauth`) and the call goes on; a 401 without a refresh, or after
one, ends the run, as does a connection that ended: the window in hand is ingested incomplete
first, and the job fails, to be tried again.

**Without an embedding provider** records are stored without their chunks, as the push API stores
them (``records_not_indexed``), and ``memini reindex`` indexes them once a provider is set: every
record of an mcp sync is written through ingest, which the index catches up with, unlike a git
sync, which writes only what changed. So the handler runs with or without one.
"""

import logging
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, nullcontext
from dataclasses import dataclass, field
from typing import Any, Final, Self
from uuid import UUID

import anyio
import httpx2
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from memini.auth import WriteAccess
from memini.config import Settings
from memini.credentials import (
    Keyring,
    Refresher,
    get_credential_info,
    read_api_key,
    refresh_oauth,
)
from memini.credentials import fresh_oauth as fresh_oauth_tokens
from memini.db import Pool
from memini.executors.budget import Budget, RetryPolicy
from memini.executors.client import CallFailure, McpSession, open_session
from memini.executors.core import (
    DriftError,
    call_identity,
    calls_to_make,
    check_drift,
    source_recipe,
    window_settings,
)
from memini.executors.egress import EgressPolicy
from memini.executors.oauth import token_refresher
from memini.executors.source import McpConfig, McpConfigError, check_endpoint
from memini.executors.transport import Resolver, system_resolver
from memini.extraction.job import extraction_enabled
from memini.ingest import DEFAULT_LIMITS, IngestLimits, RecordIndexer
from memini.intake import Plan, PlannedWindow, pin_as_of, plan_stream, record_plan
from memini.jobs import SYNC_KINDS, Handler, Job
from memini.recipes import MissingCall, Recipe, WindowInput, WindowParser, recipe_digest
from memini.removal import DEFAULT_RETENTION_BATCH, expire_source
from memini.responses import (
    InvalidResponseError,
    ServerInfo,
    WindowIngest,
    WindowNotFoundError,
    begin_window,
    ingest_window,
    load_window,
    store_response,
)
from memini.runs import Run, current_run

__all__ = [
    "MCP_SYNC",
    "ServiceOptions",
    "StreamSync",
    "SyncStopped",
    "mcp_sync_handler",
    "run_mcp_sync",
]

log = logging.getLogger(__name__)

type Conn = AsyncConnection[TupleRow]

MCP_SYNC: Final = SYNC_KINDS["mcp"]
"""The job kind of an mcp sync: ``mcp_sync``."""

BUDGET_DETAIL: Final = "budget"
DRIFT_DETAIL: Final = "drift"


class SyncStopped(Exception):
    """The run could not go on: the server refused the credential, or the connection ended. The
    window in hand was ingested incomplete first. The message names the failure, never a value."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceOptions:
    """How the service executor runs; :meth:`from_settings` for the service, tests set the rest."""

    policy: EgressPolicy = field(default_factory=EgressPolicy)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    limits: IngestLimits = DEFAULT_LIMITS
    retention_batch: int = DEFAULT_RETENTION_BATCH
    extract: bool = False
    """Enqueue decision extraction for new or changed threads and meetings (an LLM provider is
    configured: :func:`memini.extraction.job.extraction_enabled`)."""
    http_transport: Callable[[], httpx2.AsyncBaseTransport] | None = None
    """Makes the transport that reaches the network, instead of the network: tests serve an
    in-process MCP server with it. The egress guard still checks every request."""
    resolver: Resolver = system_resolver
    clock: Callable[[], float] | None = None
    """The budget's clock (seconds, monotonic); ``None``: :func:`time.monotonic`."""
    sleep: Callable[[float], Any] | None = None
    """How the budget and the backoff wait; ``None``: :func:`anyio.sleep`."""

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            policy=EgressPolicy.from_settings(settings),
            retry=RetryPolicy(attempts=settings.mcp_call_attempts),
            limits=IngestLimits.from_settings(settings),
            retention_batch=settings.retention_batch_size,
            extract=extraction_enabled(settings),
        )


@dataclass(frozen=True, slots=True)
class StreamSync:
    """What a sync did with one stream."""

    stream: str
    plan: Plan
    windows: tuple[WindowIngest, ...]
    """The windows that ran, in order; a chain's last one is incomplete if it stopped there."""


@dataclass(frozen=True, slots=True)
class _Source:
    id: UUID
    workspace_id: UUID
    owner_id: UUID
    config: McpConfig
    retention: bool


@dataclass(frozen=True, slots=True)
class _Stream:
    id: UUID
    key: str


def mcp_sync_handler(
    pool: Pool,
    *,
    keyring: Keyring,
    indexer: RecordIndexer | None = None,
    options: ServiceOptions | None = None,
) -> Handler:
    """The handler of ``mcp_sync`` jobs: :func:`run_mcp_sync` with these settings."""
    chosen = options or ServiceOptions()

    async def handle(job: Job) -> None:
        await run_mcp_sync(pool, job, keyring=keyring, indexer=indexer, options=chosen)

    return handle


async def _load(
    conn: Conn, source_id: UUID, stream_id: UUID | None
) -> tuple[_Source, list[_Stream]]:
    cur = await conn.execute(
        "SELECT workspace_id, owner_id, kind, config, retention_days IS NOT NULL"
        " FROM source WHERE id = %s",
        (source_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"no source {source_id}")
    workspace_id, owner_id, kind, config, retention = row
    if kind != "mcp":
        raise ValueError(f"source {source_id} is a {kind} source, not an mcp source")
    if stream_id is None:
        cur = await conn.execute(
            "SELECT id, key FROM stream WHERE source_id = %s ORDER BY key", (source_id,)
        )
    else:
        cur = await conn.execute(
            "SELECT id, key FROM stream WHERE source_id = %s AND id = %s", (source_id, stream_id)
        )
    streams = [_Stream(id=found[0], key=found[1]) for found in await cur.fetchall()]
    if stream_id is not None and not streams:
        raise LookupError(f"no stream {stream_id} in source {source_id}")
    source = _Source(
        source_id, workspace_id, owner_id, McpConfig.from_json(config), bool(retention)
    )
    return source, streams


async def run_mcp_sync(
    pool: Pool,
    job: Job,
    *,
    keyring: Keyring,
    indexer: RecordIndexer | None = None,
    options: ServiceOptions | None = None,
) -> list[StreamSync]:
    """Sync the stream of ``job`` (or every stream of its source), then pass retention over the
    source; see the module.

    Raises:
        LookupError: the job's source or stream does not exist.
        ValueError: the source is not an mcp source.
        McpConfigError: its endpoint is not allowed any more, or its recipe not valid.
        ParserNotFoundError: this build has no parser of the recipe.
        EgressRefused: a connection the egress policy does not allow; nothing was sent.
        ConnectError: the session could not be opened.
        DriftError: the server drifted from the recipe; no call was made.
        SyncStopped: the server refused the credential, or the connection ended.
        CredentialError: the source's credential cannot be read or refreshed.
        Exception: whatever ingest raises (an index to rebuild first, a model that failed).
    """
    chosen = options or ServiceOptions()
    if job.source_id is None:
        raise ValueError(f"{MCP_SYNC} job {job.id} names no source")
    async with pool.connection() as conn, conn.transaction():
        source, streams = await _load(conn, job.source_id, job.stream_id)
    run = current_run()
    async with pool.connection() as conn:
        sync = _Sync(pool, conn, job, source, keyring, indexer, chosen, run)
        return await sync.run(streams)


class _Sync:
    """One run of the handler: its session, its budget, its credential."""

    def __init__(
        self,
        pool: Pool,
        conn: Conn,
        job: Job,
        source: _Source,
        keyring: Keyring,
        indexer: RecordIndexer | None,
        options: ServiceOptions,
        run: Run | None,
    ) -> None:
        self._pool = pool
        self._conn = conn
        self._job = job
        self._source = source
        self._keyring = keyring
        self._indexer = indexer
        self._options = options
        self._run = run
        self._access = WriteAccess.for_owner(source.owner_id)
        self._store_lock = anyio.Lock()
        self._token: SecretStr | None = None
        self._oauth_generation: int | None = None
        self._refreshed = False
        self._stopped: CallFailure | None = None
        self._budget_spent = False
        self._window_gone = False
        # Set by run() once the session is open.
        self._session: McpSession | None = None
        self._recipe: Recipe | None = None
        self._parser: WindowParser | None = None
        self._budget: Budget | None = None
        self._limiter: anyio.CapacityLimiter | None = None

    # --- Reporting -------------------------------------------------------------------------------

    def _count(self, name: str, n: int = 1) -> None:
        if self._run is not None:
            self._run.count(name, n)

    def _step(self, name: str) -> Any:
        return self._run.step(name) if self._run is not None else nullcontext()

    # --- The credential --------------------------------------------------------------------------

    async def _read_credential(self) -> None:
        source_id = self._source.id
        async with self._pool.connection() as conn:
            async with conn.transaction():  # a read that ends: fresh_oauth needs an idle conn
                info = await get_credential_info(conn, source_id=source_id)
                if info is not None and info.kind == "api_key":
                    key = await read_api_key(conn, self._keyring, source_id=source_id)
                    self._token = key.api_key
            if info is None or info.kind == "api_key":
                return
            oauth = await fresh_oauth_tokens(
                conn, self._keyring, source_id=source_id, refresher=self._refresher()
            )
        self._token = oauth.tokens.access_token
        self._oauth_generation = oauth.info.generation

    def _refresher(self) -> Refresher:
        options = self._options
        transport = None if options.http_transport is None else options.http_transport()
        return token_refresher(options.policy, http_transport=transport, resolver=options.resolver)

    async def _refresh_after_401(self) -> bool:
        """Refresh OAuth tokens the server refused, once a run; whether there are new ones."""
        if self._oauth_generation is None or self._refreshed or self._session is None:
            return False
        self._refreshed = True
        async with self._pool.connection() as conn:
            oauth = await refresh_oauth(
                conn,
                self._keyring,
                source_id=self._source.id,
                refresher=self._refresher(),
                seen_generation=self._oauth_generation,
            )
        self._token = oauth.tokens.access_token
        self._oauth_generation = oauth.info.generation
        self._session.use_token(self._token)
        self._count("oauth_refreshed")
        return True

    # --- The run ---------------------------------------------------------------------------------

    async def run(self, streams: Sequence[_Stream]) -> list[StreamSync]:
        config = self._source.config
        options = self._options
        if config.endpoint is None:
            raise McpConfigError("endpoint", "the source has no endpoint")
        results: list[StreamSync] = []
        async with AsyncExitStack() as stack:
            async with self._step("connect"):
                endpoint = check_endpoint(config.endpoint, options.policy)
                recipe, parser = source_recipe(config)
                self._recipe, self._parser = recipe, parser
                as_of = await pin_as_of(self._conn, self._job.id)
                await self._read_credential()
                session = await stack.enter_async_context(
                    open_session(
                        endpoint,
                        policy=options.policy,
                        timeout=recipe.rate.timeout.total_seconds(),
                        token=self._token,
                        http_transport=None
                        if options.http_transport is None
                        else options.http_transport(),
                        resolver=options.resolver,
                        max_connections=recipe.rate.concurrency,
                    )
                )
                self._session = session
                drift = check_drift(recipe, session.server, session.tools)
                if self._run is not None:
                    self._run.detail(DRIFT_DETAIL, drift.to_json())
                for warning in drift.warnings:
                    log.warning("mcp source %s: %s", self._source.id, warning)
                if not drift.ok:
                    raise DriftError(drift)
            budget_options: dict[str, Any] = {}
            if options.clock is not None:
                budget_options["clock"] = options.clock
            if options.sleep is not None:
                budget_options["sleep"] = options.sleep
            self._budget = Budget(recipe.rate, **budget_options)
            self._limiter = anyio.CapacityLimiter(recipe.rate.concurrency)
            settings = window_settings(recipe, config)
            plans: dict[str, Any] = {}
            for stream in streams:
                plan = await plan_stream(self._conn, stream.id, settings, as_of)
                if self._run is not None:
                    if len(streams) == 1:
                        record_plan(self._run, plan)
                    else:
                        plans[stream.key] = plan.to_json()
                        self._run.detail("plans", plans)
                results.append(await self._sync_stream(stream, plan))
                if self._stopped is not None or self._budget_spent:
                    break
        self._report_budget()
        if self._stopped is not None:
            raise SyncStopped(f"the sync stopped: {self._stopped.reason}")
        if self._source.retention:
            async with self._step("retention"):
                await expire_source(
                    self._conn, self._source.id, batch_size=self._options.retention_batch
                )
        if self._run is not None:
            incomplete = sum(1 for result in results for w in result.windows if not w.complete)
            if self._budget_spent:
                self._run.set_outcome(
                    "partial",
                    "the run made the recipe's rate.max_calls calls; the next run goes on from"
                    " where it stopped",
                )
            elif incomplete:
                self._run.set_outcome(
                    "partial",
                    f"{incomplete} windows are incomplete; their checkpoints stay where they were",
                )
        return results

    def _report_budget(self) -> None:
        if self._run is None or self._budget is None or self._recipe is None:
            return
        self._run.detail(
            BUDGET_DETAIL,
            {
                "calls": self._budget.calls,
                "max_calls": self._recipe.rate.max_calls,
                "spent": self._budget_spent,
            },
        )

    async def _sync_stream(self, stream: _Stream, plan: Plan) -> StreamSync:
        done: list[WindowIngest] = []
        for chain in (plan.forward, plan.backfill):
            for window in chain:
                if self._stopped is not None or self._budget_spent:
                    break
                async with self._step(window.label):
                    result = await self._sync_window(stream, window)
                if result is None:
                    break  # the window was deleted while it ran (forget)
                done.append(result)
                if not result.complete:
                    break
        return StreamSync(stream=stream.key, plan=plan, windows=tuple(done))

    async def _sync_window(self, stream: _Stream, window: PlannedWindow) -> WindowIngest | None:
        recipe, parser, session = self._recipe, self._parser, self._session
        if recipe is None or parser is None or session is None or self._budget is None:
            raise RuntimeError("the session is not open")
        server = session.server or ServerInfo(name=recipe.server.name)
        raw = await begin_window(
            self._conn,
            workspace_id=self._source.workspace_id,
            owner_id=self._source.owner_id,
            source_id=self._source.id,
            stream_id=stream.id,
            window=window,
            recipe_digest=recipe_digest(recipe),
            server=ServerInfo(name=server.name, version=server.version),
            run_id=None if self._run is None else self._run.id,
        )
        attempts: dict[str, int] = {}
        self._window_gone = False
        try:
            while self._stopped is None:
                _, responses = await load_window(self._conn, raw.id)
                current = WindowInput(recipe, stream.key, window.start, window.end, responses)
                todo = calls_to_make(current, attempts, self._options.retry.attempts)
                if not todo:
                    break
                if self._budget.spent:
                    self._budget_spent = True
                    break
                async with anyio.create_task_group() as group:
                    for call in todo:
                        group.start_soon(self._make, raw.id, call, attempts)
                if self._window_gone:
                    raise WindowNotFoundError(f"no raw window {raw.id}")
            done = await ingest_window(
                self._conn,
                self._access,
                raw.id,
                recipe=recipe,
                parser=parser,
                limits=self._options.limits,
                indexer=self._indexer,
                extract=self._options.extract,
            )
        except WindowNotFoundError:
            log.info("mcp window %s was deleted while it ran (forget)", raw.id)
            self._count("windows_deleted_while_running")
            return None
        self._count("windows_complete" if done.complete else "windows_attempted_incomplete")
        return done

    async def _make(self, window_id: UUID, call: MissingCall, attempts: dict[str, int]) -> None:
        """Make ``call``, again after a failure that may pass, until it is stored, out of tries,
        or the budget is spent."""
        session, budget, limiter = self._session, self._budget, self._limiter
        if session is None or budget is None or limiter is None:
            raise RuntimeError("the session is not open")
        retry = self._options.retry
        sleep = self._options.sleep or anyio.sleep
        identity = call_identity(call)
        while self._stopped is None and not self._window_gone:
            tried = attempts.get(identity, 0)
            if tried >= retry.attempts:
                return
            if not await budget.take():
                self._budget_spent = True
                return
            attempts[identity] = tried + 1
            self._count("mcp_calls")
            if tried:
                self._count("mcp_retries")
            async with limiter:
                outcome = await session.call(call.tool, call.arguments)
            failure = outcome.failure
            if (
                failure is not None
                and failure.kind == "unauthorized"
                and await self._refresh_after_401()
            ):
                attempts[identity] = tried  # the refused try does not count
                continue
            if outcome.result is not None:
                try:
                    async with self._store_lock:
                        await store_response(
                            self._conn,
                            window_id,
                            call=call.call,
                            tool=call.tool,
                            arguments=call.arguments,
                            result=outcome.result,
                            page=call.page,
                            item_id=call.item_id,
                        )
                except InvalidResponseError as error:
                    log.warning(
                        "mcp call of %s: its result cannot be stored: %s", call.tool, error.reason
                    )
                    self._count("mcp_results_refused")
                    attempts[identity] = retry.attempts
                    return
                except WindowNotFoundError:
                    # Forget deleted the window, which was never parsed; the next run fetches it.
                    self._window_gone = True
                    return
                if not outcome.is_error:
                    return
                self._count("mcp_tool_errors")
                delay = retry.delay(attempts[identity])
            else:
                if failure is None:  # pragma: no cover - an outcome has a result or a failure
                    return
                self._count(f"mcp_failures_{failure.kind}")
                log.warning(
                    "mcp call of %s failed (try %d of %d): %s",
                    call.tool,
                    attempts[identity],
                    retry.attempts,
                    failure.reason,
                )
                if failure.fatal:
                    self._stopped = failure
                    return
                if not failure.retryable:
                    attempts[identity] = retry.attempts
                    return
                delay = retry.delay(
                    attempts[identity],
                    failure.retry_after if failure.kind == "rate_limited" else None,
                )
                if failure.kind == "rate_limited":
                    budget.pause(delay)
            if attempts[identity] >= retry.attempts:
                return
            await sleep(delay)
