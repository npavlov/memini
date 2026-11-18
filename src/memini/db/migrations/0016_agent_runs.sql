-- Runs of the agent executor: an intake of one stream of an mcp source made inside an interactive
-- agent session. docs/requirements.md, section 4 (MCP recipes; Agent executor). The code is
-- memini.executors.agent, the routes memini.api.agent_runs. The conventions at the top of 0002 hold
-- here too.
--
-- The service plans the stream's windows at a time pinned when the run starts, hands out the calls
-- that would complete the current window one at a time, and stores each raw response a capture hook
-- posts back for a call it handed out (raw_window and raw_response, 0015). At finish it parses and
-- ingests the windows and writes the run report (a run row of kind agent_sync).
--
-- * agent_run: the run: its stream, the pinned time, the recipe digest and the server it was
--   planned for, the plan as the run report shows it, the counts of the session, when it was closed
--   to calls and when it finished.
-- * agent_run_window: the windows of the plan in the order they run, and the raw window of each
--   once it is opened.
-- * agent_call: each call handed out, known by an id the hook posts back with the response; a
--   response for a call that is not here is refused. Deleted when the run finishes: the raw
--   responses keep the calls.

CREATE TABLE agent_run (
    id             uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id   uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id       uuid        NOT NULL REFERENCES principal,
    source_id      uuid        NOT NULL,
    stream_id      uuid        NOT NULL,
    -- The database time pinned when the run started: no window ends after it.
    as_of          timestamptz NOT NULL,
    -- memini.recipes.recipe_digest of the recipe the calls are planned from.
    recipe_digest  text        NOT NULL CONSTRAINT agent_run_recipe_digest_check
                               CHECK (recipe_digest ~ '^[0-9a-f]{64}$'),
    -- The server the calls go to: the recipe's, with the version the session reported, if any.
    server_name    text        NOT NULL CONSTRAINT agent_run_server_name_check
                               CHECK (server_name <> ''),
    server_version text,
    -- The plan (memini.intake.Plan.to_json), for the run report.
    plan           jsonb       NOT NULL CONSTRAINT agent_run_plan_check
                               CHECK (jsonb_typeof(plan) = 'object'),
    -- Counts of the session (calls handed out, responses stored ...), added to the run report.
    counts         jsonb       NOT NULL DEFAULT '{}' CONSTRAINT agent_run_counts_check
                               CHECK (jsonb_typeof(counts) = 'object'),
    started_at     timestamptz NOT NULL DEFAULT now(),
    -- Set when finish starts: no call is handed out or answered after it.
    closed_at      timestamptz,
    -- Set when finish has ingested the windows and written the run report.
    finished_at    timestamptz,
    -- The run report, written at finish.
    run_id         uuid        REFERENCES run ON DELETE SET NULL,
    CONSTRAINT agent_run_finished_check CHECK (finished_at IS NULL OR closed_at IS NOT NULL),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, stream_id) REFERENCES stream (source_id, id) ON DELETE CASCADE,
    UNIQUE (source_id, id)
);

-- One unfinished run per stream: two sessions would hand out the calls of the same windows.
CREATE UNIQUE INDEX agent_run_open_idx ON agent_run (stream_id) WHERE finished_at IS NULL;
-- The ON DELETE SET NULL from run.
CREATE INDEX agent_run_run_idx ON agent_run (run_id);

CREATE TABLE agent_run_window (
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    source_id    uuid        NOT NULL,
    agent_run_id uuid        NOT NULL,
    -- The window's place in the plan, from 0: the initial or forward windows oldest first, then
    -- the backfill windows newest first.
    position     integer     NOT NULL CONSTRAINT agent_run_window_position_check
                             CHECK (position >= 0),
    kind         text        NOT NULL CONSTRAINT agent_run_window_kind_check
                             CHECK (kind IN ('initial', 'forward', 'backfill')),
    window_from  timestamptz NOT NULL,
    window_to    timestamptz NOT NULL,
    -- pending: not opened yet. open: its calls are being handed out. called: nothing more to hand
    -- out. skipped: an earlier window of its chain ended incomplete. ingested: parsed and ingested
    -- at finish.
    status       text        NOT NULL DEFAULT 'pending' CONSTRAINT agent_run_window_status_check
                             CHECK (status IN ('pending', 'open', 'called', 'skipped', 'ingested')),
    -- The raw window, once opened; NULL again if it was deleted (retention, a later parse).
    window_id    uuid,
    -- The window's claim at finish: every check passed.
    complete     boolean,
    PRIMARY KEY (agent_run_id, position),
    CONSTRAINT agent_run_window_bounds_check CHECK (window_from < window_to),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, agent_run_id) REFERENCES agent_run (source_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, window_id) REFERENCES raw_window (source_id, id)
        ON DELETE SET NULL (window_id)
);

-- The ON DELETE SET NULL from raw_window.
CREATE INDEX agent_run_window_window_idx ON agent_run_window (source_id, window_id);

CREATE TABLE agent_call (
    -- The id handed out with the call and posted back with its response. Random, not time-ordered.
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    source_id    uuid        NOT NULL,
    agent_run_id uuid        NOT NULL,
    window_id    uuid        NOT NULL,
    call         text        NOT NULL CONSTRAINT agent_call_call_check
                             CHECK (call IN ('list', 'detail')),
    tool         text        NOT NULL CONSTRAINT agent_call_tool_check CHECK (tool <> ''),
    -- As handed out (memini.recipes.MissingCall.arguments): the response must name the same.
    arguments    jsonb       NOT NULL CONSTRAINT agent_call_arguments_check
                             CHECK (jsonb_typeof(arguments) = 'object'),
    -- Hash of call, tool and the canonical JSON of arguments, as raw_response.call_key.
    call_key     text        NOT NULL CONSTRAINT agent_call_call_key_check
                             CHECK (call_key ~ '^[0-9a-f]{64}$'),
    page         integer     NOT NULL CONSTRAINT agent_call_page_check CHECK (page >= 0),
    item_id      text,
    -- The check the call makes pass, and why it is needed: positions, never values
    -- (memini.recipes.MissingCall).
    missing_check text       NOT NULL CONSTRAINT agent_call_missing_check_check CHECK (
                                 missing_check IN ('failed', 'page', 'truncated', 'max_pages',
                                                   'detail', 'item', 'page_numbers',
                                                   'reply_counts', 'totals')
                             ),
    reason       text        NOT NULL,
    -- How often the call was handed out: a call not answered is handed out again, a few times.
    handouts     integer     NOT NULL DEFAULT 1 CONSTRAINT agent_call_handouts_check
                             CHECK (handouts > 0),
    issued_at    timestamptz NOT NULL DEFAULT now(),
    -- When its first response was stored.
    answered_at  timestamptz,
    -- When it was given up, handed out too often without a response.
    abandoned_at timestamptz,
    CONSTRAINT agent_call_item_check CHECK ((call = 'detail') = (item_id IS NOT NULL)),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, agent_run_id) REFERENCES agent_run (source_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, window_id) REFERENCES raw_window (source_id, id) ON DELETE CASCADE
);

-- The attempts at a call of a window; the calls of a run.
CREATE INDEX agent_call_run_idx ON agent_call (agent_run_id, window_id, call_key);
-- The ON DELETE CASCADE from raw_window.
CREATE INDEX agent_call_window_idx ON agent_call (source_id, window_id);
-- One call outstanding per run: an agent makes one call at a time.
CREATE UNIQUE INDEX agent_call_outstanding_idx ON agent_call (agent_run_id)
    WHERE answered_at IS NULL AND abandoned_at IS NULL;
