-- Lexical search with stemming for English and Russian, and code identifiers cut into words.
-- docs/requirements.md, sections 5 and 9 (Search), and open question 1, settled by the retrieval
-- eval. The code is memini.search.retrieval (TsvectorRetriever, lexical_statement).
--
-- The lexical column of 0005 used the 'simple' configuration: no stemming, no stop words, and the
-- parser's tokens as they are. On the code suite (eval/code) that found 61% of the answers in the
-- first ten hits. Two changes, measured one at a time, raise that to 82%:
--
-- * The built-in 'russian' configuration. It stems Cyrillic words with the Russian Snowball
--   stemmer and ASCII words with the English one, each with its list of stop words, so one
--   configuration serves both languages of the suite, and the mixed text of code with Russian
--   comments, without a language per chunk. The Russian stemmer folds ё into е.
-- * lexical_text, below: identifiers cut into words before parsing. The parser keeps
--   formatUptime as one word and TideTable.height or tidewater/fees.py as one host or file
--   name, so a question about the uptime, the height or the fees did not find them.
--
-- The column is generated, so a change of its expression rewrites the table and every row is
-- parsed again: nothing derived from it can be stale, and the index fingerprint (section 9) need
-- not cover the lexical configuration. A query is parsed by the same function and configuration
-- (memini.search.retrieval.LEXICAL_CONFIG); this migration and that constant change together.

-- The text the lexical column parses: dots and slashes become spaces, and a lower-case letter or
-- digit followed by a capital is cut (formatUptime: format Uptime), as is a run of capitals before
-- a capitalised word (HTMLParser: HTML Parser), in Latin and Cyrillic letters. The ranges are by
-- code point, so the result does not depend on the collation. Snake case needs nothing: the
-- parser reads '_' as a space. Search parses each word of a query with it too.
--
-- The column depends on this function; changing the function does not recompute the column. A
-- later change goes with ALTER TABLE chunk ALTER COLUMN lexical SET EXPRESSION in the same
-- migration.
CREATE FUNCTION lexical_text(body text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN regexp_replace(
        regexp_replace(
            translate(body, './', '  '),
            '([a-z0-9а-яё])([A-ZА-ЯЁ])', '\1 \2', 'g'
        ),
        '([A-ZА-ЯЁ])([A-ZА-ЯЁ][a-zа-яё])', '\1 \2', 'g'
    );

-- Rewrites the table and rebuilds chunk_lexical_idx.
ALTER TABLE chunk ALTER COLUMN lexical SET EXPRESSION AS (
    to_tsvector('russian'::regconfig, lexical_text(header || E'\n' || content))
);
