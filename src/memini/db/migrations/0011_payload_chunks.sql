-- Chunks of threads, meetings and pushed documents. docs/requirements.md, section 5 (Chunks of
-- pushed records). The chunkers are memini.chunking.payloads; the payloads they read,
-- memini.payloads.
--
-- A thread is cut into groups of messages ('messages'); a meeting into the sections of its summary
-- ('summary'), its action items ('action_items') and windows of its transcript ('transcript'). A
-- pushed document is cut by headings, as a Markdown file of a repository is ('section', 'group').
-- None of them has a language, a symbol or a path; their lines and byte offsets count in the
-- record's normalized text.
--
-- Three columns say where a chunk of such a record is, for a locator and for evidence:
--
-- * anchor: the place in the record in the form a knowledge item's evidence names it (0008):
--   msg-<id> (the first message of a thread chunk), seg-<id> (the first segment of a transcript
--   window), summary, summary/<heading trail>, action-items or overview in a meeting, a heading
--   trail in a document. NULL for code and for the files of repositories, whose place is their
--   path and lines.
-- * url: a link to that place in the source, when the payload gives one (the url of a message).
-- * occurred_at: when what the chunk holds happened: the time of its first message, the start of
--   its meeting or of its transcript window. NULL where the record's time is the chunk's.
--
-- The index writer compares them as it compares a chunk's place: a change of them alone rewrites
-- the row and keeps its vector.

ALTER TABLE chunk DROP CONSTRAINT chunk_kind_check;
ALTER TABLE chunk ADD CONSTRAINT chunk_kind_check CHECK (kind IN (
    'class', 'struct', 'interface', 'enum', 'type', 'module',
    'function', 'method', 'constant', 'variable',
    'group', 'code', 'window', 'section',
    'messages', 'summary', 'action_items', 'transcript'
));

ALTER TABLE chunk
    ADD COLUMN anchor text,
    ADD COLUMN url text,
    ADD COLUMN occurred_at timestamptz,
    -- As knowledge_evidence takes an anchor (0008).
    ADD CONSTRAINT chunk_anchor_check
        CHECK (char_length(anchor) BETWEEN 1 AND 1000 AND anchor !~ '[[:cntrl:]]'),
    ADD CONSTRAINT chunk_url_check CHECK (url <> '');
