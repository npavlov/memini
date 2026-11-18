"""The fingerprint of a slow statement: its text without literals or comments, and a short hash.

The cursor that times statements on a real database is in tests/test_statements_db.py. Every value
here is synthetic.
"""

import pytest

from memini.db import statement_fingerprint, timed_cursor
from memini.db.statements import MAX_STATEMENT_TEXT


@pytest.mark.parametrize(
    ("statement", "masked"),
    [
        ("SELECT 1", "SELECT ?"),
        ("SELECT 'synthetic-value'", "SELECT ?"),
        ("SELECT 'it''s synthetic'", "SELECT ?"),
        ("SELECT E'line\\nbreak', U&'d\\0061ta', X'1F'", "SELECT ?, ?, ?"),
        ("SELECT $$synthetic body$$, $tag$synthetic $$ body$tag$", "SELECT ?, ?"),
        ("SELECT 1.5, .5, 2e10, 3.0E-2", "SELECT ?, ?, ?, ?"),
        (
            "SELECT a FROM t WHERE id = %s AND k = %(key)s",
            "SELECT a FROM t WHERE id = %s AND k = %(key)s",
        ),
        ("SELECT $1, $2::text", "SELECT $1, $2::text"),
        ('SELECT "col 1", "we""ird" FROM t2', 'SELECT "col 1", "we""ird" FROM t2'),
        (
            "SELECT int8send(3), x::float8 FROM chunk_v2",
            "SELECT int8send(?), x::float8 FROM chunk_v2",
        ),
        (
            "SELECT a -- synthetic comment\nFROM t /* another\n one */"
            " WHERE b = ANY('{x,y}'::uuid[])",
            "SELECT a FROM t WHERE b = ANY(?::uuid[])",
        ),
        ("SELECT 'cut off", "SELECT ?"),
        ("  SELECT\n\t1\n  ", "SELECT ?"),
    ],
)
def test_literals_and_comments_are_masked_and_the_rest_kept(statement: str, masked: str) -> None:
    text, fingerprint = statement_fingerprint(statement)

    assert text == masked
    assert len(fingerprint) == 16
    assert all(c in "0123456789abcdef" for c in fingerprint)


def test_the_same_statement_with_other_values_has_one_fingerprint() -> None:
    first = statement_fingerprint("SELECT * FROM t WHERE a = 'synthetic-one' LIMIT 10")
    second = statement_fingerprint("SELECT *  FROM t\nWHERE a = 'synthetic-two' LIMIT 50")
    other = statement_fingerprint("SELECT * FROM u WHERE a = 'synthetic-one' LIMIT 10")

    assert first == second
    assert first[1] != other[1]


def test_a_long_statement_is_cut_and_its_fingerprint_covers_all_of_it() -> None:
    head = "SELECT " + ", ".join(f"c{n}" for n in range(200))
    text, fingerprint = statement_fingerprint(head + " FROM t1")
    _, other = statement_fingerprint(head + " FROM t2")

    assert len(text) == MAX_STATEMENT_TEXT
    assert text.endswith("…")
    assert fingerprint != other


def test_the_threshold_is_zero_or_more_seconds() -> None:
    assert timed_cursor(0.5).slow_after == 0.5
    assert timed_cursor(0).slow_after == 0
    with pytest.raises(ValueError, match="0 or more"):
        timed_cursor(-1)
