-- The catalog: what the code of a workspace belongs to. A project has services (a repository or a
-- directory of one) and domains (business areas, at most two levels: a domain and its
-- subdomains); a domain or subdomain names code as path masks inside a service (domain_code).
-- docs/requirements.md, sections 3 (Catalog) and 6 (Catalog tools). The code is memini.catalog.
-- The conventions listed at the top of 0002 hold here too.
--
-- * Authored data. The catalog is neither raw nor derived: an agent or a person writes it
--   (put_project), nothing rebuilds it from records, and nothing is derived from it into other
--   tables. What a file belongs to is computed from the masks when a question is asked. No chunk
--   or record holds a project, a service or a domain, so a change of the catalog needs no reindex
--   and is not part of the index fingerprint.
-- * It goes with its workspace (ON DELETE CASCADE), and a service goes with its repository: when
--   a git source is deleted, the services over it and the masks that name them are deleted too.
--   The domains stay.
-- * Isolation by foreign key, as everywhere: a service names its repository, and a mask its
--   domain and its service, together with the workspace. A domain may name a service of another
--   project, never of another workspace.
-- * Who and when. Every row keeps who changed it last (the principal and the token of the
--   request; no token for an operator acting on the database) and when. A project also counts
--   its changes: version goes up by one with every write that changes anything in the project,
--   and its updated_* columns move with it.
-- * Names are handles, as those of workspaces and sources. Titles and descriptions are free text
--   an agent wrote from what repositories hold: data, never instructions (principle 11). The
--   code redacts secrets in them before they are stored, as ingest does.

-- A service names a git source and nothing else: the kind is part of the key it refers to.
ALTER TABLE source ADD UNIQUE (workspace_id, id, kind);

-- project: a product or a system of the workspace.
CREATE TABLE project (
    id            uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    -- Unique in the workspace.
    name          text        NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    title         text        NOT NULL DEFAULT '' CHECK (char_length(title) <= 200),
    description   text        NOT NULL DEFAULT '' CHECK (char_length(description) <= 4000),
    -- How many writes have changed the project, its services, domains or masks. 0: the row was
    -- made in a transaction that has not written its document yet; no committed row holds it.
    -- put_project compares it with the version the caller read (if_version).
    version       integer     NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- The last change of anything in the project: the time, the principal and the token.
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid        NOT NULL REFERENCES principal,
    updated_token uuid        REFERENCES token ON DELETE SET NULL,
    UNIQUE (workspace_id, name),
    UNIQUE (workspace_id, id)
);

-- service: a deployable or a library of a project: a git source, whole or one directory of it.
CREATE TABLE service (
    id            uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    project_id    uuid        NOT NULL,
    -- Unique in the project.
    name          text        NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    description   text        NOT NULL DEFAULT '' CHECK (char_length(description) <= 4000),
    source_id     uuid        NOT NULL,
    -- Always git: with the key below, a service cannot name a source of another kind.
    source_kind   text        NOT NULL DEFAULT 'git' CHECK (source_kind = 'git'),
    -- The directory of the repository the service is, from its root, without a slash at either
    -- end; empty for the whole repository. The masks of domain_code are read inside it.
    root          text        NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid        NOT NULL REFERENCES principal,
    updated_token uuid        REFERENCES token ON DELETE SET NULL,
    CONSTRAINT service_root_check CHECK (
        char_length(root) <= 1024
        AND root !~ '^/|/$|//|(^|/)[.]{1,2}(/|$)|[[:cntrl:]]'
    ),
    FOREIGN KEY (workspace_id, project_id) REFERENCES project (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT service_source_fkey FOREIGN KEY (workspace_id, source_id, source_kind)
        REFERENCES source (workspace_id, id, kind) ON DELETE CASCADE,
    UNIQUE (project_id, name),
    -- A directory of a repository is one service, of one project; a domain of another project
    -- names that service. Deferred to the end of the transaction (the code asks for the check
    -- before it commits), so one write may move two services into each other's place.
    CONSTRAINT service_source_root_key UNIQUE (source_id, root) DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (workspace_id, id)
);

-- domain: a business area of a project, or a part of one. Two levels and no more, by the keys:
-- a subdomain (level 2) names a parent of level 1 in its project, and a domain of level 1 names
-- none, so a subdomain can have no subdomain, and a domain with subdomains cannot become one.
CREATE TABLE domain (
    id            uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    project_id    uuid        NOT NULL,
    -- 1: a domain. 2: a subdomain, addressed as domain/subdomain.
    level         smallint    NOT NULL DEFAULT 1,
    parent_id     uuid,
    -- The level of the parent, which is always 1: the third column of the key to it.
    parent_level  smallint,
    -- Unique among the domains of a project, and among the subdomains of a domain.
    name          text        NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    title         text        NOT NULL DEFAULT '' CHECK (char_length(title) <= 200),
    description   text        NOT NULL DEFAULT '' CHECK (char_length(description) <= 4000),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid        NOT NULL REFERENCES principal,
    updated_token uuid        REFERENCES token ON DELETE SET NULL,
    CONSTRAINT domain_level_check CHECK (
        (level = 1 AND parent_id IS NULL AND parent_level IS NULL)
        OR (level = 2 AND parent_id IS NOT NULL AND parent_level = 1)
    ),
    FOREIGN KEY (workspace_id, project_id) REFERENCES project (workspace_id, id) ON DELETE CASCADE,
    -- The target of the key to the parent.
    CONSTRAINT domain_project_id_level_key UNIQUE (project_id, id, level),
    CONSTRAINT domain_parent_fkey FOREIGN KEY (project_id, parent_id, parent_level)
        REFERENCES domain (project_id, id, level) ON DELETE CASCADE,
    CONSTRAINT domain_name_key UNIQUE NULLS NOT DISTINCT (project_id, parent_id, name),
    UNIQUE (workspace_id, id)
);

-- domain_code: the code of a domain or subdomain inside one service, as path masks. One service
-- may be named by several domains with different masks, a file may match the masks of several
-- domains or of none, and the service may be of another project of the workspace.
CREATE TABLE domain_code (
    id            uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    domain_id     uuid        NOT NULL,
    service_id    uuid        NOT NULL,
    -- Path globs in the dialect of the paths filter of search (memini.search.patterns), read
    -- inside the service's root; a file belongs when one of them matches. The code checks each
    -- mask; the database, that there is one and no NULL among them.
    paths         text[]      NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid        NOT NULL REFERENCES principal,
    updated_token uuid        REFERENCES token ON DELETE SET NULL,
    CONSTRAINT domain_code_paths_check CHECK (
        array_ndims(paths) = 1
        AND cardinality(paths) BETWEEN 1 AND 64
        AND array_position(paths, NULL) IS NULL
    ),
    FOREIGN KEY (workspace_id, domain_id) REFERENCES domain (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, service_id) REFERENCES service (workspace_id, id) ON DELETE CASCADE,
    -- One row per domain and service. Also: the masks of a domain.
    UNIQUE (domain_id, service_id)
);

-- The masks that name a service: what a file of it belongs to; deleting a service.
CREATE INDEX domain_code_service_idx ON domain_code (service_id);

-- Search filtered by a project, a domain or a service is a filter by source and path: each is a
-- set of (source, path masks), and a mask is an anchored regular expression over chunk.path.
-- This index finds the chunks of a source without reading the others, and narrows by the fixed
-- start of a mask (the service's root, the directory a mask names). text_pattern_ops, so that
-- the start of a pattern is usable whatever the collation of the database. It gives the planner
-- a way to read a small domain of a large index directly and sort it exactly, where the dense
-- retriever would otherwise walk the HNSW index past everything outside the domain; `memini
-- bench` times a selective path filter (docs/operations/performance.md).
CREATE INDEX chunk_source_path_idx ON chunk (source_id, path text_pattern_ops);
