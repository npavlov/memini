-- Where the checked history of a stream starts: the frontier the intake planner loads older
-- history from, a step per run. docs/requirements.md, section 4 (Ingest; Scheduling and hygiene).
-- The code is memini.ingest (it moves the frontier) and memini.intake (it plans the windows).
--
-- Everything in the stream from checked_from up to checked_until (0002) is stored. The first
-- complete window of a stream sets both, to its from and its to. Later complete windows extend
-- the range: one that reaches the checkpoint moves checked_until forward to its to, one that
-- reaches the frontier (ends at or after it) moves checked_from back to its from. Each moves in
-- the transaction that stores the window's records (principle 7), and neither ever moves the other
-- way. A complete window that leaves a gap below the frontier stores its records and leaves it.
--
-- NULL while checked_until is NULL, and on streams whose checkpoint moved before this column
-- existed or moves by other means (a git ref): the start of their range is not recorded, and is
-- taken to be checked_until until a complete window reaches it.

ALTER TABLE stream
    ADD COLUMN checked_from timestamptz,
    ADD CONSTRAINT stream_checked_from_check
        CHECK (checked_from IS NULL
               OR (checked_until IS NOT NULL AND checked_from <= checked_until));
