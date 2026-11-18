"""Jobs: a queue in Postgres, the workers that run it, and the scheduler that fills it.

- `enqueue` adds a job; with a dedupe key it returns the queued or running job of the same work.
- `Worker` claims jobs (FOR UPDATE SKIP LOCKED), one per source at a time (a session advisory lock
  per source), renews a lease while a job runs, retries a failed job with backoff, quarantines it
  after `max_attempts`, and on stop lets a running job finish within a grace period or returns it
  to the queue.
- `Scheduler` enqueues a sync of each stream whose schedule is due, and a retention pass of each
  source whose retention is due.
- `enqueue_extraction` adds the decision extraction of one thread or meeting (`EXTRACT_KIND`);
  ingest calls it for the records it writes.

Every attempt runs through `execute_job`, which looks up the handler of the job's kind.
"""

from memini.jobs.extraction import EXTRACT_KIND, enqueue_extraction, extraction_dedupe_key
from memini.jobs.queue import (
    DEFAULT_MAX_ATTEMPTS,
    SOURCE_LOCK_NAMESPACE,
    Enqueued,
    Job,
    enqueue,
    source_lock_key,
)
from memini.jobs.scheduler import (
    DEFAULT_RETENTION_INTERVAL,
    RETENTION_KIND,
    SYNC_KINDS,
    Fired,
    Scheduler,
    enqueue_due_retention,
    enqueue_due_streams,
    source_dedupe_key,
    stream_dedupe_key,
)
from memini.jobs.worker import Handler, RetryPolicy, Worker, describe_error, execute_job

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETENTION_INTERVAL",
    "EXTRACT_KIND",
    "RETENTION_KIND",
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
    "enqueue_due_retention",
    "enqueue_due_streams",
    "enqueue_extraction",
    "execute_job",
    "extraction_dedupe_key",
    "source_dedupe_key",
    "source_lock_key",
    "stream_dedupe_key",
]
