-- Statement vectors of knowledge items: the dense leg of the candidates of a supersession check
-- (docs/requirements.md, section 5, Decision extraction; memini.extraction.candidates). The
-- conventions listed at the top of 0002 hold here too.
--
-- A row is derived, like a chunk's vector (0005), and is not part of the journal (0008): it holds
-- the vector of an item's statement under one embedding key (memini.index.embedding_key, the
-- model and everything else that decides a vector), and nothing else. So it has none of the
-- journal's triggers: rows of another key are deleted when a workspace's items are embedded under
-- a new one, and a row goes with its item, which goes only with its workspace.
--
-- * The extract job writes the vectors of the items it stores, computed for their check, and
--   embeds, before a check, the active items of the workspace that have none under the current
--   key (an item recorded by hand, or before an embedding provider was configured).
-- * text_hash is SHA-256 of the statement that was embedded, the text as stored (checked by the
--   writer: it inserts a row only while the item's statement has that hash). With the key it is
--   what a stored vector is reused by (memini.embeddings.reuse): a statement said again in another
--   item is not embedded again (principle 6).
-- * The column is an untyped vector, as chunk.embedding is: a model of another dimension needs no
--   migration. There is no vector index: a check compares the statement with the active items of
--   one workspace, which an index scan would not make cheaper at the sizes a journal reaches.
CREATE TABLE knowledge_item_vector (
    workspace_id  uuid        NOT NULL,
    -- The item's owner, as on every row (section 8): the writer copies it from the item.
    owner_id      uuid        NOT NULL REFERENCES principal,
    item_id       uuid        NOT NULL,
    embedding_key text        NOT NULL CHECK (embedding_key ~ '^[0-9a-f]{64}$'),
    text_hash     text        NOT NULL CHECK (text_hash ~ '^[0-9a-f]{64}$'),
    embedding     vector      NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, embedding_key),
    FOREIGN KEY (workspace_id, item_id) REFERENCES knowledge_item (workspace_id, id)
        ON DELETE CASCADE
);

-- Vector reuse and the dense leg: the vectors of a workspace by key and statement hash.
CREATE INDEX knowledge_item_vector_reuse_idx
    ON knowledge_item_vector (workspace_id, embedding_key, text_hash);
