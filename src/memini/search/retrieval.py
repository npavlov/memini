"""The SQL of each retriever, and of the rows a hit is made of.

Every statement here reads ``chunk`` joined with its ``record``, under one set of conditions
(:func:`chunk_conditions`): the caller's :class:`~memini.auth.ReadFilter`, the workspaces whose
index passed the fingerprint check, that index's embedding key, no tombstones, and the query's
filters. So every retriever, and the final read of the hits, sees exactly the same chunks.

Values from the query are always query parameters (named, ``%(p0)s``), never SQL text; only values
the service itself makes go in as literals: the embedding key (a partial index is matched only
against a literal), the uuids of the authorization filter, limits. Patterns (a glob, a LIKE
pattern, a regular expression) are made by :mod:`memini.search.patterns`.

The retrievers:

- **dense**: the nearest chunks by cosine distance, through the HNSW index of the embedding key
  (:class:`~memini.index.VectorIndex`: the same expression under the same predicate, the key a
  literal). With a filter the index scan is iterative (``hnsw.iterative_scan = relaxed_order``,
  set by the caller; pgvector 0.8 or later): it goes on past ``hnsw.ef_search`` until enough rows
  pass the filter, and the rows are sorted again by distance, as pgvector advises. For a small
  workspace the planner may rather sort its rows exactly, through the btree on the workspace and
  embedding key; both give the nearest chunks.
- **lexical**: chunks whose ``lexical`` tsvector holds any word of the query, ranked by
  ``ts_rank_cd``. This is the baseline of open question 1 (BM25 in Postgres, per language): the
  column uses the ``simple`` configuration of migration 0005. A different lexical index replaces
  :func:`lexical_statement` and nothing else.
- **exact**: ``LIKE`` / ``ILIKE`` on the content; **regex**: ``~`` / ``~*``. Both are served by the
  trigram index on the content (``pg_trgm``), and run under the search's statement timeout,
  because a regular expression can take time without end and a short pattern gives the trigram
  index nothing to narrow by.
- **symbol**: the ``symbol`` table by name or qualified name.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter
from memini.embeddings import Vector
from memini.gitsync.model import read_pin
from memini.ids import RecordKind
from memini.index import VectorIndex
from memini.search.model import (
    Hit,
    SearchFilters,
    SourceKind,
    SymbolDefinition,
    SymbolMatch,
    record_kind,
    source_kind,
)
from memini.search.patterns import globs_regex, lexical_terms, like_pattern, line_mode

__all__ = [
    "LEXICAL_CONFIG",
    "ChunkRow",
    "ChunkScope",
    "Params",
    "Statement",
    "chunk_conditions",
    "chunk_rows",
    "dense_statement",
    "lexical_statement",
    "match_statement",
    "pinned_commit",
    "symbol_definition",
    "symbol_statement",
]

type Conn = AsyncConnection[TupleRow]

LEXICAL_CONFIG: Final = "simple"
"""The text search configuration of the ``lexical`` column (migration 0005). A query is parsed
with the same one, or its words would not meet the stored ones."""


@dataclass(frozen=True, slots=True)
class Statement:
    """A composed query and its named parameters."""

    query: sql.Composed
    params: dict[str, Any]

    async def rows(self, conn: Conn) -> list[tuple[Any, ...]]:
        cur = await conn.execute(self.query, self.params)
        return [tuple(row) for row in await cur.fetchall()]


class Params:
    """Collects the parameters of one statement; each value gets a placeholder of its own."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def __call__(self, value: Any) -> sql.Placeholder:
        name = f"p{len(self.values)}"
        self.values[name] = value
        return sql.Placeholder(name)


@dataclass(frozen=True, slots=True)
class ChunkScope:
    """The chunks a search may return, with every filter resolved."""

    access: ReadFilter
    workspaces: dict[UUID, str]
    """The workspaces searched, by id, with their names: those in the caller's reach (and the
    workspaces filter) whose index exists and passed the fingerprint check."""
    index: VectorIndex
    """The index the check approved: its embedding key is the one every chunk read must carry."""
    source_ids: tuple[UUID, ...] | None
    """The sources and repositories the filters name, resolved; ``None`` for no such filter."""
    filters: SearchFilters


def _c(column: str) -> sql.Identifier:
    return sql.Identifier("c", column)


def chunk_conditions(scope: ChunkScope, param: Params) -> sql.Composable:
    """The conditions on ``chunk c`` joined with ``record r`` that every statement shares."""
    filters = scope.filters
    parts: list[sql.Composable] = [
        scope.access.where(
            workspace_id=_c("workspace_id"), owner_id=_c("owner_id"), source_id=_c("source_id")
        ),
        sql.SQL("c.workspace_id = ANY({})").format(param(sorted(scope.workspaces))),
        scope.index.predicate("c"),
        # The writer deletes a tombstone's chunks; this holds also between the two.
        sql.SQL("r.deleted_at IS NULL"),
    ]
    if scope.source_ids is not None:
        parts.append(sql.SQL("c.source_id = ANY({})").format(param(list(scope.source_ids))))
    if filters.kinds is not None:
        parts.append(sql.SQL("c.record_kind = ANY({})").format(param(list(filters.kinds))))
    if filters.languages is not None:
        parts.append(sql.SQL("c.language = ANY({})").format(param(list(filters.languages))))
    if filters.paths:
        parts.append(sql.SQL("c.path ~ {}").format(param(globs_regex(filters.paths))))
    if filters.since is not None:
        parts.append(sql.SQL("r.occurred_at >= {}").format(param(filters.since)))
    if filters.until is not None:
        parts.append(sql.SQL("r.occurred_at < {}").format(param(filters.until)))
    return sql.SQL(" AND ").join(parts)


_FROM: Final = sql.SQL("chunk c JOIN record r ON r.id = c.record_id")


def dense_statement(scope: ChunkScope, vector: Vector, limit: int) -> Statement:
    """The ``limit`` chunks nearest to ``vector``: rows of ``(chunk id, cosine distance)``,
    nearest first."""
    param = Params()
    where = chunk_conditions(scope, param)
    distance = scope.index.distance(param(vector), "c")
    query = sql.SQL(
        "WITH nearest AS MATERIALIZED ("
        "SELECT c.id, {distance} AS distance FROM {from_} WHERE {where}"
        " ORDER BY {distance} LIMIT {limit})"
        " SELECT id, distance FROM nearest ORDER BY distance, id"
    ).format(distance=distance, from_=_FROM, where=where, limit=sql.Literal(limit))
    return Statement(query, param.values)


def lexical_statement(scope: ChunkScope, text: str, limit: int) -> Statement:
    """The ``limit`` chunks that hold most of the words of ``text``: rows of
    ``(chunk id, ts_rank_cd)``, best first.

    The words are alternatives (``|``): a question in natural language has words no chunk holds,
    and the simple configuration drops no stop words. The rank weighs how many of them a chunk
    holds and how close together.
    """
    param = Params()
    where = chunk_conditions(scope, param)
    config = sql.Literal(LEXICAL_CONFIG)
    tsquery = sql.SQL("({})").format(
        sql.SQL(" || ").join(
            sql.SQL("plainto_tsquery({}::regconfig, {})").format(config, param(term))
            for term in lexical_terms(text)
        )
    )
    query = sql.SQL(
        "SELECT c.id, ts_rank_cd(c.lexical, {tsquery}) AS rank FROM {from_}"
        " WHERE c.lexical @@ {tsquery} AND {where}"
        " ORDER BY rank DESC, c.id LIMIT {limit}"
    ).format(tsquery=tsquery, from_=_FROM, where=where, limit=sql.Literal(limit))
    return Statement(query, param.values)


def match_statement(
    scope: ChunkScope,
    mode: Literal["exact", "regex"],
    text: str,
    *,
    case_sensitive: bool,
    limit: int,
) -> Statement:
    """Chunks whose content holds ``text`` (``exact``) or matches it as a regular expression
    (``regex``, in line mode: :func:`~memini.search.patterns.line_mode`): rows of
    ``(chunk id, line of the first match or None)``.

    Ordered by place, as grep would list them: source, path, line, then newest record first for
    chunks without a path. The line of the first match is found only for the rows returned.
    """
    param = Params()
    where = chunk_conditions(scope, param)
    if mode == "exact":
        condition = sql.SQL("c.content {} {}").format(
            sql.SQL("LIKE" if case_sensitive else "ILIKE"), param(like_pattern(text))
        )
        needle = param(text)
        haystack = sql.SQL("content" if case_sensitive else "lower(content)")
        position = sql.SQL("strpos({}, {})").format(
            haystack, needle if case_sensitive else sql.SQL("lower({})").format(needle)
        )
    else:
        pattern = param(line_mode(text))
        condition = sql.SQL("c.content {} {}").format(
            sql.SQL("~" if case_sensitive else "~*"), pattern
        )
        haystack = sql.SQL("content")
        position = sql.SQL("regexp_instr(content, {}, 1, 1, 0, {})").format(
            pattern, sql.Literal("" if case_sensitive else "i")
        )
    before = sql.SQL("left({}, position - 1)").format(haystack)
    query = sql.SQL(
        "WITH matches AS MATERIALIZED ("
        "SELECT c.id, c.content, c.start_line, row_number() OVER ("
        "ORDER BY src.name, c.path NULLS LAST, c.start_line NULLS LAST, r.occurred_at DESC, c.id"
        ") AS n FROM {from_} JOIN source src ON src.id = c.source_id"
        " WHERE {condition} AND {where} ORDER BY n LIMIT {limit}),"
        " located AS (SELECT id, n, start_line, content, {position} AS position FROM matches)"
        " SELECT id, CASE WHEN start_line IS NULL OR position < 1 THEN NULL"
        " ELSE start_line + length({before}) - length(replace({before}, chr(10), '')) END"
        " FROM located ORDER BY n"
    ).format(
        from_=_FROM,
        condition=condition,
        where=where,
        limit=sql.Literal(limit),
        position=position,
        before=before,
    )
    return Statement(query, param.values)


def symbol_statement(
    scope: ChunkScope,
    text: str,
    *,
    match: SymbolMatch,
    case_sensitive: bool,
    limit: int,
) -> Statement:
    """Definitions named ``text``: rows of ``(chunk id, name, qualified name, kind, parent,
    language, path, start line, end line)``.

    A text with a ``.`` is compared with the qualified name (``Lamp.on``), any other with the
    name (``on``, any parent). ``prefix`` finds names that start with the text, whole matches
    first. Then the shorter qualified name first, then by name and place.
    """
    param = Params()
    where = chunk_conditions(scope, param)
    column = sql.Identifier("s", "qualified_name" if "." in text else "name")
    value = param(text)
    left, right = (
        (column, value)
        if case_sensitive
        else (sql.SQL("lower({})").format(column), sql.SQL("lower({})").format(value))
    )
    equal = sql.SQL("{} = {}").format(left, right)
    condition = equal if match == "exact" else sql.SQL("starts_with({}, {})").format(left, right)
    whole_first = sql.SQL("({}) DESC, ").format(equal) if match == "prefix" else sql.SQL("")
    access = scope.access.where(
        workspace_id=sql.Identifier("s", "workspace_id"),
        owner_id=sql.Identifier("s", "owner_id"),
        source_id=sql.Identifier("s", "source_id"),
    )
    query = sql.SQL(
        "SELECT s.chunk_id, s.name, s.qualified_name, s.kind, s.parent, s.language, s.path,"
        " s.start_line, s.end_line"
        " FROM symbol s JOIN chunk c ON c.id = s.chunk_id JOIN record r ON r.id = c.record_id"
        " JOIN source src ON src.id = c.source_id"
        " WHERE {condition} AND {access} AND {where}"
        " ORDER BY {whole_first}char_length(s.qualified_name), s.qualified_name, src.name, s.path,"
        " s.start_line, s.ordinal, s.record_id"
        " LIMIT {limit}"
    ).format(
        condition=condition,
        access=access,
        where=where,
        whole_first=whole_first,
        limit=sql.Literal(limit),
    )
    return Statement(query, param.values)


def symbol_definition(row: Sequence[Any]) -> SymbolDefinition:
    """A :class:`SymbolDefinition` from a row of :func:`symbol_statement`."""
    return SymbolDefinition(
        name=row[1],
        qualified_name=row[2],
        kind=row[3],
        parent=row[4],
        language=row[5],
        path=row[6],
        start_line=row[7],
        end_line=row[8],
    )


def pinned_commit(kind: SourceKind, cursor: object) -> str | None:
    """The commit a code record is pinned at, from the cursor of the stream that stored it.

    A code record is a file at the pinned commit of a git ref, and the stream of that ref holds
    the commit in its cursor (``stream.cursor``, read by :func:`memini.gitsync.model.read_pin`; it
    moves in the transaction that stores the last files, principle 7). ``None`` when the source is
    not git, the stream has no complete sync yet, or its last sync failed part way (``partial``):
    then some records are already past the pin and the commit of a given file is unknown. This is
    the only place search reads a pin.
    """
    if kind != "git":
        return None
    pin = read_pin(cursor)
    if pin is None or pin.partial:
        return None
    return pin.commit


@dataclass(frozen=True, slots=True)
class ChunkRow:
    """The stored parts of a hit: one chunk with its record and source."""

    chunk_id: str
    record_id: str
    workspace_id: UUID
    source_id: UUID
    source: str
    source_kind: SourceKind
    record_kind: RecordKind
    external_id: str
    external_url: str | None
    occurred_at: datetime
    commit: str | None
    chunk_kind: str
    language: str | None
    path: str | None
    symbol: str | None
    start_line: int | None
    end_line: int | None
    header: str
    snippet: str
    truncated: bool

    def hit(
        self,
        workspace: str,
        *,
        score: float | None = None,
        dense_rank: int | None = None,
        lexical_rank: int | None = None,
        match_line: int | None = None,
        definition: SymbolDefinition | None = None,
    ) -> Hit:
        """The hit of this chunk, with what the retriever found out about it."""
        return Hit(
            chunk_id=self.chunk_id,
            record_id=self.record_id,
            workspace_id=self.workspace_id,
            workspace=workspace,
            source_id=self.source_id,
            source=self.source,
            source_kind=self.source_kind,
            record_kind=self.record_kind,
            external_id=self.external_id,
            external_url=self.external_url,
            occurred_at=self.occurred_at,
            commit=self.commit,
            chunk_kind=self.chunk_kind,
            language=self.language,
            path=self.path,
            symbol=self.symbol,
            start_line=self.start_line,
            end_line=self.end_line,
            header=self.header,
            snippet=self.snippet,
            truncated=self.truncated,
            score=score,
            dense_rank=dense_rank,
            lexical_rank=lexical_rank,
            match_line=match_line,
            definition=definition,
        )


async def chunk_rows(
    conn: Conn, scope: ChunkScope, chunk_ids: Sequence[str], *, snippet_chars: int
) -> dict[str, ChunkRow]:
    """The chunks ``chunk_ids`` with their record and source, by id, under the same conditions as
    the retrievers. A chunk that does not pass them is left out."""
    if not chunk_ids:
        return {}
    param = Params()
    where = chunk_conditions(scope, param)
    chars = sql.Literal(snippet_chars)
    query = sql.SQL(
        "SELECT c.id, c.record_id, c.workspace_id, c.source_id, src.name, src.kind,"
        " c.record_kind, r.external_id, r.external_url, r.occurred_at, st.cursor, c.kind,"
        " c.language, c.path, c.symbol, c.start_line, c.end_line, c.header,"
        " left(c.content, {chars}), char_length(c.content) > {chars}"
        " FROM {from_} JOIN source src ON src.id = c.source_id"
        " LEFT JOIN stream st ON st.source_id = r.source_id AND st.id = r.stream_id"
        " WHERE c.id = ANY({ids}) AND {where}"
    ).format(chars=chars, from_=_FROM, ids=param(list(chunk_ids)), where=where)
    found: dict[str, ChunkRow] = {}
    for row in await Statement(query, param.values).rows(conn):
        kind = source_kind(row[5])
        found[row[0]] = ChunkRow(
            chunk_id=row[0],
            record_id=row[1],
            workspace_id=row[2],
            source_id=row[3],
            source=row[4],
            source_kind=kind,
            record_kind=record_kind(row[6]),
            external_id=row[7],
            external_url=row[8],
            occurred_at=row[9],
            commit=pinned_commit(kind, row[10]) if kind == "git" else None,
            chunk_kind=row[11],
            language=row[12],
            path=row[13],
            symbol=row[14],
            start_line=row[15],
            end_line=row[16],
            header=row[17],
            snippet=row[18],
            truncated=row[19],
        )
    return found
