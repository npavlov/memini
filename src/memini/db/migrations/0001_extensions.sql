-- Extensions the schema builds on. Both are trusted, so the owner of the database can create them.
-- vector: dense embeddings of chunks (pgvector).
-- pg_trgm: trigram index for exact and regex code search.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
