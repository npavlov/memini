-- Document chunks: the chunk kind 'section'. docs/requirements.md, section 5 (Document chunks).
--
-- Markdown files of git sources are document records, chunked by headings
-- (memini.chunking.chunk_markdown). A chunk of a document is one section, a window of one, or the
-- text before the first heading ('section'); small neighbouring sections merged are a 'group', the
-- kind code siblings merged already have. A section is not a code definition, so none of the
-- kinds of 0005 fits it; the list of kinds is a check, hence this migration. A document chunk has
-- no language (NULL is allowed) and no symbol; it keeps its path and its place in the file.

ALTER TABLE chunk DROP CONSTRAINT chunk_kind_check;
ALTER TABLE chunk ADD CONSTRAINT chunk_kind_check CHECK (kind IN (
    'class', 'struct', 'interface', 'enum', 'type', 'module',
    'function', 'method', 'constant', 'variable',
    'group', 'code', 'window', 'section'
));
