-- Sensitive records: docs/requirements.md, section 8 (Sensitive records). The code is
-- memini.sensitivity and the read filter, memini.auth.authorization. The conventions listed at the
-- top of 0002 hold here too.
--
-- * A record can be marked sensitive (a salary, a diagnosis, a date of birth): it stays stored and
--   indexed, and every read leaves it out unless the request asks for it with a token that has the
--   new scope `sensitive`. Nothing is deleted or re-embedded, so marking needs no reindex.
-- * sensitive_reason is a short note of why, shown to an admin who lists marked records; it is
--   redacted like any text an agent writes. sensitive_at is when the mark was last set.
-- * The scope list of tokens grows by `sensitive` (memini.auth.principal.SCOPES).

ALTER TABLE record
    ADD COLUMN sensitive        boolean     NOT NULL DEFAULT false,
    ADD COLUMN sensitive_reason text,
    ADD COLUMN sensitive_at     timestamptz;

ALTER TABLE record ADD CONSTRAINT record_sensitive_check CHECK (
    (sensitive AND sensitive_at IS NOT NULL)
    OR (NOT sensitive AND sensitive_reason IS NULL AND sensitive_at IS NULL)
);
ALTER TABLE record ADD CONSTRAINT record_sensitive_reason_check
    CHECK (sensitive_reason IS NULL OR char_length(sensitive_reason) BETWEEN 1 AND 200);

-- The admin listing of marked records reads only them.
CREATE INDEX record_sensitive_idx ON record (workspace_id, sensitive_at DESC) WHERE sensitive;

ALTER TABLE token DROP CONSTRAINT token_scopes_check;
ALTER TABLE token ADD CONSTRAINT token_scopes_check CHECK (
    cardinality(scopes) > 0
    AND array_ndims(scopes) = 1
    AND scopes <@ ARRAY['read', 'ingest', 'admin', 'sensitive']
);
