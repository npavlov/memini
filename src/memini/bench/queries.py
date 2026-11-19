"""The queries the benchmark times: made from what the index holds, the same for a given seed.

Code queries come from the symbol table: a sample of definition names (those of plain identifier
characters, 4 to 40 long), taken in an order fixed by the seed. A name is asked for as it is in
``symbol`` and ``exact`` mode, as a call or definition (``name\\s*\\(``) in ``regex`` mode, and as
words (``get_absolute_url``: "how does get absolute url work") in the ranked modes. Message queries
come from the topics and phrases of the synthetic message set (:mod:`memini.bench.corpus`). The
ranked modes take two code queries for each message query when both are there; ``exact`` and
``regex`` alternate.
"""

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.bench.corpus import PHRASES, TOPICS
from memini.search import Mode

__all__ = ["BenchQuery", "build_queries", "identifier_words"]

type Conn = AsyncConnection[TupleRow]

_TEMPLATES: Final = (
    "how does {} work",
    "where is {} handled",
    "{}",
    "what calls {}",
)
_CAMEL: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True, slots=True)
class BenchQuery:
    """One timed search."""

    text: str
    mode: Mode
    origin: Literal["code", "messages"]


def identifier_words(name: str) -> str:
    """``get_absolute_url`` or ``getAbsoluteUrl``: ``get absolute url``."""
    return " ".join(part.lower() for part in _CAMEL.sub("_", name).split("_") if part)


async def _symbol_names(conn: Conn, count: int, seed: int) -> list[str]:
    cur = await conn.execute(
        "SELECT name FROM (SELECT DISTINCT name FROM symbol"
        r" WHERE name ~ '^[A-Za-z_][A-Za-z0-9_]{3,39}$') AS names"
        " ORDER BY md5(name || %s), name LIMIT %s",
        (str(seed), count),
    )
    return [row[0] for row in await cur.fetchall()]


def _message_text(rng: random.Random) -> str:
    topic = rng.choice(TOPICS)
    match rng.randrange(3):
        case 0:
            return f"{rng.choice(PHRASES)} the {topic}"
        case 1:
            return f"why does the {topic} fail"
        case _:
            return f"decision about the {topic}"


def _message_regex(rng: random.Random) -> str:
    first, second = rng.sample(TOPICS, 2)
    a, b = first.split()[0], second.split()[0]
    c, d = first.split()[-1], second.split()[-1]
    return f"({a}|{b}) ({c}|{d})"


def _interleave(
    code: Sequence[BenchQuery], messages: Sequence[BenchQuery], count: int, *, code_share: int
) -> list[BenchQuery]:
    """``count`` queries: ``code_share`` code queries, then one message query, and again."""
    out: list[BenchQuery] = []
    ci = mi = 0
    while len(out) < count and (ci < len(code) or mi < len(messages)):
        for _ in range(code_share):
            if ci < len(code) and len(out) < count:
                out.append(code[ci])
                ci += 1
        if mi < len(messages) and len(out) < count:
            out.append(messages[mi])
            mi += 1
        if ci >= len(code) and mi >= len(messages):
            break
    return out


async def build_queries(
    conn: Conn, *, per_mode: int, seed: int, messages: bool
) -> dict[Mode, list[BenchQuery]]:
    """``per_mode`` queries for each mode (fewer when the index has fewer symbols), see the module.

    ``messages``: whether the index holds the synthetic message set.
    """
    rng = random.Random(seed)  # noqa: S311 - a fixed sample, not cryptography
    names = await _symbol_names(conn, per_mode, seed)
    message_count = per_mode if messages else 0
    ranked_code = [
        BenchQuery(rng.choice(_TEMPLATES).format(identifier_words(name)), "hybrid", "code")
        for name in names
    ]
    ranked_messages = [
        BenchQuery(_message_text(rng), "hybrid", "messages") for _ in range(message_count)
    ]
    ranked = _interleave(ranked_code, ranked_messages, per_mode, code_share=2 if names else 0)
    exact = _interleave(
        [BenchQuery(name, "exact", "code") for name in names],
        [BenchQuery(rng.choice(TOPICS), "exact", "messages") for _ in range(message_count)],
        per_mode,
        code_share=1 if names else 0,
    )
    regex = _interleave(
        [BenchQuery(rf"{name}\s*\(", "regex", "code") for name in names],
        [BenchQuery(_message_regex(rng), "regex", "messages") for _ in range(message_count)],
        per_mode,
        code_share=1 if names else 0,
    )
    queries: dict[Mode, list[BenchQuery]] = {}
    for mode in ("hybrid", "dense", "lexical"):
        queries[mode] = [BenchQuery(q.text, mode, q.origin) for q in ranked]
    queries["exact"] = exact
    queries["regex"] = regex
    queries["symbol"] = [BenchQuery(name, "symbol", "code") for name in names]
    return queries
