-- Index state: the fingerprint each workspace's index was built with (principle 9: fail closed on
-- index mismatch). docs/requirements.md, sections 2, 5 and 9. The code is memini.index.
-- The conventions listed at the top of 0002 hold here too.
--
-- The fingerprint is a digest over what decides the vectors and chunks of an index: the embedding
-- provider, model, model digest, dimension, normalization, pooling, token limit and document
-- prefix (together, the embedding key), and the chunker version. The service computes the
-- fingerprint its settings call for; a search in a workspace whose stored fingerprint differs is
-- refused with a message that names `memini reindex`, and so is writing to that index.
--
-- One row per workspace, not one for the database: the settings are global, but an index is
-- rebuilt one workspace at a time, so after a change of model each workspace is blocked until its
-- own rebuild is done, and no longer. No row: nothing has been indexed in the workspace yet. The
-- writer creates the row with its first chunks, in the same transaction; a rebuild replaces it in
-- the transaction that finishes it.
--
-- The parts are kept beside the fingerprint so that a refusal can say what changed (model,
-- dimension, chunker ...). None of them is a secret: no endpoint URL, no API key.

CREATE TABLE index_state (
    id              uuid        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id    uuid        NOT NULL UNIQUE REFERENCES workspace ON DELETE CASCADE,
    owner_id        uuid        NOT NULL REFERENCES principal,
    -- memini.index.IndexFingerprint.digest: SHA-256 over the embedding key and the chunker
    -- version, 64 lowercase hex characters.
    fingerprint     text        NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    -- memini.index.IndexFingerprint.embedding_key: SHA-256 over the embedding parts below. Stored
    -- vectors are reused by (workspace, embedding key, text hash), across chunker versions.
    embedding_key   text        NOT NULL CHECK (embedding_key ~ '^[0-9a-f]{64}$'),
    -- openai_compatible: an OpenAI-compatible HTTP API. local: an ONNX model in the process.
    provider        text        NOT NULL CHECK (provider IN ('openai_compatible', 'local')),
    model           text        NOT NULL CHECK (model <> ''),
    -- What pins the model's content: for local, the digest of the model files; for
    -- openai_compatible, what the user pinned for the served model. '' when nothing is pinned.
    model_digest    text        NOT NULL DEFAULT '',
    -- Length of every vector. 16000 is the most a pgvector vector column holds.
    dimension       integer     NOT NULL CHECK (dimension BETWEEN 1 AND 16000),
    -- How vectors are scaled. l2: to length 1, so cosine similarity is the inner product.
    normalization   text        NOT NULL CHECK (normalization IN ('l2')),
    -- local only: how token vectors become one vector, and the most tokens of a text embedded.
    pooling         text        CHECK (pooling IN ('mean', 'cls')),
    max_tokens      integer     CHECK (max_tokens > 0),
    -- Prepended to every document before it is embedded, e.g. 'passage: '.
    document_prefix text        NOT NULL DEFAULT '',
    -- The version of the chunker that cut the chunks, e.g. memini.chunking.CHUNKER_VERSION.
    chunker_version text        NOT NULL CHECK (chunker_version <> ''),
    created_at      timestamptz NOT NULL DEFAULT now(),  -- the first index of the workspace
    updated_at      timestamptz NOT NULL DEFAULT now(),  -- the fingerprint last changed
    -- Pooling and the token limit belong to the local provider: a server decides them for itself.
    CONSTRAINT index_state_local_check CHECK (
        (provider = 'local') = (pooling IS NOT NULL AND max_tokens IS NOT NULL)
        AND (pooling IS NULL) = (max_tokens IS NULL)
    ),
    -- A local model is always pinned by the digest of its files.
    CONSTRAINT index_state_model_digest_check
        CHECK (provider <> 'local' OR model_digest ~ '^sha256:[0-9a-f]{64}$')
);
