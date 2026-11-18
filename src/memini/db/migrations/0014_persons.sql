-- Persons: resolution through aliases, reversible merges, "me", and the links between records and
-- the persons in them. docs/requirements.md, sections 3 (Person) and 6 (list_meetings,
-- list_messages). The code is memini.persons. The conventions listed at the top of 0002 hold here
-- too, with one exception: person has a trigger, which refuses a merge that would close a cycle.
--
-- * Alias kinds. A display name is one kind, wherever it was seen (a chat, a mail header, a
--   meeting, a document): meeting_name of 0002 becomes display_name. Resolution looks up a name
--   only for an identity that carries no deterministic key (email, chat user id), and a name
--   that matches no alias makes a person of its own, until the user merges it.
-- * Merges. Merging person B into person A sets B.merged_into = A and changes nothing else: B's
--   aliases, its links to records and the knowledge items that cite it (knowledge_participant is
--   append-only) keep naming B. Reads resolve a person through merged_into to its canonical
--   person, the end of the chain (person_root), and gather the persons merged into it
--   (person_members). Splitting B clears B.merged_into, which restores exactly what was there
--   before the merge, persons merged into B included. A person is never deleted by a merge.
-- * record_person: who appears in a record, and how. Derived from the record's payload at ingest
--   (principle 1), so it goes with its record (ON DELETE CASCADE) and is written again whenever
--   the record's row is.

ALTER TABLE alias DROP CONSTRAINT alias_kind_check;
UPDATE alias SET kind = 'display_name' WHERE kind = 'meeting_name';
-- email: a mail address. chat_user_id: a user id in a chat or meeting system, as the client sends
-- it. display_name: a name as a source shows it. git_author: an author name in commits.
ALTER TABLE alias ADD CONSTRAINT alias_kind_check
    CHECK (kind IN ('email', 'chat_user_id', 'display_name', 'git_author'));

ALTER TABLE person
    -- The person this one was merged into; NULL for a canonical person. In the same workspace.
    ADD COLUMN merged_into uuid,
    -- When it was merged; set exactly while merged_into is.
    ADD COLUMN merged_at   timestamptz,
    ADD CONSTRAINT person_merged_into_self_check CHECK (merged_into <> id),
    ADD CONSTRAINT person_merged_at_check CHECK ((merged_into IS NULL) = (merged_at IS NULL)),
    -- A person that others are merged into cannot be deleted while they are.
    ADD CONSTRAINT person_merged_into_fkey FOREIGN KEY (workspace_id, merged_into)
        REFERENCES person (workspace_id, id);

-- The persons merged into a person: reads gather them, a split finds them.
CREATE INDEX person_merged_into_idx ON person (merged_into);

-- The canonical person of a person: merged_into followed to its end. NULL for no such person.
CREATE FUNCTION person_root(person_id uuid) RETURNS uuid
    LANGUAGE sql STABLE STRICT PARALLEL SAFE
BEGIN ATOMIC
    WITH RECURSIVE up (id, merged_into) AS (
        SELECT p.id, p.merged_into FROM person p WHERE p.id = person_root.person_id
        UNION
        SELECT p.id, p.merged_into FROM person p JOIN up ON p.id = up.merged_into
    )
    SELECT up.id FROM up WHERE up.merged_into IS NULL;
END;

-- A person and every person merged into it, directly or through others. For the persons a
-- canonical person stands for: person_members(person_root(x)).
CREATE FUNCTION person_members(person_id uuid) RETURNS SETOF uuid
    LANGUAGE sql STABLE STRICT PARALLEL SAFE
BEGIN ATOMIC
    WITH RECURSIVE down (id) AS (
        SELECT p.id FROM person p WHERE p.id = person_members.person_id
        UNION
        SELECT p.id FROM person p JOIN down ON p.merged_into = down.id
    )
    SELECT down.id FROM down;
END;

-- Refuses a merge into a person that is merged, directly or through others, into this one.
CREATE FUNCTION person_merge_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        WITH RECURSIVE up (id) AS (
            SELECT NEW.merged_into
            UNION
            SELECT p.merged_into FROM person p JOIN up ON p.id = up.id
            WHERE p.merged_into IS NOT NULL
        )
        SELECT 1 FROM up WHERE up.id = NEW.id
    ) THEN
        RAISE EXCEPTION 'person % is merged into % already: the merge would close a cycle',
                        NEW.merged_into, NEW.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'person_merge_cycle_check',
                  SCHEMA = TG_TABLE_SCHEMA, TABLE = TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER person_merge_check BEFORE INSERT OR UPDATE OF merged_into ON person
    FOR EACH ROW WHEN (NEW.merged_into IS NOT NULL) EXECUTE FUNCTION person_merge_check();

-- record_person: a person in a record, with a role. Written by memini.persons at ingest, in the
-- transaction that stores the record.
CREATE TABLE record_person (
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    source_id    uuid        NOT NULL,
    -- The record's kind, kept so that a list filters by it without a join.
    record_kind  text        NOT NULL,
    record_id    text        NOT NULL,
    -- The person the alias that matched names, not its canonical person: a merge changes no link,
    -- and reads resolve through person_root.
    person_id    uuid        NOT NULL,
    -- author: wrote a message or a document. recipient: a message was sent to them. mention: a
    -- message mentions them. participant: took part in a meeting.
    role         text        NOT NULL
                             CHECK (role IN ('author', 'recipient', 'mention', 'participant')),
    -- Where in the record: the id of the message in a thread, if the message has one. NULL: the
    -- record as a whole. Opaque: compared as given.
    anchor       text,
    -- When: the message's sent_at, the meeting's started_at, else the record's occurred_at.
    occurred_at  timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT record_person_anchor_check
        CHECK (char_length(anchor) BETWEEN 1 AND 1000 AND anchor !~ '[[:cntrl:]]'),
    CONSTRAINT record_person_record_fkey FOREIGN KEY (workspace_id, source_id, record_kind, record_id)
        REFERENCES record (workspace_id, source_id, kind, id) ON DELETE CASCADE,
    -- A person in a record cannot be deleted; merging keeps the row.
    CONSTRAINT record_person_person_fkey FOREIGN KEY (workspace_id, person_id)
        REFERENCES person (workspace_id, id),
    -- One row per person, role and place in a record. Also: the links of a record.
    CONSTRAINT record_person_key UNIQUE NULLS NOT DISTINCT (record_id, person_id, role, anchor)
);

-- list_meetings, list_messages: the records where a person has a role, by time.
CREATE INDEX record_person_person_role_occurred_idx
    ON record_person (person_id, role, occurred_at);
