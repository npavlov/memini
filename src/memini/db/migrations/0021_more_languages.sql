-- Fifteen more languages for chunks and symbols: docs/requirements.md, section 9 (Chunking). The
-- code is memini.chunking (chunker 3). The conventions listed at the top of 0002 hold here too.
--
-- * The language of a chunk or symbol was one of five names (0005). The list of languages is the
--   chunker's, memini.chunking.Language, and grows with it; the column keeps only its shape, a
--   lowercase name, so a new language needs no migration.
-- * No row changes: chunks of a file in a new language were line windows without a language.
--   Chunker 3 changes the index fingerprint, so search refuses a workspace until `memini reindex`
--   writes its chunks again; those files then get definition chunks and symbols.

ALTER TABLE chunk DROP CONSTRAINT chunk_language_check;
ALTER TABLE chunk ADD CONSTRAINT chunk_language_check CHECK (language ~ '^[a-z][a-z0-9]*$');

ALTER TABLE symbol DROP CONSTRAINT symbol_language_check;
ALTER TABLE symbol ADD CONSTRAINT symbol_language_check CHECK (language ~ '^[a-z][a-z0-9]*$');
