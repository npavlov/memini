-- Decisions by project and domain: what a record, a source or an item says it belongs to, and the
-- answers of the assign_domains job. docs/requirements.md, sections 3 (Decisions by project and
-- domain), 5 (Domain assignment) and 6 (Manual operations). The code is memini.decisions.context
-- and memini.decisions.assign. The conventions listed at the top of 0002 hold here too.
--
-- * Outside the journal. None of these tables is part of the knowledge journal (0008), and the
--   journal's tables and triggers do not change: like knowledge_item_vector (0017), a row here
--   names an item and goes with it, and has none of the journal's triggers. A link is added and
--   removed; the item it names never changes.
-- * By name. A project is named by its name and a domain by its address (domain or
--   domain/subdomain), as put_project writes them; they are not foreign keys to the catalog
--   (0018). A name is a handle there too: put_project compares a document with what is stored by
--   name, and a domain removed and written again is the same name with a new id. So a link
--   survives a rewrite of the project, and one that names what the catalog no longer holds is left
--   out of the answers until the name is back. What a decision belongs to is computed when a
--   question is asked (memini.decisions.context), from these rows, the evidence and the masks.
-- * Origins, in the order they take precedence: push (record_catalog), default (source_catalog),
--   manual and model (knowledge_item_domain); code is computed from the masks and stored nowhere.

-- record_catalog: the project and domains whoever pushed a record gave with it (the optional
-- project and domains of the push contract). One row per record, written as a diff by the batch
-- that stores the record: the same record again with the same fields writes nothing, and a record
-- pushed again without them loses its row, as a record is replaced whole. It goes with its record:
-- retention, forget and a tombstone's later removal take it.
CREATE TABLE record_catalog (
    record_id    text        PRIMARY KEY,
    workspace_id uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id     uuid        NOT NULL REFERENCES principal,
    source_id    uuid        NOT NULL,
    record_kind  text        NOT NULL,
    project      text        NOT NULL CHECK (project ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    -- Addresses: domain or domain/subdomain, of the project.
    domains      text[]      NOT NULL DEFAULT '{}',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT record_catalog_domains_check CHECK (
        cardinality(domains) <= 16
        AND array_position(domains, NULL) IS NULL
        AND array_to_string(domains, ' ')
            ~ '^(([a-z0-9][a-z0-9_-]{0,62})(/[a-z0-9][a-z0-9_-]{0,62})?( |$))*$'
    ),
    FOREIGN KEY (workspace_id, source_id, record_kind, record_id)
        REFERENCES record (workspace_id, source_id, kind, id) ON DELETE CASCADE
);

-- source_catalog: the project and domains of the records of a source, or of one of its streams,
-- that were pushed without their own. A stream is named by its key, so a default can be set
-- before the stream's first batch; the default of a stream comes before the source's.
CREATE TABLE source_catalog (
    id            uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    source_id     uuid        NOT NULL,
    -- NULL: every stream of the source.
    stream_key    text        CHECK (stream_key <> ''),
    project       text        NOT NULL CHECK (project ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    domains       text[]      NOT NULL DEFAULT '{}',
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid        NOT NULL REFERENCES principal,
    updated_token uuid        REFERENCES token ON DELETE SET NULL,
    CONSTRAINT source_catalog_domains_check CHECK (
        cardinality(domains) <= 16
        AND array_position(domains, NULL) IS NULL
        AND array_to_string(domains, ' ')
            ~ '^(([a-z0-9][a-z0-9_-]{0,62})(/[a-z0-9][a-z0-9_-]{0,62})?( |$))*$'
    ),
    FOREIGN KEY (workspace_id, source_id) REFERENCES source (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT source_catalog_key UNIQUE NULLS NOT DISTINCT (source_id, stream_key)
);

CREATE INDEX source_catalog_workspace_idx ON source_catalog (workspace_id);

-- knowledge_item_domain: a link of an item to a project, or to a domain of a project, made by a
-- person (manual: record_decision, or the link route) or by the assign_domains job (model). A
-- person's link replaces the model's of the same domain; the job never writes over a person's.
CREATE TABLE knowledge_item_domain (
    id            uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  uuid        NOT NULL,
    owner_id      uuid        NOT NULL REFERENCES principal,
    item_id       uuid        NOT NULL,
    project       text        NOT NULL CHECK (project ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    -- NULL: the project, without a domain.
    domain        text        CHECK (
        domain ~ '^[a-z0-9][a-z0-9_-]{0,62}(/[a-z0-9][a-z0-9_-]{0,62})?$'
    ),
    origin        text        NOT NULL CHECK (origin IN ('manual', 'model')),
    -- Who made it: the principal and the token of the request (none for the job, or an operator),
    -- and for a model link the model and prompt version that chose it.
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    uuid        NOT NULL REFERENCES principal,
    created_token uuid        REFERENCES token ON DELETE SET NULL,
    assigner      text,
    CONSTRAINT knowledge_item_domain_assigner_check CHECK (
        (origin = 'model') = (assigner IS NOT NULL)
    ),
    FOREIGN KEY (workspace_id, item_id) REFERENCES knowledge_item (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT knowledge_item_domain_key UNIQUE NULLS NOT DISTINCT (item_id, project, domain)
);

-- The links of a workspace: what list_decisions reads to filter by project and domain.
CREATE INDEX knowledge_item_domain_workspace_idx ON knowledge_item_domain (workspace_id);

-- knowledge_domain_assignment: the last answer of the assign_domains job for an item, "none"
-- included, so that a run again asks the model nothing it has answered: an item is asked again
-- only by another assigner (model and prompt version) or when the domains it would be shown
-- changed (catalog_hash, SHA-256 of what the prompt listed).
CREATE TABLE knowledge_domain_assignment (
    item_id      uuid        PRIMARY KEY,
    workspace_id uuid        NOT NULL,
    owner_id     uuid        NOT NULL REFERENCES principal,
    assigner     text        NOT NULL CHECK (assigner <> ''),
    catalog_hash text        NOT NULL CHECK (catalog_hash ~ '^[0-9a-f]{64}$'),
    answer       text        NOT NULL CHECK (answer IN ('domains', 'none')),
    answered_at  timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, item_id) REFERENCES knowledge_item (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX knowledge_domain_assignment_workspace_idx
    ON knowledge_domain_assignment (workspace_id);
