-- Core schema: who acts (principal, token), the isolation unit (workspace), what is synced
-- (source, stream), the raw layer (record), people (person, alias) and what the service did (run).
-- docs/requirements.md, sections 3, 4 and 8.
--
-- Conventions, so the tables below need not repeat them:
--
-- * Scoping. Every row carries workspace_id and owner_id. workspace_id is the isolation unit;
--   owner_id is the principal who owns the row. v1 has one principal, and the one authorization
--   filter lets everything through; the columns are here so that adding users later needs no data
--   migration (section 8). tests/test_schema.py fails when a table lacks either column, except
--   for the few tables it lists with a reason (principal, workspace, schema_migration).
-- * Isolation by foreign key. A child names its parent together with the workspace:
--   (workspace_id, source_id) REFERENCES source (workspace_id, id). A row therefore cannot point
--   at a parent in another workspace. Parents carry UNIQUE (workspace_id, id) next to their
--   primary key as the target of such keys. Inside a source the same works with source_id: a
--   record or run names its stream as (source_id, stream_id).
-- * Ids are uuidv7(): time-ordered, built into Postgres 18. Records are the exception: their id
--   is derived from what the record is (record_id below), so the same record always gets the same
--   id (principle 6).
-- * Enumerations are text with a CHECK, not enum types. A later migration extends a list by
--   replacing the CHECK.
-- * Times are timestamptz. The code that changes a row sets its updated_at; there are no triggers.
-- * Deleting a workspace deletes everything in it; deleting a source deletes its streams, records
--   and runs (ON DELETE CASCADE). A principal that owns rows cannot be deleted.
-- * The names of principals, workspaces and sources are handles, typed in the CLI and shown in
--   URLs: lowercase letters, digits, '-' and '_', at most 63 characters.

-- principal: someone who acts on the service. v1 has exactly one, the user. A principal is not
-- inside a workspace (it owns rows in several) and is itself the owner, so it has neither
-- workspace_id nor owner_id.
CREATE TABLE principal (
    id         uuid        PRIMARY KEY DEFAULT uuidv7(),
    name       text        NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- workspace: the isolation unit, a company or a project. Its own id is the workspace_id of every
-- row inside it, so it has no workspace_id column. Its owner is the principal who created it.
CREATE TABLE workspace (
    id         uuid        PRIMARY KEY DEFAULT uuidv7(),
    owner_id   uuid        NOT NULL REFERENCES principal,
    name       text        NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- token: a bearer token. A request with the token acts as its owner, the principal the token
-- resolves to, limited to the token's scopes and, if set, to one workspace. The secret is shown
-- once when the token is created and is never stored; this table keeps its digest.
CREATE TABLE token (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    -- NULL: the token reaches every workspace its owner may read. Set: only this workspace; the
    -- authorization filter narrows every read to it.
    workspace_id uuid        REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    -- What the token is for, e.g. "laptop agent".
    name         text        NOT NULL CHECK (name <> ''),
    -- SHA-256 digest of the secret. The token of a request is hashed and looked up here.
    hash         bytea       NOT NULL UNIQUE CHECK (octet_length(hash) = 32),
    -- The first characters of the secret, too few to use it, shown in listings so a user can
    -- tell tokens apart.
    prefix       text        NOT NULL CHECK (char_length(prefix) BETWEEN 1 AND 16),
    -- read: query tools. ingest: write tools and the push API. admin: workspaces, sources,
    -- tokens.
    scopes       text[]      NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,           -- NULL: does not expire
    revoked_at   timestamptz,           -- set: refused from this time on
    last_used_at timestamptz,
    CONSTRAINT token_scopes_check CHECK (
        cardinality(scopes) > 0
        AND array_ndims(scopes) = 1
        AND scopes <@ ARRAY['read', 'ingest', 'admin']
    ),
    CONSTRAINT token_expires_at_check CHECK (expires_at > created_at)
);

-- source: a configured origin of records (section 3, raw layer).
CREATE TABLE source (
    id             uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id   uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id       uuid        NOT NULL REFERENCES principal,
    -- Unique in the workspace.
    name           text        NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    -- git: the service mirrors a repository. push: clients send records through the API.
    -- mcp: a recipe syncs an MCP server.
    kind           text        NOT NULL CHECK (kind IN ('git', 'push', 'mcp')),
    -- Settings of the kind: repository URL and tracked refs, recipe, filters. Never a secret:
    -- secrets come from the environment or the credential store (section 7).
    config         jsonb       NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(config) = 'object'),
    -- private: only the owner may read the source. workspace: everyone who may read the
    -- workspace. One user in v1 makes them equal; private is the safe default for later.
    visibility     text        NOT NULL DEFAULT 'private'
                               CHECK (visibility IN ('private', 'workspace')),
    -- Records whose occurred_at is older than this many days are deleted on every run, with
    -- everything derived from them. NULL: kept until deleted at the source.
    retention_days integer     CHECK (retention_days > 0),
    -- Which LLM may read the content of this source in the derive stage (principle 8).
    -- none: no LLM; nothing is extracted. local: the local provider of the workspace.
    -- cloud: a cloud provider too, if the workspace has opted in to one. Default local: a cloud
    -- provider is always an explicit choice, for the workspace and for the source.
    llm_policy     text        NOT NULL DEFAULT 'local'
                               CHECK (llm_policy IN ('none', 'local', 'cloud')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name),
    UNIQUE (workspace_id, id)
);

-- stream: the unit of incremental sync inside a source: a git ref, a channel, a mailbox query, a
-- meeting list. Each stream has its own schedule and checkpoint. Its last run and that run's
-- result are the newest run row of the stream (run_stream_started_idx); they are not copied here.
CREATE TABLE stream (
    id            uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    source_id     uuid        NOT NULL,
    -- What the stream follows inside its source: a ref name, a channel id, a query.
    key           text        NOT NULL CHECK (key <> ''),
    -- Where the next window starts: a commit for a git ref, a page token or a time for a recipe.
    -- Its shape belongs to the connector. NULL before the first complete window. It moves only in
    -- the transaction that stores a complete window (principle 7).
    cursor        jsonb,
    -- Everything in the stream up to this time is stored. Lag is now() - checked_until.
    -- NULL before the first complete window.
    checked_until timestamptz,
    -- Run every this long. NULL: only when triggered.
    schedule      interval    CHECK (schedule > interval '0'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    UNIQUE (source_id, key),
    UNIQUE (source_id, id)
);

-- Deterministic record id, written out in Python as memini.ids.record_id; tests pin both to the
-- same fixed values. SHA-256 over four length-prefixed fields: the domain 'memini.record.v1', the
-- 16 bytes of the source id, the kind and the external id in UTF-8. Each field is preceded by its
-- length as 8 bytes, big-endian, so no choice of field values can make two inputs encode alike.
-- Result: 64 lowercase hex characters.
CREATE FUNCTION length_prefixed(field bytea) RETURNS bytea
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN int8send(octet_length(field)) || field;

-- IMMUTABLE although convert_to is only STABLE: convert_to is STABLE because encoding conversions
-- are catalog entries, and the result of converting to UTF8 does not depend on them.
CREATE FUNCTION record_id(source_id uuid, kind text, external_id text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN encode(
        sha256(
            length_prefixed(convert_to('memini.record.v1', 'UTF8'))
            || length_prefixed(uuid_send(source_id))
            || length_prefixed(convert_to(kind, 'UTF8'))
            || length_prefixed(convert_to(external_id, 'UTF8'))
        ),
        'hex'
    );

-- record: one raw unit, the source of truth for everything derived (principle 1). Ingest is an
-- upsert by id. A record seen again with the same content_hash changes nothing.
CREATE TABLE record (
    -- record_id(source_id, kind, external_id), enforced below: the id and the external identity
    -- cannot disagree.
    id              text        PRIMARY KEY,
    workspace_id    uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id        uuid        NOT NULL REFERENCES principal,
    source_id       uuid        NOT NULL,
    -- The stream that last stored the record, if any. Not part of the id: a thread reached
    -- through two streams of one source is one record.
    stream_id       uuid,
    -- code_file: a file at the pinned commit of a git ref. thread: messages with authors, times,
    -- recipients and mentions. meeting: title, times, participants, summary, action items,
    -- transcript. document: a pushed or synced document.
    kind            text        NOT NULL
                                CHECK (kind IN ('code_file', 'thread', 'meeting', 'document')),
    -- The id of the record in its source: a thread key, a meeting id, a ref and path. Unique per
    -- source and kind. Opaque: compared as given, never normalized.
    external_id     text        NOT NULL CHECK (external_id <> ''),
    external_url    text,
    -- Digest of the content as lowercase hex; for a code file, its git blob id. An unchanged hash
    -- means nothing to re-derive or re-embed (principle 6).
    content_hash    text        NOT NULL CHECK (content_hash <> ''),
    -- Event time in the source: the latest message of a thread, the start of a meeting, the
    -- commit of a code file. Search results show it; retention counts from it.
    occurred_at     timestamptz NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),  -- first stored
    updated_at      timestamptz NOT NULL DEFAULT now(),  -- content or deleted_at last changed
    -- Tombstone: the source deleted the record. The row stays until retention or forget.
    deleted_at      timestamptz,
    -- The payload as received, byte for byte, so a parser fix can be replayed. bytea, not jsonb
    -- or text: jsonb reorders keys and drops duplicate keys and whitespace; text cannot hold
    -- every byte. NULL for code files.
    payload         bytea,
    -- Plain text that the parser of the kind derives from the payload; chunking reads it.
    normalized_text text,
    CONSTRAINT record_id_check CHECK (id = record_id(source_id, kind, external_id)),
    -- Code is not copied into the database: the mirror of the repository is the raw store.
    CONSTRAINT record_code_file_check
        CHECK (kind <> 'code_file' OR (payload IS NULL AND normalized_text IS NULL)),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, stream_id) REFERENCES stream (source_id, id)
        ON DELETE SET NULL (stream_id),
    UNIQUE (source_id, kind, external_id)
);

-- Retention: records of a source older than its retention_days.
CREATE INDEX record_source_occurred_idx ON record (source_id, occurred_at);
-- Lists by kind and time in a workspace: meetings, threads, recent documents.
CREATE INDEX record_workspace_kind_occurred_idx ON record (workspace_id, kind, occurred_at);
-- Records of a stream: full-snapshot reconcile; clearing stream_id when a stream is deleted.
CREATE INDEX record_stream_idx ON record (stream_id);

-- person: a human seen in the sources of a workspace. Persons are resolved through their aliases;
-- names that match no alias stay separate persons until the user merges them.
CREATE TABLE person (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    display_name text        NOT NULL CHECK (display_name <> ''),
    -- Set on the person who is this principal: "me" in that principal's queries, as in "meetings
    -- I attended". At most one person per principal in a workspace; with the one principal of v1,
    -- at most one "me" per workspace. With more users, each has their own "me".
    principal_id uuid        REFERENCES principal,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, principal_id),
    UNIQUE (workspace_id, id)
);

-- alias: one way a person appears in sources. Resolution looks up (workspace, kind, normalized
-- value); deterministic keys (email, chat user id) come first.
CREATE TABLE alias (
    id               uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id     uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id         uuid        NOT NULL REFERENCES principal,
    person_id        uuid        NOT NULL,
    -- email: a mail address. chat_user_id: a user id in a chat system. meeting_name: a display
    -- name in meetings. git_author: an author name in commits (the address is an email alias).
    kind             text        NOT NULL CHECK (
                                     kind IN ('email', 'chat_user_id', 'meeting_name', 'git_author')
                                 ),
    -- As seen in the source.
    value            text        NOT NULL CHECK (value <> ''),
    -- The lookup key: value normalized by the rules of its kind, e.g. a lowercased address, or a
    -- chat user id qualified by its chat system.
    normalized_value text        NOT NULL CHECK (normalized_value <> ''),
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, person_id) REFERENCES person (workspace_id, id) ON DELETE CASCADE,
    UNIQUE (workspace_id, kind, normalized_value)
);

-- Aliases of a person: showing a person, merging two.
CREATE INDEX alias_person_idx ON alias (person_id);

-- run: one execution of a job (a sync, a retention pass, a reindex) and its report (section 4:
-- steps, durations, record counts, LLM tokens). Linked to the source and stream it worked on.
CREATE TABLE run (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    source_id    uuid,
    stream_id    uuid,
    -- What ran, named by the job: sync, retention, reindex and so on.
    kind         text        NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]*$'),
    -- running: not finished. succeeded: all work done. partial: finished with work left over,
    -- e.g. an incomplete window that left the checkpoint in place. failed: stopped by an error.
    -- cancelled: stopped on purpose, e.g. at shutdown.
    status       text        NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'succeeded', 'partial', 'failed', 'cancelled')
    ),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    -- The run report: steps with durations, record counts, LLM tokens.
    report       jsonb       NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(report) = 'object'),
    -- Why a run failed or is partial.
    error        text,
    -- A run is running exactly while it has no finish time.
    CONSTRAINT run_finished_at_check
        CHECK ((status = 'running') = (finished_at IS NULL) AND finished_at >= started_at),
    -- A foreign key with a NULL column is not checked, so a run of a stream names its source.
    CONSTRAINT run_stream_id_check CHECK (stream_id IS NULL OR source_id IS NOT NULL),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, stream_id) REFERENCES stream (source_id, id) ON DELETE CASCADE
);

-- Runs of a workspace, a source or a stream by time. The newest run of a stream is the last
-- result that status reports for it.
CREATE INDEX run_workspace_started_idx ON run (workspace_id, started_at);
CREATE INDEX run_source_started_idx ON run (source_id, started_at);
CREATE INDEX run_stream_started_idx ON run (stream_id, started_at);
