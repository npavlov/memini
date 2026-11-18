-- Extensions the schema builds on.
-- vector: dense embeddings of chunks (pgvector). Not a trusted extension: creating it needs a
--   superuser. Where the service role is not one, an administrator creates it beforehand and the
--   IF NOT EXISTS below makes this step a no-op.
-- pg_trgm: trigram index for exact and regex code search. Trusted: the database owner can create it.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
