-- Forget and retention: removing raw records with everything derived from them, by hand (forget)
-- and by age (retention). docs/requirements.md, sections 3 (knowledge layer), 4 (Scheduling and
-- hygiene) and 6 (HTTP API: forget). The code is memini.removal. The conventions listed at the
-- top of 0002 hold here too.
--
-- Both delete the record row, its chunks and symbols (0005), and whatever else holds its text:
-- the person links that cascade from the record, and the raw responses it was parsed from once
-- the raw response store exists (memini.removal.core names the hook). Knowledge evidence that
-- cites the record stays: the journal is append-only (0008), and evidence keeps the reference
-- (source, kind, external id and URL), never the text. What the two tables below add is what
-- the reference alone cannot say: why the record is gone.
--
-- * forgotten_record: one row per record forgotten by hand. It is the audit of forget (who and
--   when) and the list a sync consults so that a forgotten record does not come back: ingest
--   skips a record whose id is here, and a git sync skips the file. The id suffices for both, as
--   a record id is derived from the source, kind and external id (record_id, 0002) and every
--   writer computes it before it writes. The row keeps no external id, URL or text: an external
--   id can itself be private (a mail subject, a meeting title, a file name that names someone).
--   No foreign key to the source: the audit outlives it; a record of a source added again later
--   has another id, so an old row denies nothing new.
-- * expired_record: a record retention removed while knowledge evidence cited it, so that the
--   evidence reads as expired rather than merely gone. Only cited records get a row; retention
--   counts the others in its run report and keeps no trace of them. A record that comes back
--   (sent again inside the retention period) reads as stored again; its row stays until the
--   source is deleted.
--
-- Evidence reads, in this order: stored or deleted (a tombstone) while the record row exists;
-- forgotten; expired; else gone (its source was deleted).

CREATE TABLE forgotten_record (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    -- Who: the principal that forgot the record, and the token of the request (NULL for an
    -- operator acting on the database without one).
    owner_id     uuid        NOT NULL REFERENCES principal,
    token_id     uuid        REFERENCES token ON DELETE SET NULL,
    -- The record, by its deterministic id. A record is forgotten once: it cannot come back to be
    -- forgotten again.
    record_id    text        NOT NULL UNIQUE CHECK (record_id ~ '^[0-9a-f]{64}$'),
    source_id    uuid        NOT NULL,
    record_kind  text        NOT NULL
                             CHECK (record_kind IN ('code_file', 'thread', 'meeting', 'document')),
    -- When: the time of the transaction that deleted the record.
    forgotten_at timestamptz NOT NULL DEFAULT now()
);

-- The audit of a workspace by time.
CREATE INDEX forgotten_record_workspace_idx ON forgotten_record (workspace_id, forgotten_at);

CREATE TABLE expired_record (
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    record_id    text        PRIMARY KEY CHECK (record_id ~ '^[0-9a-f]{64}$'),
    source_id    uuid        NOT NULL,
    record_kind  text        NOT NULL
                             CHECK (record_kind IN ('code_file', 'thread', 'meeting', 'document')),
    -- The last time retention removed it.
    expired_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE
);

-- Deleting a source: its expired records.
CREATE INDEX expired_record_source_idx ON expired_record (source_id);

-- When the scheduler next enqueues a retention job of the source. Read only where retention_days
-- is set. NULL: at the scheduler's next look, so a new or changed retention applies at once. On
-- enqueue the scheduler moves it to now() plus the retention interval of the settings; code that
-- changes retention_days sets it to NULL.
ALTER TABLE source ADD COLUMN retention_next_at timestamptz;

-- The scheduler: sources with a retention, in the order they become due.
CREATE INDEX source_retention_next_idx ON source (retention_next_at)
    WHERE retention_days IS NOT NULL;

-- Retention of a git source expires its tombstones by deleted_at (a file of the tree at the pinned
-- commit is current whatever its age). No index on deleted_at: it would make every tombstone and
-- revival a non-HOT update, for a scan of one source's files that the source's indexes bound.
