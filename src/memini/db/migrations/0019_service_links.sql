-- Links between services: a service of a project declares the services it uses, in its own
-- project or in another project of the same workspace. docs/requirements.md, sections 3
-- (Catalog) and 6 (Catalog tools). The code is memini.catalog. The conventions listed at the top
-- of 0002 and 0018 hold here too.
--
-- * Declared, not inferred. An agent or a person writes a link as part of the put_project
--   document of the project whose service uses the other; nothing reads one from the code. It is
--   authored data, as the rest of the catalog: no reindex, not part of the index fingerprint.
-- * Isolation by foreign key: both ends name their service together with the workspace, so a
--   link never crosses a workspace.
-- * A link goes with either of its services (ON DELETE CASCADE): with the using service when its
--   project drops it, and with both when their repository is deleted. put_project refuses to
--   drop a service that a link of another project names (service_in_use), as it refuses to drop
--   one that a domain of another project names.
-- * The note is free text an agent wrote: data, never instructions (principle 11). The code
--   redacts secrets in it before it is stored. The masks are identifiers in the dialect of the
--   paths filter of search, read inside the root of the using service: the places that show the
--   use.

CREATE TABLE service_link (
    id            uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    -- The service that uses; its project holds the link in its document.
    service_id    uuid        NOT NULL,
    -- The service it uses.
    target_id     uuid        NOT NULL,
    -- How: a library it imports, an HTTP API it calls, a queue between them, or another way.
    kind          text        NOT NULL CHECK (kind IN ('library', 'http', 'queue', 'other')),
    note          text        NOT NULL DEFAULT '' CHECK (char_length(note) <= 1000),
    -- Path globs inside the using service's root that show the use; none is allowed.
    paths         text[]      NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid        NOT NULL REFERENCES principal,
    updated_token uuid        REFERENCES token ON DELETE SET NULL,
    CONSTRAINT service_link_paths_check CHECK (
        cardinality(paths) <= 64
        AND (cardinality(paths) = 0 OR array_ndims(paths) = 1)
        AND array_position(paths, NULL) IS NULL
    ),
    CONSTRAINT service_link_self_check CHECK (service_id <> target_id),
    CONSTRAINT service_link_service_fkey FOREIGN KEY (workspace_id, service_id)
        REFERENCES service (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT service_link_target_fkey FOREIGN KEY (workspace_id, target_id)
        REFERENCES service (workspace_id, id) ON DELETE CASCADE,
    -- One link per pair of services, in each direction. Also: the links of a service.
    CONSTRAINT service_link_key UNIQUE (service_id, target_id)
);

-- What uses a service: get_project's used_by; dropping a service.
CREATE INDEX service_link_target_idx ON service_link (target_id);
