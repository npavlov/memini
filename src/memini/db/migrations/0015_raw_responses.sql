-- Raw responses of MCP recipe runs: every tool result an executor received, stored after
-- redaction and otherwise as the server returned it, so that a parser fix is replayed without
-- calling the source again. docs/requirements.md, section 4 (MCP recipes; Raw responses and
-- replay). The code is memini.responses; the parser protocol is memini.recipes.parsing. The
-- conventions at the top of 0002 hold here too.
--
-- * raw_window: one window [window_from, window_to) of a stream that an executor ran. A parser
--   reads the calls of one window together, and replay goes through windows in the order they
--   were started.
-- * raw_response: one call of a window: which call it was (list or detail, the tool, the
--   arguments as sent, the page, the listed item), when its result came, and the hash of the
--   result's bytes. Unique per window by its identity (call_key, a hash of call, tool and
--   arguments): the same call stored again with the same result writes nothing, with another
--   result (a retry) replaces it.
-- * raw_body: the bytes of a result, stored once per source by content hash: a page fetched again
--   unchanged, by a later run or a retry, adds a raw_response row of a few hundred bytes and no
--   body. The bytes are the MCP result object (content blocks, structuredContent, isError) with
--   secrets and card numbers redacted, as canonical JSON; nothing else rewrites them.
--
-- Forget: raw_response.record_ids holds the records the parser built from the response, as of the
-- window's last parse. Forgetting a record sets body_hash to NULL on every response that names
-- it, marks their windows pruned_at and deletes the bodies no response references any more; the
-- call itself (arguments, item id, attribution) stays, so replay still knows which window last
-- held the record. Retention deletes whole windows that ended before its cutoff.

CREATE TABLE raw_window (
    id             uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id   uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id       uuid        NOT NULL REFERENCES principal,
    source_id      uuid        NOT NULL,
    stream_id      uuid        NOT NULL,
    -- The run that made the calls; NULL once its row is gone.
    run_id         uuid        REFERENCES run ON DELETE SET NULL,
    -- The planner's kind of window (memini.intake).
    kind           text        NOT NULL CONSTRAINT raw_window_kind_check
                               CHECK (kind IN ('initial', 'forward', 'backfill')),
    window_from    timestamptz NOT NULL,
    window_to      timestamptz NOT NULL,
    -- memini.recipes.recipe_digest of the recipe the calls were planned from.
    recipe_digest  text        NOT NULL CONSTRAINT raw_window_recipe_digest_check
                               CHECK (recipe_digest ~ '^[0-9a-f]{64}$'),
    -- What the server reported when the session started (serverInfo).
    server_name    text        NOT NULL CONSTRAINT raw_window_server_name_check
                               CHECK (server_name <> ''),
    server_version text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    -- When the window's responses were first parsed and its records sent to ingest. NULL: its
    -- calls are being made, or its run died first; such a window is never replayed.
    parsed_at      timestamptz,
    -- What the last parse found: every check passed.
    complete       boolean,
    -- Set when forget removed the body of one of its responses; a pruned window is never claimed
    -- complete again.
    pruned_at      timestamptz,
    CONSTRAINT raw_window_bounds_check CHECK (window_from < window_to),
    CONSTRAINT raw_window_parsed_check CHECK ((parsed_at IS NULL) = (complete IS NULL)),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, stream_id) REFERENCES stream (source_id, id) ON DELETE CASCADE,
    UNIQUE (source_id, id)
);

-- Replay: the windows of a source, or of a stream, in the order they were started.
CREATE INDEX raw_window_source_started_idx ON raw_window (source_id, started_at, id);
CREATE INDEX raw_window_stream_started_idx ON raw_window (stream_id, started_at);
-- Replay of one run; and the ON DELETE SET NULL from run.
CREATE INDEX raw_window_run_idx ON raw_window (run_id);
-- Retention: the windows of a source that ended before a time.
CREATE INDEX raw_window_source_to_idx ON raw_window (source_id, window_to);

CREATE TABLE raw_body (
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    source_id    uuid        NOT NULL,
    -- SHA-256 of body, lowercase hex.
    hash         text        NOT NULL,
    body         bytea       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, hash),
    CONSTRAINT raw_body_hash_check CHECK (hash = encode(sha256(body), 'hex')),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE
);

-- Deleting a workspace: its bodies.
CREATE INDEX raw_body_workspace_idx ON raw_body (workspace_id);

CREATE TABLE raw_response (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    source_id    uuid        NOT NULL,
    window_id    uuid        NOT NULL,
    -- The call's place in its window, from 1, in the order the calls were made.
    seq          integer     NOT NULL CONSTRAINT raw_response_seq_check CHECK (seq > 0),
    call         text        NOT NULL CONSTRAINT raw_response_call_check
                             CHECK (call IN ('list', 'detail')),
    tool         text        NOT NULL CONSTRAINT raw_response_tool_check CHECK (tool <> ''),
    -- As sent, with secrets redacted.
    arguments    jsonb       NOT NULL CONSTRAINT raw_response_arguments_check
                             CHECK (jsonb_typeof(arguments) = 'object'),
    -- Hash of call, tool and the canonical JSON of arguments: the call's identity in its window.
    call_key     text        NOT NULL CONSTRAINT raw_response_call_key_check
                             CHECK (call_key ~ '^[0-9a-f]{64}$'),
    -- The page's place in its listing or its item's detail call, from 0.
    page         integer     NOT NULL CONSTRAINT raw_response_page_check CHECK (page >= 0),
    -- The listed item a detail call was made for.
    item_id      text,
    -- The result's bytes in raw_body. NULL: forget removed them.
    body_hash    text,
    -- The server reported the call as failed (isError).
    is_error     boolean     NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    -- The ids of the records the parser built from this response (sorted, without repeats) as of
    -- the window's last parse; NULL before the first.
    record_ids   text[],
    CONSTRAINT raw_response_item_check CHECK ((call = 'detail') = (item_id IS NOT NULL)),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, window_id) REFERENCES raw_window (source_id, id) ON DELETE CASCADE,
    -- NO ACTION, checked at the end of the statement: deleting a source or a workspace removes
    -- responses and bodies in one statement.
    FOREIGN KEY (source_id, body_hash) REFERENCES raw_body (source_id, hash),
    UNIQUE (window_id, seq),
    UNIQUE (window_id, call_key)
);

-- Whether a body is still referenced: deleting bodies no response needs.
CREATE INDEX raw_response_body_idx ON raw_response (source_id, body_hash);
-- Forget: the responses that hold content of a record.
CREATE INDEX raw_response_record_ids_idx ON raw_response USING gin (record_ids);
