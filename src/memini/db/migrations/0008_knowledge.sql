-- Knowledge layer: decisions and open questions (knowledge_item), the records they come from
-- (knowledge_evidence), the persons involved (knowledge_participant), the links by which a later
-- item replaces or answers earlier ones (knowledge_supersession), and the extractions that
-- produced items (knowledge_extraction). docs/requirements.md, sections 2 (principles 2 and 4),
-- 3 (knowledge layer), 5 and 6. The code is memini.knowledge. The conventions listed at the top
-- of 0002 hold here too, with one exception: these tables have triggers. They guard the rules
-- below and fill what the database knows better than the writer (times, the record a piece of
-- evidence names); they compute nothing else.
--
-- The journal is append-only, and the database enforces it, not only the code that writes it:
--
-- * A knowledge item never changes after it is recorded, except its status, once:
--
--              link from a newer item, relation replaces --> superseded
--     active --link from a newer item, relation answers ---> closed
--              close -----------------------------------------> closed
--              retract ---------------------------------------> retracted
--
--   invalidated_at is set with the change, to the time of the transaction that makes it. A
--   superseded, closed or retracted item stays as it is for good. Any other UPDATE is refused.
-- * An item has evidence: at least one record it comes from (principle 2), checked when the
--   transaction commits. Evidence and participants are written with their item, in the
--   transaction that records it, and never change afterwards.
-- * A supersession link is stored on the newer item only (knowledge_supersession.item_id); the
--   earlier item holds no pointer back. Inserting the link is what supersedes: the database
--   moves the earlier item to superseded or closed in the same statement. Both items must be
--   active, so an item is superseded or answered at most once and no chain of links can close a
--   cycle.
-- * Nothing is deleted or truncated, except everything at once with its workspace.
--
-- A refusal raises check_violation (SQLSTATE 23514) naming a constraint, as a CHECK would, or
-- foreign_key_violation (23503) for a record that is not in the item's workspace. memini.knowledge
-- maps them to its errors.
--
-- Evidence outlives its record. Retention, forget and deleting a source delete records; the
-- items that cite them stay (principle 4, section 4). So evidence names its record by the
-- record's deterministic id without a foreign key, and keeps a copy of what says where the record
-- was (source, kind, external id and URL), taken from the record when the evidence is written.
-- It keeps no text of the record: no quote, so nothing of the record's content stays once the
-- record is gone. A record ingested again gets the same id, and its evidence finds it again.
--
-- Backups: pg_dump writes triggers after the data, so a full restore loads the rows as they are.
-- A data-only restore into an existing schema fires them and needs --disable-triggers.

-- knowledge_extraction: one record's content read by one extractor. Extraction is idempotent per
-- record content hash and extractor version (section 5): the row is written in the transaction
-- that stores the items found, also when none were found, and a second extraction of the same
-- content by the same extractor finds it and writes nothing.
CREATE TABLE knowledge_extraction (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    -- The record that was read, by its deterministic id; it must exist when the row is written.
    -- Not a foreign key: like the items, the extraction outlives the record, so that the same
    -- content is not extracted again when the record comes back with it.
    record_id    text        NOT NULL CHECK (record_id ~ '^[0-9a-f]{64}$'),
    -- record.content_hash of the content that was read.
    content_hash text        NOT NULL CHECK (content_hash <> ''),
    -- The model and prompt version, e.g. 'qwen3-8b@decisions-v1'. 'manual' is reserved for items
    -- a person recorded.
    extractor    text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_extraction_extractor_check
        CHECK (extractor ~ '^[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,199}$' AND extractor <> 'manual'),
    CONSTRAINT knowledge_extraction_key UNIQUE (workspace_id, record_id, content_hash, extractor),
    -- The target of the item's key, which names the extractor too.
    UNIQUE (workspace_id, id, extractor)
);

-- knowledge_item: a decision or an open question (section 3, knowledge layer).
CREATE TABLE knowledge_item (
    id             uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id   uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id       uuid        NOT NULL REFERENCES principal,
    -- decision: something decided. open_question: something asked and not decided.
    kind           text        NOT NULL CHECK (kind IN ('decision', 'open_question')),
    -- One or two sentences: what was decided or asked.
    statement      text        NOT NULL,
    -- When it was said or asked (event time), as the evidence shows.
    decided_at     timestamptz NOT NULL,
    -- active: current, in the default view. superseded: a newer item replaces it. closed: answered
    -- by a decision, or closed by hand without a replacement. retracted: it was wrong, a misread
    -- extraction or a mistake; it stays in the history marked so.
    status         text        NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'superseded', 'closed', 'retracted')),
    -- When the service learned it: the time of the transaction that recorded it, set by the
    -- database whatever the writer passes.
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    -- When it stopped being current: the time of the transaction that changed its status.
    invalidated_at timestamptz,
    -- The model and prompt version of the extraction, or 'manual' for an item a person recorded.
    -- Extraction never supersedes or answers a manual item.
    extractor      text        NOT NULL DEFAULT 'manual',
    -- The extraction that found the item; NULL for a manual item.
    extraction_id  uuid,
    CONSTRAINT knowledge_item_statement_check CHECK (
        char_length(statement) BETWEEN 1 AND 2000 AND statement ~ '^\S' AND statement ~ '\S$'
    ),
    CONSTRAINT knowledge_item_extractor_check
        CHECK (extractor ~ '^[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,199}$'),
    CONSTRAINT knowledge_item_extraction_check
        CHECK ((extractor = 'manual') = (extraction_id IS NULL)),
    -- An item is invalidated exactly when it is no longer active.
    CONSTRAINT knowledge_item_invalidated_at_check
        CHECK ((status = 'active') = (invalidated_at IS NULL) AND invalidated_at >= recorded_at),
    -- An extracted item names its extraction, in its workspace, with the same extractor.
    CONSTRAINT knowledge_item_extraction_fkey FOREIGN KEY (workspace_id, extraction_id, extractor)
        REFERENCES knowledge_extraction (workspace_id, id, extractor),
    -- The target of the keys of evidence, participants and links.
    UNIQUE (workspace_id, id)
);

-- list_decisions: the active items of a workspace by time; the history with every status.
CREATE INDEX knowledge_item_workspace_status_decided_idx
    ON knowledge_item (workspace_id, status, decided_at);
-- The items of an extraction: a repeated extraction returns them.
CREATE INDEX knowledge_item_extraction_idx ON knowledge_item (extraction_id);

-- knowledge_evidence: a record an item comes from, and where in it (section 3: evidence).
CREATE TABLE knowledge_evidence (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    item_id      uuid        NOT NULL,
    -- The record, by its deterministic id. It must exist in the item's workspace when the row is
    -- written. Not a foreign key: the evidence stays when the record is deleted (see the top).
    record_id    text        NOT NULL CHECK (record_id ~ '^[0-9a-f]{64}$'),
    -- Copied from the record when the row is written, whatever the writer passes, so that the
    -- reference still says what and where the record was after the record is gone. The source is
    -- not a foreign key either.
    source_id    uuid        NOT NULL,
    record_kind  text        NOT NULL
                             CHECK (record_kind IN ('code_file', 'thread', 'meeting', 'document')),
    external_id  text        NOT NULL CHECK (external_id <> ''),
    external_url text,
    -- Where in the record, as the record's kind has it: in a thread the id of the message; in a
    -- meeting a segment, a time offset or a section (summary, action items); in a code file the
    -- commit, optionally with a symbol or lines ('<commit>#Greenhouse.water', '<commit>#L12-L30');
    -- in a document a heading trail or lines. NULL: the whole record (documents only). Opaque:
    -- compared as given.
    anchor       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_evidence_anchor_check
        CHECK (char_length(anchor) BETWEEN 1 AND 1000 AND anchor !~ '[[:cntrl:]]'),
    -- Evidence in a thread or a meeting names the message (section 3).
    CONSTRAINT knowledge_evidence_message_anchor_check
        CHECK (record_kind NOT IN ('thread', 'meeting') OR anchor IS NOT NULL),
    -- Code is a fact at a commit (principle 3): repo@sha:path#symbol.
    CONSTRAINT knowledge_evidence_commit_anchor_check CHECK (
        record_kind <> 'code_file'
        OR coalesce(anchor ~ '^([0-9a-f]{40}|[0-9a-f]{64})(#.+)?$', false)
    ),
    FOREIGN KEY (workspace_id, item_id) REFERENCES knowledge_item (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT knowledge_evidence_item_record_anchor_key
        UNIQUE NULLS NOT DISTINCT (item_id, record_id, anchor)
);

-- The items a record is evidence of: decisions from a thread, evidence of a record that is gone.
CREATE INDEX knowledge_evidence_record_idx ON knowledge_evidence (record_id);

-- knowledge_participant: a person involved in an item (section 3: participants). A person cited
-- here cannot be deleted; merging persons keeps the person rows (section 3, raw layer).
CREATE TABLE knowledge_participant (
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    item_id      uuid        NOT NULL,
    person_id    uuid        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, person_id),
    FOREIGN KEY (workspace_id, item_id) REFERENCES knowledge_item (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT knowledge_participant_person_fkey FOREIGN KEY (workspace_id, person_id)
        REFERENCES person (workspace_id, id)
);

-- list_decisions by person: the items a person took part in.
CREATE INDEX knowledge_participant_person_idx ON knowledge_participant (person_id);

-- knowledge_supersession: the newer item replaces or answers an earlier one (section 3:
-- supersedes). Stored on the newer item only: item_id is the newer item, the row belongs to it.
CREATE TABLE knowledge_supersession (
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    -- The newer item, which stores the link.
    item_id       uuid        NOT NULL,
    -- The earlier item. It becomes superseded (replaces) or closed (answers).
    supersedes_id uuid        NOT NULL,
    -- replaces: the newer item takes the earlier one's place, e.g. a decision reversed.
    -- answers: a decision answers an open question.
    relation      text        NOT NULL CHECK (relation IN ('replaces', 'answers')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_supersession_self_check CHECK (item_id <> supersedes_id),
    PRIMARY KEY (item_id, supersedes_id),
    -- An item is replaced or answered at most once. Also: what replaced an item.
    CONSTRAINT knowledge_supersession_supersedes_id_key UNIQUE (supersedes_id),
    CONSTRAINT knowledge_supersession_item_fkey FOREIGN KEY (workspace_id, item_id)
        REFERENCES knowledge_item (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT knowledge_supersession_supersedes_fkey FOREIGN KEY (workspace_id, supersedes_id)
        REFERENCES knowledge_item (workspace_id, id) ON DELETE CASCADE
);

-- --- Guards ---------------------------------------------------------------------------------

-- Refuses an UPDATE, and a DELETE unless the row's workspace is being deleted: by the time the
-- cascade from workspace reaches a row, the workspace row is gone.
CREATE FUNCTION knowledge_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM workspace WHERE id = OLD.workspace_id) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is append-only: a row is never changed or deleted', TG_TABLE_NAME
        USING ERRCODE = 'check_violation', CONSTRAINT = TG_TABLE_NAME || '_append_only',
              SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
END
$$;

-- TRUNCATE skips row triggers, so it is refused on its own.
CREATE FUNCTION knowledge_no_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: TRUNCATE is refused', TG_TABLE_NAME
        USING ERRCODE = 'check_violation', CONSTRAINT = TG_TABLE_NAME || '_append_only',
              SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
END
$$;

-- knowledge_item: recorded active at the time of its transaction; afterwards only the status
-- changes, once, from active; never deleted but with its workspace.
CREATE FUNCTION knowledge_item_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'active' OR NEW.invalidated_at IS NOT NULL THEN
            RAISE EXCEPTION 'a knowledge item is recorded active'
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'knowledge_item_status_transition',
                      SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
        END IF;
        NEW.recorded_at := now();
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF NOT EXISTS (SELECT 1 FROM workspace WHERE id = OLD.workspace_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'knowledge_item is append-only: an item is never deleted; retract it'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_item_append_only',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;

    IF (to_jsonb(NEW) - 'status' - 'invalidated_at')
            IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'invalidated_at') THEN
        RAISE EXCEPTION 'knowledge_item is append-only: only the status of an item changes;'
                        ' record a new item that supersedes it'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_item_append_only',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    IF OLD.status <> 'active' THEN
        RAISE EXCEPTION 'knowledge item % is %; its status does not change again',
                        OLD.id, OLD.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_item_status_transition',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    IF NEW.status = 'active' THEN
        RAISE EXCEPTION 'knowledge item % stays active: an update only supersedes, closes or'
                        ' retracts an item', OLD.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_item_status_transition',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    IF NEW.status = 'superseded' AND NOT EXISTS (
        SELECT 1 FROM knowledge_supersession s
        WHERE s.supersedes_id = NEW.id AND s.relation = 'replaces'
    ) THEN
        RAISE EXCEPTION 'knowledge item % is superseded by linking the item that replaces it'
                        ' (knowledge_supersession), not by an update', OLD.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_item_status_transition',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    NEW.invalidated_at := greatest(now(), OLD.recorded_at);
    RETURN NEW;
END
$$;

-- At commit: an item without evidence is not stored (principle 2).
CREATE FUNCTION knowledge_item_evidence_required() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM knowledge_item WHERE id = NEW.id)
            AND NOT EXISTS (SELECT 1 FROM knowledge_evidence WHERE item_id = NEW.id) THEN
        RAISE EXCEPTION 'knowledge item % has no evidence: an item names at least one record it'
                        ' comes from', NEW.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_item_evidence_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    RETURN NULL;
END
$$;

-- Evidence and participants are written in the transaction that records their item: its
-- recorded_at is that transaction's time. An item that does not exist is left to the foreign key.
CREATE FUNCTION knowledge_written_with_item() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM knowledge_item WHERE id = NEW.item_id AND recorded_at <> now()) THEN
        RAISE EXCEPTION '% of knowledge item % is written with the item, in the transaction that'
                        ' records it', TG_TABLE_NAME, NEW.item_id
            USING ERRCODE = 'check_violation', CONSTRAINT = TG_TABLE_NAME || '_append_only',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END
$$;

-- Evidence names a record of the item's workspace; what says where the record is comes from the
-- record. An extracted item cites the record it was extracted from.
CREATE FUNCTION knowledge_evidence_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    extracted text;
BEGIN
    SELECT r.source_id, r.kind, r.external_id, r.external_url
    INTO NEW.source_id, NEW.record_kind, NEW.external_id, NEW.external_url
    FROM record r
    WHERE r.id = NEW.record_id AND r.workspace_id = NEW.workspace_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no record % in the workspace of the item', NEW.record_id
            USING ERRCODE = 'foreign_key_violation',
                  CONSTRAINT = 'knowledge_evidence_record_id_fkey',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    SELECT x.record_id INTO extracted
    FROM knowledge_item i JOIN knowledge_extraction x ON x.id = i.extraction_id
    WHERE i.id = NEW.item_id;
    IF extracted IS NOT NULL AND extracted <> NEW.record_id THEN
        RAISE EXCEPTION 'an extracted item cites the record it was extracted from'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'knowledge_evidence_extraction_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END
$$;

-- An extraction names a record of its workspace.
CREATE FUNCTION knowledge_extraction_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM record WHERE id = NEW.record_id AND workspace_id = NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'no record % in the workspace of the extraction', NEW.record_id
            USING ERRCODE = 'foreign_key_violation',
                  CONSTRAINT = 'knowledge_extraction_record_id_fkey',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END
$$;

-- Before a link is stored: both items are active, the link closes no cycle, the relation fits
-- the kinds, the newer item was said no earlier than the one it supersedes, and extraction does
-- not supersede a manual item.
CREATE FUNCTION knowledge_supersession_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    newer knowledge_item;
    older knowledge_item;
BEGIN
    -- Both items stay as they are until this transaction ends. NO KEY UPDATE, taken in id order:
    -- two transactions linking the same items wait for each other instead of deadlocking, and
    -- the KEY SHARE locks of other transactions' foreign key checks do not block it.
    PERFORM 1 FROM knowledge_item
    WHERE id IN (NEW.item_id, NEW.supersedes_id)
    ORDER BY id
    FOR NO KEY UPDATE;
    SELECT * INTO newer FROM knowledge_item
    WHERE id = NEW.item_id AND workspace_id = NEW.workspace_id;
    SELECT * INTO older FROM knowledge_item
    WHERE id = NEW.supersedes_id AND workspace_id = NEW.workspace_id;
    IF newer.id IS NULL OR older.id IS NULL THEN
        RETURN NEW;  -- the foreign keys refuse the row
    END IF;

    IF EXISTS (
        WITH RECURSIVE earlier (id) AS (
            SELECT NEW.supersedes_id
            UNION
            SELECT s.supersedes_id
            FROM knowledge_supersession s JOIN earlier e ON s.item_id = e.id
        )
        SELECT 1 FROM earlier WHERE id = NEW.item_id
    ) THEN
        RAISE EXCEPTION 'knowledge item % already supersedes % through a chain of links: the'
                        ' link would close a cycle', NEW.supersedes_id, NEW.item_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_supersession_cycle_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    IF newer.status <> 'active' THEN
        RAISE EXCEPTION 'knowledge item % is %: only an active item supersedes others',
                        newer.id, newer.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_supersession_active_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    IF older.status <> 'active' THEN
        RAISE EXCEPTION 'knowledge item % is %: only an active item is superseded',
                        older.id, older.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_supersession_active_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    IF NEW.relation = 'answers' AND (newer.kind <> 'decision' OR older.kind <> 'open_question') THEN
        RAISE EXCEPTION 'a decision answers an open question; other items replace each other'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'knowledge_supersession_relation_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    IF newer.decided_at < older.decided_at THEN
        RAISE EXCEPTION 'knowledge item % was said before %: an item supersedes only earlier ones',
                        newer.id, older.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_supersession_order_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    IF newer.extractor <> 'manual' AND older.extractor = 'manual' THEN
        RAISE EXCEPTION 'knowledge item % was recorded by hand: extraction never supersedes or'
                        ' answers a manual item', older.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'knowledge_supersession_manual_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END
$$;

-- After a link is stored: the earlier item leaves the default view. knowledge_item_guard sets
-- invalidated_at and accepts superseded because the link now exists.
CREATE FUNCTION knowledge_supersession_apply() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE knowledge_item
    SET status = CASE NEW.relation WHEN 'replaces' THEN 'superseded' ELSE 'closed' END
    WHERE id = NEW.supersedes_id;
    RETURN NULL;
END
$$;

CREATE TRIGGER knowledge_extraction_record BEFORE INSERT ON knowledge_extraction
    FOR EACH ROW EXECUTE FUNCTION knowledge_extraction_record();
CREATE TRIGGER knowledge_extraction_append_only BEFORE UPDATE OR DELETE ON knowledge_extraction
    FOR EACH ROW EXECUTE FUNCTION knowledge_append_only();

CREATE TRIGGER knowledge_item_guard BEFORE INSERT OR UPDATE OR DELETE ON knowledge_item
    FOR EACH ROW EXECUTE FUNCTION knowledge_item_guard();
CREATE CONSTRAINT TRIGGER knowledge_item_evidence_check AFTER INSERT ON knowledge_item
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION knowledge_item_evidence_required();

CREATE TRIGGER knowledge_evidence_written_with_item BEFORE INSERT ON knowledge_evidence
    FOR EACH ROW EXECUTE FUNCTION knowledge_written_with_item();
CREATE TRIGGER knowledge_evidence_record BEFORE INSERT ON knowledge_evidence
    FOR EACH ROW EXECUTE FUNCTION knowledge_evidence_record();
CREATE TRIGGER knowledge_evidence_append_only BEFORE UPDATE OR DELETE ON knowledge_evidence
    FOR EACH ROW EXECUTE FUNCTION knowledge_append_only();

CREATE TRIGGER knowledge_participant_written_with_item BEFORE INSERT ON knowledge_participant
    FOR EACH ROW EXECUTE FUNCTION knowledge_written_with_item();
CREATE TRIGGER knowledge_participant_append_only BEFORE UPDATE OR DELETE ON knowledge_participant
    FOR EACH ROW EXECUTE FUNCTION knowledge_append_only();

CREATE TRIGGER knowledge_supersession_check BEFORE INSERT ON knowledge_supersession
    FOR EACH ROW EXECUTE FUNCTION knowledge_supersession_check();
CREATE TRIGGER knowledge_supersession_apply AFTER INSERT ON knowledge_supersession
    FOR EACH ROW EXECUTE FUNCTION knowledge_supersession_apply();
CREATE TRIGGER knowledge_supersession_append_only
    BEFORE UPDATE OR DELETE ON knowledge_supersession
    FOR EACH ROW EXECUTE FUNCTION knowledge_append_only();

CREATE TRIGGER knowledge_extraction_no_truncate BEFORE TRUNCATE ON knowledge_extraction
    FOR EACH STATEMENT EXECUTE FUNCTION knowledge_no_truncate();
CREATE TRIGGER knowledge_item_no_truncate BEFORE TRUNCATE ON knowledge_item
    FOR EACH STATEMENT EXECUTE FUNCTION knowledge_no_truncate();
CREATE TRIGGER knowledge_evidence_no_truncate BEFORE TRUNCATE ON knowledge_evidence
    FOR EACH STATEMENT EXECUTE FUNCTION knowledge_no_truncate();
CREATE TRIGGER knowledge_participant_no_truncate BEFORE TRUNCATE ON knowledge_participant
    FOR EACH STATEMENT EXECUTE FUNCTION knowledge_no_truncate();
CREATE TRIGGER knowledge_supersession_no_truncate BEFORE TRUNCATE ON knowledge_supersession
    FOR EACH STATEMENT EXECUTE FUNCTION knowledge_no_truncate();
