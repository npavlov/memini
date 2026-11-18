-- Job queue and stream schedules: the work the workers run, and when a stream is due for a sync.
-- docs/requirements.md, sections 4 (scheduling and hygiene), 7 and 9. The code is memini.jobs.
-- The conventions listed at the top of 0002 hold here too.
--
-- How a job moves:
--
--   queued --claim--> running --handler returns--> succeeded
--     ^                  |
--     |                  +--handler raises, or lease ran out--+--attempts < max_attempts--> queued
--     |                  |                                    +--attempts = max_attempts--> quarantined
--     +--worker stops----+
--
-- * Workers claim the oldest due queued job with FOR UPDATE SKIP LOCKED: two workers never claim
--   the same job, and a worker never waits for another one's claim.
-- * A claim adds one to attempts and sets a lease. While the job runs, the worker renews the lease
--   (a heartbeat). A lease that has run out cannot be renewed: the worker died or hung, and the
--   next claim of any worker puts the job back in the queue. The lost attempt counts, so a job
--   that kills its worker every time ends in quarantine like one that fails.
-- * A failed attempt puts the job back with run_after moved forward by a backoff, until the job
--   has used max_attempts; then it is quarantined and stays until a person looks at it.
-- * A worker that stops returns its running jobs to the queue and gives the attempt back: a
--   shutdown is not the job's fault.
-- * One job per source runs at a time; this is not a column. The worker holds a session advisory
--   lock on the source for as long as the job runs, on the connection it renews the lease with. A
--   dead worker's connection closes and the lock goes with it. The lock has the two-key form
--   (namespace, key of the source) and so never collides with the migration lock, which uses the
--   single 64-bit form.
-- * Finished jobs stay as history. What an execution did is reported in run (0002), not here.

CREATE TABLE job (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    -- The source and stream the job works on, if any. A job of a source does not run while
    -- another job of the same source runs.
    source_id    uuid,
    stream_id    uuid,
    -- What to run: a worker runs a job only if it has a handler for the kind. The same form as
    -- run.kind, e.g. git_sync, retention, reindex.
    kind         text        NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]*$'),
    -- Arguments of the handler. Never a secret (section 7).
    args         jsonb       NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(args) = 'object'),
    -- Jobs with the same workspace, kind and key do the same work: while one of them is queued or
    -- running, enqueuing another returns that one (job_dedupe_key_idx). NULL: never merged. The
    -- scheduler keys the jobs of a stream by the stream id.
    dedupe_key   text        CHECK (dedupe_key <> ''),
    -- queued: waiting for a worker, from run_after on. running: claimed; lease_until is set.
    -- succeeded: the handler returned. quarantined: max_attempts attempts failed.
    status       text        NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'running', 'succeeded', 'quarantined')),
    -- Claims so far, a running one included. A shutdown gives its claim back.
    attempts     integer     NOT NULL DEFAULT 0,
    max_attempts integer     NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    -- Not claimed before this time: the time of enqueue for a new job, later for a retry.
    run_after    timestamptz NOT NULL DEFAULT now(),
    -- Set while running. The claim holds until this time unless the worker renews it; after it,
    -- the worker counts as dead.
    lease_until  timestamptz,
    -- The worker that claimed the job last: host, process and slot. For status and logs.
    worker       text,
    -- Why the last failed attempt failed: exception type and message, credentials in URLs masked.
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,           -- the last claim
    finished_at  timestamptz,           -- set when succeeded or quarantined
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT job_attempts_check CHECK (attempts BETWEEN 0 AND max_attempts),
    -- A job holds a lease exactly while it runs.
    CONSTRAINT job_lease_until_check CHECK ((status = 'running') = (lease_until IS NOT NULL)),
    CONSTRAINT job_finished_at_check
        CHECK ((status IN ('succeeded', 'quarantined')) = (finished_at IS NOT NULL)),
    -- A foreign key with a NULL column is not checked, so a job of a stream names its source.
    CONSTRAINT job_stream_id_check CHECK (stream_id IS NULL OR source_id IS NOT NULL),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, stream_id) REFERENCES stream (source_id, id) ON DELETE CASCADE
);

-- Claim: queued jobs in the order they became due.
CREATE INDEX job_queued_idx ON job (run_after) WHERE status = 'queued';
-- Reap: running jobs whose lease ran out.
CREATE INDEX job_running_idx ON job (lease_until) WHERE status = 'running';
-- At most one queued or running job per workspace, kind and key; the conflict target of enqueue.
CREATE UNIQUE INDEX job_dedupe_key_idx ON job (workspace_id, kind, dedupe_key)
    WHERE status IN ('queued', 'running');
-- Jobs of a workspace by time: listings, and deleting a workspace.
CREATE INDEX job_workspace_created_idx ON job (workspace_id, created_at);
-- Jobs of a source or a stream: listings, and deleting a source or a stream.
CREATE INDEX job_source_stream_idx ON job (source_id, stream_id);

-- When the scheduler next enqueues a sync of the stream. Read only where schedule is set. NULL:
-- at the scheduler's next look, so a new stream syncs at once. On enqueue the scheduler moves it to
-- now() + schedule. Code that changes the schedule of a stream sets it to NULL, so the new
-- schedule starts now instead of after the old interval.
ALTER TABLE stream ADD COLUMN next_run_at timestamptz;

-- The scheduler: streams with a schedule, in the order they become due.
CREATE INDEX stream_next_run_idx ON stream (next_run_at) WHERE schedule IS NOT NULL;
