"""Workers: claim jobs, run their handlers, record how each attempt ended.

A `Worker` runs a number of slots. A slot claims one job at a time (memini.jobs.queue.claim) and
keeps the pooled connection it claimed on for as long as the job runs. On that connection it holds
the advisory lock of the job's source and renews the job's lease. The handler does its own
database work on connections of its own.

How an attempt ends:

- The handler returns: the job succeeded.
- The handler raises: the attempt failed. The job is retried after a backoff (`RetryPolicy`), or
  quarantined when it has used all its attempts.
- The worker is asked to stop (`Worker.stop`): it claims nothing more and gives a running handler
  the grace period to finish. A handler still running after it is cancelled, and its job goes back
  to the queue with the attempt given back. Cancelling `Worker.run` from outside is a stop
  without grace.
- The claim is lost: the lease could not be renewed, or the connection holding it closed. The
  handler is cancelled and the job row is left alone. If the connection is gone, so is the source
  lock; the lease runs out and another claim puts the job back, as for a worker that died.

Every execution of a claimed job goes through `execute_job`. That is the place to wrap an attempt
with a run report.
"""

import enum
import logging
import os
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import anyio
from psycopg import AsyncConnection

from memini.db import Pool
from memini.jobs.queue import (
    Job,
    check_kind,
    claim,
    complete,
    fail,
    release,
    renew_lease,
    unlock_source,
)
from memini.runs import record_run

__all__ = [
    "Handler",
    "RetryPolicy",
    "Worker",
    "describe_error",
    "execute_job",
]

log = logging.getLogger(__name__)

type Handler = Callable[[Job], Awaitable[None]]
"""Does the work of one job kind. Returns when done; raises when the attempt failed."""

_SETTLE_TIMEOUT: Final = 10.0
"""Seconds to record the end of an attempt and release the source lock, even when cancelled."""

_ERROR_LIMIT: Final = 2000
_URL_CREDENTIALS: Final = re.compile(r"(?<=://)[^/\s@]+@")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backoff between failed attempts: `base`, then times `factor` per attempt, at most `cap`."""

    base: timedelta = timedelta(seconds=30)
    factor: float = 2.0
    cap: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if self.base < timedelta(0) or self.cap < self.base or self.factor < 1:
            raise ValueError("a retry policy needs 0 <= base <= cap and factor >= 1")

    def delay(self, attempt: int) -> timedelta:
        """The wait after failed attempt number `attempt` (1 for the first)."""
        seconds = self.base.total_seconds() * self.factor ** max(attempt - 1, 0)
        return min(timedelta(seconds=seconds), self.cap)


def describe_error(error: BaseException) -> str:
    """Exception type and message for job.last_error: user and password in URLs masked, cut short.

    A handler should raise errors that hold no secret; this catches the common slip of a URL with
    credentials, such as a git remote, in an error message.
    """
    message = str(error)
    text = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return _URL_CREDENTIALS.sub("***@", text)[:_ERROR_LIMIT]


async def execute_job(job: Job, handlers: Mapping[str, Handler], pool: Pool) -> None:
    """Run the handler of a claimed job: the one place every attempt goes through.

    Every attempt is recorded as a run (:func:`memini.runs.record_run`): a ``run`` row of the job's
    kind, workspace, source and stream, with the report the handler fills through
    :func:`memini.runs.current_run`. The row is written through ``pool``, outside the job's own
    transactions. Returns when the handler returns and raises what it raises; the worker records
    the outcome of the job.
    """
    handler = handlers[job.kind]
    async with record_run(
        pool,
        workspace_id=job.workspace_id,
        owner_id=job.owner_id,
        kind=job.kind,
        source_id=job.source_id,
        stream_id=job.stream_id,
    ):
        await handler(job)


class _Outcome(enum.Enum):
    SUCCEEDED = enum.auto()
    FAILED = enum.auto()
    INTERRUPTED = enum.auto()  # stopped by the worker: back to the queue, attempt given back
    LOST = enum.auto()  # the claim is gone: leave the row alone


class Worker:
    """Runs queued jobs whose kinds it has handlers for, until stopped.

    `pool` lends each slot one connection per running job, and handlers draw from it too: size it
    for at least `concurrency` connections plus what the handlers use.

    `lease` is how long a claim holds without renewal; the worker renews it every `heartbeat`
    (default: a third of the lease). A worker that dies leaves its jobs for at most `lease` before
    they run again. `poll_interval` is the wait between looks at an empty queue.
    """

    def __init__(
        self,
        pool: Pool,
        handlers: Mapping[str, Handler],
        *,
        name: str | None = None,
        concurrency: int = 1,
        lease: timedelta = timedelta(seconds=60),
        heartbeat: timedelta | None = None,
        poll_interval: timedelta = timedelta(seconds=1),
        grace: timedelta = timedelta(seconds=30),
        retry: RetryPolicy | None = None,
    ) -> None:
        if not handlers:
            raise ValueError("a worker needs at least one handler")
        for kind in handlers:
            check_kind(kind)
        heartbeat = lease / 3 if heartbeat is None else heartbeat
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if not timedelta(0) < heartbeat < lease:
            raise ValueError("the heartbeat must be positive and shorter than the lease")
        if poll_interval <= timedelta(0) or grace < timedelta(0):
            raise ValueError("poll_interval must be positive and grace not negative")
        self.name: Final = name or f"{socket.gethostname()}:{os.getpid()}"
        self._pool = pool
        self._handlers: Final = dict(handlers)
        self._kinds: Final = sorted(self._handlers)
        self._concurrency = concurrency
        self._lease = lease
        self._heartbeat = heartbeat
        self._poll = poll_interval
        self._grace = grace
        self._retry = retry or RetryPolicy()
        self._stop_requested = False
        self._stopping: anyio.Event | None = None

    def stop(self) -> None:
        """Ask the worker to stop: claim nothing more, let running jobs finish within the grace.

        `run` returns once every slot is done. Safe to call more than once, and before `run`.
        """
        self._stop_requested = True
        if self._stopping is not None:
            self._stopping.set()

    async def run(self) -> None:
        """Claim and run jobs until `stop` is called; then wait for the running ones."""
        self._stopping = stopping = anyio.Event()
        if self._stop_requested:
            stopping.set()
        async with anyio.create_task_group() as slots:
            for slot in range(self._concurrency):
                slots.start_soon(self._slot, f"{self.name}/{slot}", stopping)

    async def _slot(self, worker: str, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            try:
                ran = await self._claim_and_run(worker, stopping)
            except Exception:
                # The database is away, or a bug: say so, wait, try again.
                log.exception("worker %s could not claim a job", worker)
                ran = False
            if not ran:
                with anyio.move_on_after(self._poll.total_seconds()):
                    await stopping.wait()

    async def _claim_and_run(self, worker: str, stopping: anyio.Event) -> bool:
        conn = await self._pool.getconn()
        try:
            try:
                job = await claim(conn, kinds=self._kinds, worker=worker, lease=self._lease)
            except BaseException:
                # The connection may hold the lock of a source; closing it is the sure release.
                with anyio.CancelScope(shield=True):
                    await conn.close()
                raise
            if job is None:
                return False
            log.info(
                "worker %s claimed job %s (%s), attempt %d", worker, job.id, job.kind, job.attempt
            )
            outcome, error = _Outcome.INTERRUPTED, None
            try:
                outcome, error = await self._attempt(conn, job, stopping)
            finally:
                await self._settle(conn, job, outcome, error)
            return True
        finally:
            # Shielded: a cancelled worker still hands the connection back (or a closed one, which
            # the pool replaces).
            with anyio.CancelScope(shield=True):
                await self._pool.putconn(conn)

    async def _attempt(
        self, conn: AsyncConnection[Any], job: Job, stopping: anyio.Event
    ) -> tuple[_Outcome, str | None]:
        """Run the handler and renew the lease; cancel the handler on a lost claim, or when the
        grace after a stop has passed."""
        handler_scope = anyio.CancelScope()
        lost = False

        async def keep_lease() -> None:
            nonlocal lost
            try:
                while True:
                    # Waiting for notifications doubles as a watch on the connection: when the
                    # server ends it, the wait fails at once instead of at the next renewal.
                    async with aclosing(
                        conn.notifies(timeout=self._heartbeat.total_seconds())
                    ) as notifies:
                        async for _ in notifies:
                            pass
                    with anyio.CancelScope(shield=True):
                        if not await renew_lease(conn, job, lease=self._lease):
                            log.warning("job %s: the lease was taken over", job.id)
                            break
            except Exception as error:
                log.warning("job %s: lost the claim: %s", job.id, describe_error(error))
            lost = True
            handler_scope.cancel()

        async def grace_on_stop() -> None:
            await stopping.wait()
            await anyio.sleep(self._grace.total_seconds())
            handler_scope.cancel()

        completed = False
        error: str | None = None
        async with anyio.create_task_group() as watchers:
            watchers.start_soon(keep_lease)
            watchers.start_soon(grace_on_stop)
            with handler_scope:
                try:
                    await execute_job(job, self._handlers, self._pool)
                    completed = True
                except Exception as raised:
                    error = describe_error(raised)
                    log.warning(
                        "job %s (%s) attempt %d failed: %s", job.id, job.kind, job.attempt, error
                    )
            watchers.cancel_scope.cancel()

        if completed:
            return _Outcome.SUCCEEDED, None
        if lost:
            return _Outcome.LOST, None
        if error is not None and not handler_scope.cancel_called:
            return _Outcome.FAILED, error
        return _Outcome.INTERRUPTED, None

    async def _settle(
        self, conn: AsyncConnection[Any], job: Job, outcome: _Outcome, error: str | None
    ) -> None:
        """Record the outcome and release the source lock; runs even when cancelled."""
        with anyio.move_on_after(_SETTLE_TIMEOUT, shield=True) as scope:
            try:
                await self._record(conn, job, outcome, error)
                if job.source_id is not None and not await unlock_source(conn, job.source_id):
                    log.warning("job %s: the lock of source %s was not held", job.id, job.source_id)
                return
            except Exception as failure:
                log.warning(
                    "job %s: could not record the outcome: %s", job.id, describe_error(failure)
                )
        if scope.cancelled_caught:
            log.warning("job %s: recording the outcome timed out", job.id)
        # Closing the session drops the source lock it may still hold; the pool replaces it.
        with anyio.CancelScope(shield=True):
            await conn.close()

    async def _record(
        self, conn: AsyncConnection[Any], job: Job, outcome: _Outcome, error: str | None
    ) -> None:
        match outcome:
            case _Outcome.SUCCEEDED:
                kept = await complete(conn, job)
                log.info("job %s (%s) succeeded", job.id, job.kind)
            case _Outcome.FAILED:
                status = await fail(
                    conn, job, error=error or "failed", retry_in=self._retry.delay(job.attempt)
                )
                kept = status is not None
                if status == "quarantined":
                    log.error(
                        "job %s (%s) quarantined after %d attempts", job.id, job.kind, job.attempt
                    )
            case _Outcome.INTERRUPTED:
                kept = await release(conn, job)
                log.info("job %s (%s) returned to the queue", job.id, job.kind)
            case _Outcome.LOST:
                kept = True
        if not kept:
            log.warning("job %s: the claim was lost before its outcome was recorded", job.id)
