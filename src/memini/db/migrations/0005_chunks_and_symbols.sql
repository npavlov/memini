-- Index layer: chunks and symbols, derived from records (principle 1: they can be rebuilt from raw
-- records at any time). docs/requirements.md, sections 3 (index layer), 5 and 9. The code is
-- memini.index; the chunks come from memini.chunking. The conventions listed at the top of 0002
-- hold here too.
--
-- * A chunk is a piece of a record with its text, a dense vector, a lexical index entry and its
--   place in the record. Its id is deterministic (memini.chunking.chunk_id: the record id and a
--   structural key), so writing the same record again finds the same rows (principle 6).
-- * Writes are upserts by id that skip unchanged rows: a chunk whose text hash, embedding key and
--   place are unchanged is not written at all. The text hash is SHA-256 of the text that is
--   embedded (the header line, a newline, the content), checked below; with the embedding key it
--   is what a stored vector is reused by (memini.embeddings.reuse), so an unchanged text is never
--   embedded again, also in another record or after a change of the chunker.
-- * Chunks and symbols belong to their record: deleting the record (forget, retention, deleting
--   its source or workspace) deletes them (ON DELETE CASCADE). A tombstoned record keeps its row,
--   but the index writer deletes its chunks.
--
-- Vectors. The column is an untyped vector: the dimension is a setting of the embedding model, and
-- a change of model (and so of dimension) must not need a migration. Rows of one workspace all
-- carry the embedding key of that workspace's index_state, except while a rebuild is running, when
-- rows of the old key and the new one coexist. An HNSW index needs a fixed dimension, so there is
-- one per embedding key, created by the service at run time rather than here
-- (memini.index.vectors):
--
--     CREATE INDEX chunk_embedding_<first 32 hex digits of the key> ON chunk
--         USING hnsw ((embedding::vector(<dimension>)) vector_cosine_ops)
--         WHERE embedding_key = '<key>'
--
-- (halfvec instead of vector for 2001 to 4000 dimensions, the limits of pgvector's HNSW; no
-- index above that). The writer creates it before the first chunk of a key is written, and a
-- rebuild before it starts; a finished rebuild drops the indexes of keys no workspace uses any
-- more. A search uses it by ordering on the same expression under the same predicate, with the
-- key as a literal (memini.index.vectors.VectorIndex).
--
-- Lexical search. The lexical column is a tsvector with the 'simple' configuration: no stemming
-- and no stop words, the same for every language, so mixed Russian and English text and code
-- identifiers are indexed as they are written. It is a baseline: whether BM25 through
-- pg_textsearch or tsvector with per-language configurations ranks better is open question 1 of
-- the requirements, decided by the retrieval eval, and a change of it is a later migration.
-- Exact and regex search over code uses a trigram index on the content (pg_trgm).

-- A chunk names its record together with the record's workspace, source and kind, and a symbol
-- names its chunk together with the same workspace, source and record: neither can point into
-- another workspace or source, and a chunk's kind of record is the record's.
ALTER TABLE record ADD UNIQUE (workspace_id, source_id, kind, id);

-- chunk: a piece of a record to embed and to search (section 3, index layer).
CREATE TABLE chunk (
    -- memini.chunking.chunk_id: 64 lowercase hex characters.
    id            text        PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
    workspace_id  uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id      uuid        NOT NULL REFERENCES principal,
    source_id     uuid        NOT NULL,
    record_id     text        NOT NULL,
    -- The record's kind, kept here so that a search filters by it without a join.
    record_kind   text        NOT NULL,
    -- The structural key in readable form (memini.chunking.Chunk.key), e.g. Order.total#0.
    key           text        NOT NULL CHECK (key <> ''),
    -- One definition, or a part of one: the kind of the definition. group: small siblings merged.
    -- code: statements outside definitions. window: a line window of a file that was not parsed.
    kind          text        NOT NULL CHECK (kind IN (
                                  'class', 'struct', 'interface', 'enum', 'type', 'module',
                                  'function', 'method', 'constant', 'variable',
                                  'group', 'code', 'window'
                              )),
    -- The language the chunk was parsed as; NULL for a file without a grammar.
    language      text        CHECK (
                                  language IN ('python', 'javascript', 'typescript', 'ruby', 'go')
                              ),
    -- For code: the path of the file in its repository, as in the header.
    path          text        CHECK (path <> ''),
    -- The qualified name of the definition the chunk is, is part of, or is inside; NULL at the
    -- top of a file.
    symbol        text        CHECK (symbol <> ''),
    -- Where the chunk is in the record: 1-based inclusive lines, byte offsets [start, end).
    start_line    integer,
    end_line      integer,
    start_byte    integer,
    end_byte      integer,
    -- One line naming the chunk: the path and the qualified names it holds.
    header        text        NOT NULL CHECK (header <> ''),
    content       text        NOT NULL,
    -- SHA-256 of the text that is embedded, as lowercase hex. Checked, so that a vector is reused
    -- only for the text it was computed from.
    text_hash     text        NOT NULL,
    -- memini.index.IndexFingerprint.embedding_key of the vector's model (0004).
    embedding_key text        NOT NULL CHECK (embedding_key ~ '^[0-9a-f]{64}$'),
    embedding     vector      NOT NULL,
    lexical       tsvector    NOT NULL
                              GENERATED ALWAYS AS (
                                  to_tsvector('simple'::regconfig, header || E'\n' || content)
                              ) STORED,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),  -- any column last changed
    CONSTRAINT chunk_text_hash_check
        CHECK (text_hash = encode(sha256(convert_to(header || E'\n' || content, 'UTF8')), 'hex')),
    CONSTRAINT chunk_lines_check CHECK (
        (start_line IS NULL) = (end_line IS NULL) AND start_line >= 1 AND end_line >= start_line
    ),
    CONSTRAINT chunk_bytes_check CHECK (
        (start_byte IS NULL) = (end_byte IS NULL) AND start_byte >= 0 AND end_byte >= start_byte
    ),
    -- A chunk of code always says where it is.
    CONSTRAINT chunk_code_file_check CHECK (
        record_kind <> 'code_file'
        OR (path IS NOT NULL AND start_line IS NOT NULL AND start_byte IS NOT NULL)
    ),
    FOREIGN KEY (workspace_id, source_id, record_kind, record_id)
        REFERENCES record (workspace_id, source_id, kind, id) ON DELETE CASCADE,
    -- The target of the symbol's key. Also: the chunks of a record, which the writer reads and
    -- deletes; and the chunks of a source.
    UNIQUE (workspace_id, source_id, record_id, id)
);

-- Vector reuse: the stored vectors of a workspace by embedding key and text hash.
CREATE INDEX chunk_vector_reuse_idx ON chunk (workspace_id, embedding_key, text_hash);
-- Lexical search (the baseline of open question 1).
CREATE INDEX chunk_lexical_idx ON chunk USING gin (lexical);
-- Exact and regex search over code: LIKE, ILIKE and ~ on the content.
CREATE INDEX chunk_content_trgm_idx ON chunk USING gin (content gin_trgm_ops);

-- symbol: a code definition (section 3, index layer), for get_symbol. Every definition of a file,
-- nested ones included, with the chunk that holds its first line.
CREATE TABLE symbol (
    workspace_id   uuid        NOT NULL REFERENCES workspace ON DELETE CASCADE,
    owner_id       uuid        NOT NULL REFERENCES principal,
    source_id      uuid        NOT NULL,
    record_id      text        NOT NULL,
    chunk_id       text        NOT NULL,
    name           text        NOT NULL CHECK (name <> ''),
    -- Names from the outermost enclosing definition, joined with '.' in every language.
    qualified_name text        NOT NULL CHECK (qualified_name <> ''),
    -- How many symbols with the same qualified name come before this one in the file.
    ordinal        integer     NOT NULL CHECK (ordinal >= 0),
    kind           text        NOT NULL CHECK (kind IN (
                                   'class', 'struct', 'interface', 'enum', 'type', 'module',
                                   'function', 'method', 'constant', 'variable'
                               )),
    language       text        CHECK (
                                   language IN ('python', 'javascript', 'typescript', 'ruby', 'go')
                               ),
    path           text        NOT NULL CHECK (path <> ''),
    start_line     integer     NOT NULL,
    end_line       integer     NOT NULL,
    start_byte     integer     NOT NULL,
    end_byte       integer     NOT NULL,
    -- The qualified name of the enclosing definition (for a Go method, its receiver type).
    parent         text        CHECK (parent <> ''),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),  -- any column last changed
    CONSTRAINT symbol_lines_check CHECK (start_line >= 1 AND end_line >= start_line),
    CONSTRAINT symbol_bytes_check CHECK (start_byte >= 0 AND end_byte >= start_byte),
    -- A symbol is one per qualified name and ordinal in its file.
    PRIMARY KEY (record_id, qualified_name, ordinal),
    FOREIGN KEY (workspace_id, source_id, record_id, chunk_id)
        REFERENCES chunk (workspace_id, source_id, record_id, id) ON DELETE CASCADE
);

-- get_symbol: by name or by qualified name in a workspace, optionally in one source.
CREATE INDEX symbol_workspace_name_idx ON symbol (workspace_id, name, source_id);
CREATE INDEX symbol_workspace_qualified_name_idx
    ON symbol (workspace_id, qualified_name, source_id);
