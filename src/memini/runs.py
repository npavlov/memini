"""Run reports: every execution of a job leaves one row in the ``run`` table.

Wrap the work of a job in :func:`record_run`::

    async with record_run(pool, workspace_id=ws, owner_id=me, kind="sync",
                          source_id=src, stream_id=stream) as run:
        async with run.step("fetch"):
            batch = await fetch()
        run.count("records_fetched", len(batch))
        async with run.step("store"):
            run.count("records_stored", await store(batch))
        run.llm_tokens(model="local-model", input=1200, output=80)
        if batch.incomplete:
            run.set_outcome("partial", "3 calls of the window are missing")

What it writes, and when:

- On entry, a ``running`` row, committed at once, so ``status`` shows the run while it works
  (principle 10). The row's id is in :data:`memini.logs.run_id_var` until the block ends, so every
  log line of the run carries it.
- At the end of a step, the report so far, at most once per ``progress_interval`` seconds. A
  process that dies mid-run leaves a ``running`` row whose report shows how far it got.
- On exit, ``status``, ``finished_at``, ``error`` and the final report:

  - ``succeeded`` when the block ends normally, unless :meth:`Run.set_outcome` chose ``partial``,
    ``failed`` or ``cancelled``; ``error`` then holds the reason given;
  - ``failed`` when it raises an exception; ``error`` holds the exception type and message,
    redacted (:func:`memini.logs.redact`) and cut to :data:`MAX_ERROR_LENGTH` characters;
  - ``cancelled`` on ``asyncio.CancelledError``, ``KeyboardInterrupt`` or ``SystemExit``.

  The exception always propagates. The final write runs in a task of its own that cancellation
  does not interrupt, and a failure to write is logged, never raised: it does not replace the
  job's own exception. :func:`wait_for_run_writes` waits for writes still in flight, for shutdown.

The run rows are written on connections of their own, never inside the job's transaction, so a job
that rolls back still leaves its report. Pass the pool, or a connection used for nothing else.

The report, version 1 (:class:`RunReport`)::

    {
      "version": 1,
      "duration_ms": 1520.4,               # null while the run is running
      "steps": [                           # in the order they started, at most MAX_STEPS
        {"name": "fetch", "started_at": "2026-01-02T03:04:05.678Z",
         "duration_ms": 1200.1, "status": "succeeded"},
        {"name": "store", "started_at": "…", "duration_ms": 300.2, "status": "failed",
         "error": "ValueError"}            # error: only on a failed step, the exception type
      ],
      "steps_omitted": 0,                  # steps past MAX_STEPS: timed, not listed
      "counts": {"records_fetched": 120, "records_stored": 118},
      "llm": {"calls": 2, "input_tokens": 1200, "output_tokens": 80,
              "by_model": {"local-model": {"calls": 2, "input_tokens": 1200,
                                           "output_tokens": 80}}}
    }

A step's ``status`` is ``running``, ``succeeded``, ``failed`` or ``cancelled``; a running step has
``duration_ms`` null. Durations are in milliseconds from a monotonic clock. A later version adds
keys or bumps ``version``; readers check ``version`` first.
"""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final, Literal, NotRequired, TypedDict

from psycopg import AsyncConnection, errors, pq
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from memini.logs import redact, run_id_var

__all__ = [
    "MAX_ERROR_LENGTH",
    "MAX_STEPS",
    "REPORT_VERSION",
    "LlmReport",
    "ModelUsage",
    "Outcome",
    "Run",
    "RunError",
    "RunReport",
    "RunStatus",
    "StepReport",
    "StepStatus",
    "current_run",
    "record_run",
    "wait_for_run_writes",
]

log = logging.getLogger(__name__)

_current_run: ContextVar["Run | None"] = ContextVar("memini_current_run", default=None)


def current_run() -> "Run | None":
    """The run being recorded in this task, if any: how a job handler reaches its report.

    Set by :func:`record_run` for the duration of its block, like :data:`memini.logs.run_id_var`.
    """
    return _current_run.get()


REPORT_VERSION: Final = 1
MAX_STEPS: Final = 1000
"""Steps listed in a report; later ones are timed but only counted in ``steps_omitted``."""
MAX_ERROR_LENGTH: Final = 1000
"""Characters of ``error`` kept; a longer text is cut and ends in an ellipsis."""
DEFAULT_PROGRESS_INTERVAL: Final = 5.0

RunStatus = Literal["running", "succeeded", "partial", "failed", "cancelled"]
Outcome = Literal["partial", "failed", "cancelled"]
"""Statuses a job may choose for a run that ends without an exception."""
StepStatus = Literal["running", "succeeded", "failed", "cancelled"]

_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")  # run kinds (as the schema checks) and count names
_MAX_STEP_NAME: Final = 200
_OUTCOMES: Final[frozenset[str]] = frozenset({"partial", "failed", "cancelled"})

type _Database = AsyncConnectionPool[Any] | AsyncConnection[Any]


class StepReport(TypedDict):
    name: str
    started_at: str
    duration_ms: float | None
    status: StepStatus
    error: NotRequired[str]


class ModelUsage(TypedDict):
    calls: int
    input_tokens: int
    output_tokens: int


class LlmReport(TypedDict):
    calls: int
    input_tokens: int
    output_tokens: int
    by_model: dict[str, ModelUsage]


class RunReport(TypedDict):
    version: int
    duration_ms: float | None
    steps: list[StepReport]
    steps_omitted: int
    counts: dict[str, int]
    llm: LlmReport


class RunError(Exception):
    """The run row could not be written at the start, so the job did not run."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


def _error_text(text: str) -> str:
    text = redact(text)
    if len(text) > MAX_ERROR_LENGTH:
        text = text[: MAX_ERROR_LENGTH - 1] + "…"
    return text


def _describe(exc: BaseException) -> str:
    try:
        message = str(exc)
    except Exception:
        message = ""
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _check_amount(value: int, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"{what} must be a whole number of at least 0, got {value!r}")


class Run:
    """The run being recorded; :func:`record_run` yields it. Use it from the job's task."""

    def __init__(
        self,
        database: _Database,
        *,
        run_id: uuid.UUID,
        kind: str,
        workspace_id: uuid.UUID,
        owner_id: uuid.UUID,
        source_id: uuid.UUID | None,
        stream_id: uuid.UUID | None,
        started_at: datetime,
        progress_interval: float | None,
    ) -> None:
        self.id: Final = run_id
        self.kind: Final = kind
        self.workspace_id: Final = workspace_id
        self.owner_id: Final = owner_id
        self.source_id: Final = source_id
        self.stream_id: Final = stream_id
        self.started_at: Final = started_at
        self._database = database
        self._progress_interval = progress_interval
        self._clock_start = time.monotonic()
        self._last_save = self._clock_start
        self._steps: list[StepReport] = []
        self._steps_omitted = 0
        self._counts: dict[str, int] = {}
        self._llm: dict[str, ModelUsage] = {}
        self._outcome: tuple[Outcome, str] | None = None
        self._lock = asyncio.Lock()

    # --- What the job records ---------------------------------------------------------------

    @asynccontextmanager
    async def step(self, name: str) -> AsyncGenerator[None]:
        """Time the block as a step called ``name``; its status follows how the block ends."""
        if not name or len(name) > _MAX_STEP_NAME:
            raise ValueError(f"a step name has 1 to {_MAX_STEP_NAME} characters")
        entry: StepReport = {
            "name": name,
            "started_at": _now_iso(),
            "duration_ms": None,
            "status": "running",
        }
        if len(self._steps) < MAX_STEPS:
            self._steps.append(entry)
        else:
            self._steps_omitted += 1
        start = time.monotonic()
        try:
            yield
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            entry["status"] = "cancelled"
            raise
        except BaseException as exc:
            entry["status"] = "failed"
            entry["error"] = type(exc).__name__
            raise
        else:
            entry["status"] = "succeeded"
        finally:
            entry["duration_ms"] = _ms(time.monotonic() - start)
        await self._save_progress_if_due()

    def count(self, name: str, n: int = 1) -> None:
        """Add ``n`` to the count ``name``, e.g. ``records_stored``: lower case, digits, ``_``."""
        if not _NAME.fullmatch(name):
            raise ValueError(f"a count name matches {_NAME.pattern}, got {name!r}")
        _check_amount(n, "a count")
        self._counts[name] = self._counts.get(name, 0) + n

    def llm_tokens(self, *, model: str, input: int, output: int) -> None:
        """Record one LLM call to ``model`` that read ``input`` and wrote ``output`` tokens."""
        if not model:
            raise ValueError("a model name is not empty")
        _check_amount(input, "input tokens")
        _check_amount(output, "output tokens")
        usage = self._llm.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        usage["calls"] += 1
        usage["input_tokens"] += input
        usage["output_tokens"] += output

    def set_outcome(self, status: Outcome, reason: str) -> None:
        """End the run as ``partial``, ``failed`` or ``cancelled`` when the block exits normally.

        ``reason`` goes to the run's ``error``, redacted. An exception from the block overrides it.
        """
        if status not in _OUTCOMES:
            raise ValueError(f"an outcome is one of {sorted(_OUTCOMES)}, got {status!r}")
        if not reason:
            raise ValueError("an outcome needs a reason")
        self._outcome = (status, _error_text(reason))

    def report(self) -> RunReport:
        """The report as it stands; a copy."""
        llm: LlmReport = {
            "calls": sum(usage["calls"] for usage in self._llm.values()),
            "input_tokens": sum(usage["input_tokens"] for usage in self._llm.values()),
            "output_tokens": sum(usage["output_tokens"] for usage in self._llm.values()),
            "by_model": {model: ModelUsage(**usage) for model, usage in self._llm.items()},
        }
        return {
            "version": REPORT_VERSION,
            "duration_ms": None,
            "steps": [StepReport(**step) for step in self._steps],
            "steps_omitted": self._steps_omitted,
            "counts": dict(self._counts),
            "llm": llm,
        }

    async def save_progress(self) -> None:
        """Write the report so far to the running row now. A failure is logged, not raised."""
        async with self._lock:
            report = self.report()
            self._last_save = time.monotonic()
            try:
                async with _connection(self._database) as conn:
                    _require_idle(conn)
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE run SET report = %s WHERE id = %s AND status = 'running'",
                            (Jsonb(report), self.id),
                        )
            except Exception:
                log.warning("could not save the progress of the run", exc_info=True)

    async def _save_progress_if_due(self) -> None:
        interval = self._progress_interval
        if interval is not None and time.monotonic() - self._last_save >= interval:
            await self.save_progress()

    # --- The end ----------------------------------------------------------------------------

    def _final(self, exc: BaseException | None) -> tuple[RunStatus, str | None, RunReport]:
        status: RunStatus
        error: str | None
        if exc is None:
            status, error = self._outcome or ("succeeded", None)
        elif isinstance(exc, asyncio.CancelledError | KeyboardInterrupt | SystemExit):
            status, error = "cancelled", _error_text(_describe(exc))
        else:
            status, error = "failed", _error_text(_describe(exc))
        report = self.report()
        report["duration_ms"] = _ms(time.monotonic() - self._clock_start)
        return status, error, report

    async def _write_final(
        self, status: RunStatus, error: str | None, report: RunReport, exc: BaseException | None
    ) -> None:
        """Write the end of the run and log it. Never raises an Exception."""
        try:
            async with self._lock, _connection(self._database) as conn:
                _require_idle(conn)
                async with conn.transaction():
                    cur = await conn.execute(
                        "UPDATE run SET status = %s, finished_at = greatest(now(), started_at),"
                        " report = %s, error = %s WHERE id = %s AND status = 'running'",
                        (status, Jsonb(report), error, self.id),
                    )
                    if cur.rowcount != 1:
                        log.warning("the run row was no longer running; its end is not written")
        except Exception:
            log.error("could not write the report of the run", exc_info=True)

        level = {"succeeded": logging.INFO, "failed": logging.ERROR}.get(status, logging.WARNING)
        log.log(
            level,
            "run finished: %s",
            status,
            exc_info=(type(exc), exc, exc.__traceback__) if status == "failed" and exc else None,
            extra={
                "kind": self.kind,
                "status": status,
                "duration_ms": report["duration_ms"],
                "counts": report["counts"],
                "llm_input_tokens": report["llm"]["input_tokens"],
                "llm_output_tokens": report["llm"]["output_tokens"],
                "error": error,
            },
        )


@asynccontextmanager
async def _connection(database: _Database) -> AsyncGenerator[AsyncConnection[Any]]:
    if isinstance(database, AsyncConnectionPool):
        async with database.connection() as conn:
            yield conn
    else:
        yield database


def _require_idle(conn: AsyncConnection[Any]) -> None:
    if conn.closed:
        raise RunError("the run's connection is closed")
    # Inside a transaction, the run row would commit or roll back with the job's work.
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise RunError(
            "the run's connection is inside a transaction; pass the pool, or a connection used"
            " only for run reports"
        )


# Final writes in flight. A reference keeps each task alive until it is done.
_writes: set[asyncio.Task[None]] = set()


async def wait_for_run_writes() -> None:
    """Wait until the final write of every run that has ended is done.

    Call it before closing the pool at shutdown: a run cancelled at shutdown finishes its write in
    the background.
    """
    while _writes:
        await asyncio.gather(*_writes, return_exceptions=True)


class _RecordRun(AbstractAsyncContextManager[Run]):
    def __init__(
        self,
        database: _Database,
        *,
        workspace_id: uuid.UUID,
        owner_id: uuid.UUID,
        kind: str,
        source_id: uuid.UUID | None,
        stream_id: uuid.UUID | None,
        progress_interval: float | None,
    ) -> None:
        if not _NAME.fullmatch(kind):
            raise ValueError(f"a run kind matches {_NAME.pattern}, got {kind!r}")
        if stream_id is not None and source_id is None:
            raise ValueError("a run of a stream names its source: pass source_id with stream_id")
        if progress_interval is not None and progress_interval < 0:
            raise ValueError("progress_interval is at least 0, or None for no progress saves")
        self._database = database
        self._workspace_id = workspace_id
        self._owner_id = owner_id
        self._kind = kind
        self._source_id = source_id
        self._stream_id = stream_id
        self._progress_interval = progress_interval
        self._run: Run | None = None
        self._token: Token[uuid.UUID | None] | None = None
        self._run_token: Token[Run | None] | None = None

    async def __aenter__(self) -> Run:
        empty: RunReport = {
            "version": REPORT_VERSION,
            "duration_ms": None,
            "steps": [],
            "steps_omitted": 0,
            "counts": {},
            "llm": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_model": {}},
        }
        try:
            async with _connection(self._database) as conn:
                _require_idle(conn)
                async with conn.transaction():
                    cur = await conn.execute(
                        "INSERT INTO run (workspace_id, owner_id, source_id, stream_id, kind,"
                        " report) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, started_at",
                        (
                            self._workspace_id,
                            self._owner_id,
                            self._source_id,
                            self._stream_id,
                            self._kind,
                            Jsonb(empty),
                        ),
                    )
                    row = await cur.fetchone()
        except errors.IntegrityError as exc:
            constraint = exc.diag.constraint_name or type(exc).__name__
            raise RunError(
                f"the database refused the run ({constraint}): the workspace, owner, source and"
                " stream must exist, and the stream must belong to the source and the source to"
                " the workspace"
            ) from exc
        if row is None:  # pragma: no cover - INSERT … RETURNING returns the row
            raise RunError("the run row was not returned")
        run_id, started_at = row[0], row[1]
        run = Run(
            self._database,
            run_id=run_id,
            kind=self._kind,
            workspace_id=self._workspace_id,
            owner_id=self._owner_id,
            source_id=self._source_id,
            stream_id=self._stream_id,
            started_at=started_at,
            progress_interval=self._progress_interval,
        )
        self._run = run
        self._token = run_id_var.set(run_id)
        self._run_token = _current_run.set(run)
        log.info(
            "run started: %s",
            self._kind,
            extra={
                "kind": self._kind,
                "workspace_id": self._workspace_id,
                "source_id": self._source_id,
                "stream_id": self._stream_id,
            },
        )
        return run

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        run, token = self._run, self._token
        if run is None or token is None:
            raise RuntimeError("__aexit__ without __aenter__")
        try:
            status, error, report = run._final(exc)  # pyright: ignore[reportPrivateUsage]
            # A task of its own, so that a cancellation of this task, even a repeated one, does
            # not cut the write short. It copies the context, run id included, for its log lines.
            write = asyncio.create_task(run._write_final(status, error, report, exc))  # pyright: ignore[reportPrivateUsage]
            _writes.add(write)
            write.add_done_callback(_writes.discard)
            await asyncio.shield(write)
        finally:
            run_id_var.reset(token)
            if self._run_token is not None:
                _current_run.reset(self._run_token)
        # None: the job's exception, if any, propagates.


def record_run(
    database: AsyncConnectionPool[Any] | AsyncConnection[Any],
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    kind: str,
    source_id: uuid.UUID | None = None,
    stream_id: uuid.UUID | None = None,
    progress_interval: float | None = DEFAULT_PROGRESS_INTERVAL,
) -> AbstractAsyncContextManager[Run]:
    """Record the block as one run of a job: a ``run`` row with its report (see the module).

    Args:
        database: the pool, or a connection used for nothing but run reports. Each write commits
            on its own, outside any transaction of the job.
        workspace_id, owner_id: the scope of the run row.
        kind: what runs, as named by the job: ``sync``, ``retention``, ``reindex`` …; lower case,
            digits and ``_``, starting with a letter.
        source_id, stream_id: what the run works on, if anything. A stream needs its source.
        progress_interval: save the report at the end of a step when at least this many seconds
            passed since the last save; ``0`` saves at every step, ``None`` only at the end.

    Raises:
        ValueError: when called, for a bad ``kind``, a stream without its source, or a negative
            ``progress_interval``.
        RunError: on entry, when the database refuses the row (an unknown workspace, a stream of
            another source …) or the connection is inside a transaction. The block does not run.
    """
    return _RecordRun(
        database,
        workspace_id=workspace_id,
        owner_id=owner_id,
        kind=kind,
        source_id=source_id,
        stream_id=stream_id,
        progress_interval=progress_interval,
    )
