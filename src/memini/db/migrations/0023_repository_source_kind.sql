-- A source of code is a repository (docs/mcp/conventions.md, Sources): the kind once called git
-- is called repository, the word every tool, route and command uses for it. Only the name
-- changes: ids, records and the index stay as they are.

-- The catalog's services point at their source by (workspace, id, kind); the key comes off while
-- both sides change and goes back on after.
ALTER TABLE service DROP CONSTRAINT service_source_fkey;
ALTER TABLE service DROP CONSTRAINT service_source_kind_check;
ALTER TABLE source DROP CONSTRAINT source_kind_check;

UPDATE source SET kind = 'repository' WHERE kind = 'git';
UPDATE service SET source_kind = 'repository' WHERE source_kind = 'git';

ALTER TABLE source ADD CONSTRAINT source_kind_check CHECK (kind IN ('repository', 'push', 'mcp'));
ALTER TABLE service ALTER COLUMN source_kind SET DEFAULT 'repository';
ALTER TABLE service
    ADD CONSTRAINT service_source_kind_check CHECK (source_kind = 'repository');
ALTER TABLE service
    ADD CONSTRAINT service_source_fkey FOREIGN KEY (workspace_id, source_id, source_kind)
        REFERENCES source (workspace_id, id, kind) ON DELETE CASCADE;
