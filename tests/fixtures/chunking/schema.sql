-- schema.sql is a synthetic fixture for the chunker tests.
CREATE TABLE greenhouse (
    id integer PRIMARY KEY,
    name text NOT NULL,
    opened date
);

CREATE TABLE reading (
    greenhouse_id integer REFERENCES greenhouse (id),
    taken_at timestamptz NOT NULL,
    celsius numeric(4, 1)
);

CREATE INDEX reading_taken_at ON reading (taken_at);

-- The warmest reading of each greenhouse.
CREATE VIEW warmest AS
SELECT greenhouse_id, max(celsius) AS celsius
FROM reading
GROUP BY greenhouse_id;

CREATE FUNCTION fahrenheit(celsius numeric) RETURNS numeric
LANGUAGE sql
AS $$ SELECT celsius * 9 / 5 + 32 $$;
