"""Jobs: a queue in Postgres, the workers that run it, and the scheduler that fills it.

- `enqueue` adds a job; with a dedupe key it returns the queued or running job of the same work.
- `Worker` claims jobs (FOR UPDATE SKIP LOCKED), one per source at a time (a session advisory lock
  per source), renews a lease while a job runs, retries a failed job with backoff, quarantines it
  after `max_attempts`, and on stop lets a running job finish within a grace period or returns it
  to the queue.
- `Scheduler` enqueues a sync of each stream whose schedule is due.

Every attempt runs through `execute_job`, which looks up the handler of the job's kind.
"""

from memini.jobs.queue import (
    DEFAULT_MAX_ATTEMPTS,
    SOURCE_LOCK_NAMESPACE,
    Enqueued,
    Job,
    enqueue,
    source_lock_key,
)
from memini.jobs.scheduler import (
    SYNC_KINDS,
    Fired,
    Scheduler,
    enqueue_due_streams,
    stream_dedupe_key,
)
from memini.jobs.worker import Handler, RetryPolicy, Worker, describe_error, execute_job

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "SOURCE_LOCK_NAMESPACE",
    "SYNC_KINDS",
    "Enqueued",
    "Fired",
    "Handler",
    "Job",
    "RetryPolicy",
    "Scheduler",
    "Worker",
    "describe_error",
    "enqueue",
    "enqueue_due_streams",
    "execute_job",
    "source_lock_key",
    "stream_dedupe_key",
]
