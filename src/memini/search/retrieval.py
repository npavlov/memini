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
  ``ts_rank``. The column (migration 0010) is the chunk's header and content with identifiers cut
  into words (:func:`split_identifiers`), parsed with the ``russian`` configuration, which stems
  Russian and English words alike (:data:`LEXICAL_CONFIG`); open question 1 chose it by the
  retrieval eval over the ``simple`` configuration of 0005 and BM25 through ``pg_textsearch``. A
  different lexical index replaces :data:`lexical_statement` and nothing else;
  :func:`memini.search.search` takes another one of the same shape (:data:`LexicalStatement`),
  which is how the retrieval eval compares them.
- **exact**: ``LIKE`` / ``ILIKE`` on the content; **regex**: ``~`` / ``~*``. Both are served by the
  trigram index on the content (``pg_trgm``), and run under the search's statement timeout,
  because a regular expression can take time without end and a short pattern gives the trigram
  index nothing to narrow by.
- **symbol**: the ``symbol`` table by name or qualified name.
"""

from collections.abc import Callable, Sequence
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
from memini.search.catalog import CatalogGroup, RecordPlaces
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
    "LexicalStatement",
    "Params",
    "RankFunction",
    "Statement",
    "TsvectorRetriever",
    "chunk_conditions",
    "chunk_rows",
    "dense_statement",
    "lexical_statement",
    "match_statement",
    "pinned_commit",
    "split_identifiers",
    "symbol_definition",
    "symbol_statement",
]

type Conn = AsyncConnection[TupleRow]

LEXICAL_CONFIG: Final = "russian"
"""The text search configuration of the ``lexical`` column (migration 0010). It stems Cyrillic words
with the Russian Snowball stemmer and ASCII words with the English one, each with its stop words.
A query is parsed with the same one, or its words would not meet the stored ones."""


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
    person_ids: tuple[UUID, ...] | None = None
    """The persons the ``persons`` filter names, resolved: each canonical person and everyone
    merged into it, whose links (``record_person``) count; ``None`` for no such filter."""
    catalog_groups: tuple[CatalogGroup, ...] = ()
    """The ``projects``, ``domains`` and ``services`` filters, resolved from the catalog
    (:func:`memini.search.catalog.catalog_groups`): one group per filter given, each a set of
    sources with a regular expression over paths and, for projects and domains, the links that
    place a record other than code. A chunk passes when it is in one member of every group; an
    empty group (a domain that names no code, in a search of code) lets nothing through."""


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
    for group in scope.catalog_groups:
        # (source, path masks) pairs, any of them. The source comes first: the index on
        # (source_id, path) of migration 0018 reads a small domain without the rest.
        members: list[sql.Composable] = [
            sql.SQL("c.source_id = {}").format(param(member.source_id))
            if member.regex is None
            else sql.SQL("(c.source_id = {} AND c.path ~ {})").format(
                param(member.source_id), param(member.regex)
            )
            for member in group.code
        ]
        if group.records is not None:
            members.append(_linked(group.records, param))
        parts.append(
            sql.SQL("({})").format(sql.SQL(" OR ").join(members)) if members else sql.SQL("FALSE")
        )
    if filters.since is not None:
        parts.append(sql.SQL("r.occurred_at >= {}").format(param(filters.since)))
    if filters.until is not None:
        parts.append(sql.SQL("r.occurred_at < {}").format(param(filters.until)))
    if scope.person_ids is not None:
        # A link of the record to one of the persons, through the unique key that starts with
        # record_id; a merged person's links keep naming it, so its members are listed.
        roles = sql.SQL("")
        if filters.roles is not None:
            roles = sql.SQL(" AND rp.role = ANY({})").format(param(list(filters.roles)))
        parts.append(
            sql.SQL(
                "EXISTS (SELECT 1 FROM record_person rp WHERE rp.record_id = c.record_id"
                " AND rp.person_id = ANY({}){})"
            ).format(param(list(scope.person_ids)), roles)
        )
    return sql.SQL(" AND ").join(parts)


# The links of the record of ``chunk c``, ``record r``: what it was pushed with, else the default
# of its stream, else of its source; at most one row (memini.decisions.context, the same order).
# Each is one lookup: record_catalog by its key, source_catalog by its key (source_id,
# stream_key), stream by its key.
_PLACE: Final = sql.SQL(
    "SELECT rc.project, rc.domains FROM record_catalog rc WHERE rc.record_id = c.record_id"
    " UNION ALL (SELECT d.project, d.domains FROM source_catalog d"
    " WHERE d.source_id = c.source_id"
    " AND (d.stream_key IS NULL"
    " OR d.stream_key = (SELECT st.key FROM stream st WHERE st.id = r.stream_id))"
    " AND NOT EXISTS (SELECT 1 FROM record_catalog rc WHERE rc.record_id = c.record_id)"
    " ORDER BY d.stream_key NULLS LAST LIMIT 1)"
)

_WORKSPACE_KEY: Final = sql.SQL("c.workspace_id::text")


def _linked(places: RecordPlaces, param: Params) -> sql.Composable:
    """Whether the record of ``chunk c`` is linked to what ``places`` names
    (:mod:`memini.search.catalog`, Records other than code).

    Its links are compared as :func:`memini.search.catalog.place_key` makes them. The domains of
    the model count only when the record's own links give it no domain the catalog holds; they
    are found through the evidence of the record (knowledge_evidence by record_id) and the
    model's links of each item (knowledge_item_domain by its key, which starts with item_id), so
    each test of a chunk is a few index lookups. The sources come first: like the code members,
    this one starts with ``c.source_id``, which the index on (source_id, path) serves."""
    if not places.sources or not places.names:
        return sql.SQL("FALSE")
    names = param(list(places.names))
    known = param(list(places.known))
    ws = _WORKSPACE_KEY
    if places.level == "domain":
        held = sql.SQL("k.held && {}::text[]").format(names)
        model = sql.SQL(
            "{ws} || ':' || l.project || ':' || l.domain = ANY({names}::text[])"
        ).format(ws=ws, names=names)
    else:
        held = sql.SQL("{ws} || ':' || p.project = ANY({names}::text[])").format(ws=ws, names=names)
        model = sql.SQL(
            "{ws} || ':' || l.project = ANY({names}::text[])"
            " AND {ws} || ':' || l.project || ':' || l.domain = ANY({known}::text[])"
        ).format(ws=ws, names=names, known=known)
    return sql.SQL(
        "(c.source_id = ANY({sources}) AND EXISTS ("
        "SELECT 1 FROM (SELECT 1) AS one LEFT JOIN LATERAL ({place}) AS p ON TRUE"
        " CROSS JOIN LATERAL (SELECT ARRAY("
        "SELECT {ws} || ':' || p.project || ':' || a.address"
        " FROM unnest(p.domains) AS a (address)) AS held) AS k"
        " WHERE {held} OR (NOT (k.held && {known}::text[]) AND EXISTS ("
        "SELECT 1 FROM knowledge_evidence e"
        " JOIN knowledge_item i ON i.id = e.item_id AND i.status <> 'retracted'"
        " JOIN knowledge_item_domain l ON l.item_id = e.item_id AND l.origin = 'model'"
        " WHERE e.record_id = c.record_id AND e.workspace_id = c.workspace_id AND {model}))))"
    ).format(
        sources=param(list(places.sources)),
        place=_PLACE,
        ws=ws,
        held=held,
        known=known,
        model=model,
    )


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


def split_identifiers(text: sql.Composable) -> sql.Composable:
    """``text`` with the identifiers of code cut into words: the SQL function ``lexical_text``
    of migration 0010.

    The text search parser keeps ``formatUptime`` as one word and ``TideTable.height`` or
    ``tidewater/fees.py`` as one host or file name, so a question about the uptime or the fees
    would not find them. ``lexical_text`` turns dots and slashes into spaces and cuts a lower-case
    letter or digit followed by a capital (``formatUptime``: ``format Uptime``), and a run of
    capitals before a capitalised word (``HTMLParser``: ``HTML Parser``), in Latin and Cyrillic
    letters. Snake case needs nothing: the parser reads ``_`` as a space. The ``lexical`` column
    is made with it, so a query word goes through the same function.
    """
    return sql.SQL("lexical_text({})").format(text)


type RankFunction = Literal["ts_rank", "ts_rank_cd"]


@dataclass(frozen=True, slots=True)
class TsvectorRetriever:
    """A lexical retriever over a ``tsvector`` column of ``chunk``; an instance is a
    :data:`LexicalStatement`.

    The column holds :meth:`document` of the chunk's header and content. A query word is parsed
    the same way, with each configuration, so its lexemes meet the stored ones. The service's is
    :data:`lexical_statement`; the retrieval eval measures others, each on a column it adds to
    its scratch database (:mod:`memini.evaluation.variants`).
    """

    column: str
    configs: tuple[str, ...]
    """Text search configurations; the column concatenates the tsvector of each."""
    split: bool
    """Whether identifiers are cut into words (:func:`split_identifiers`) before parsing."""
    rank: RankFunction = "ts_rank"
    normalization: int = 0
    """The ``normalization`` argument of the rank function (a bit mask; 0: none)."""

    def _text(self, text: sql.Composable) -> sql.Composable:
        return split_identifiers(text) if self.split else text

    def document(self, text: sql.Composable) -> sql.Composable:
        """The expression of the column over ``text``."""
        return sql.SQL(" || ").join(
            sql.SQL("to_tsvector({}::regconfig, {})").format(sql.Literal(config), self._text(text))
            for config in self.configs
        )

    def tsquery(self, text: str, param: Params) -> sql.Composable:
        """Any word of ``text``: the alternatives (``||``) of each word parsed with each
        configuration. A word that is only a stop word adds nothing."""
        return sql.SQL("({})").format(
            sql.SQL(" || ").join(
                sql.SQL("plainto_tsquery({}::regconfig, {})").format(
                    sql.Literal(config), self._text(param(term))
                )
                for term in lexical_terms(text)
                for config in self.configs
            )
        )

    def __call__(self, scope: ChunkScope, text: str, limit: int) -> Statement:
        """The ``limit`` chunks that hold most of the words of ``text``: rows of
        ``(chunk id, rank)``, best first.

        The words are alternatives (``|``): a question in natural language has words no chunk
        holds. The rank weighs how often a chunk holds them (``ts_rank``), or how many of them
        and how close together (``ts_rank_cd``).
        """
        param = Params()
        where = chunk_conditions(scope, param)
        tsquery = self.tsquery(text, param)
        column = _c(self.column)
        query = sql.SQL(
            "SELECT c.id, {rank}({column}, {tsquery}, {normalization}) AS rank FROM {from_}"
            " WHERE {column} @@ {tsquery} AND {where}"
            " ORDER BY rank DESC, c.id LIMIT {limit}"
        ).format(
            rank=sql.Identifier(self.rank),
            column=column,
            tsquery=tsquery,
            normalization=sql.Literal(self.normalization),
            from_=_FROM,
            where=where,
            limit=sql.Literal(limit),
        )
        return Statement(query, param.values)


lexical_statement: Final = TsvectorRetriever(
    column="lexical", configs=(LEXICAL_CONFIG,), split=True, rank="ts_rank"
)
"""The service's lexical retriever: the ``lexical`` column of migration 0010, any word of the query
parsed with :data:`LEXICAL_CONFIG` after :func:`split_identifiers`, ranked by ``ts_rank``."""


type LexicalStatement = Callable[[ChunkScope, str, int], Statement]
"""Makes the statement of the lexical retriever from the scope, the query text and a limit: rows of
``(chunk id, score)``, best first, at most the limit, under :func:`chunk_conditions`.
:data:`lexical_statement` is the service's."""


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
    anchor: str | None
    chunk_url: str | None
    chunk_occurred_at: datetime | None
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
            anchor=self.anchor,
            chunk_url=self.chunk_url,
            chunk_occurred_at=self.chunk_occurred_at,
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
    the retrievers. A chunk that does not pass them is left out.

    Each row has the record's time (the one the time filter compares) and the chunk's own place:
    its anchor, URL and time (migration 0011), ``NULL`` where the chunk has none."""
    if not chunk_ids:
        return {}
    param = Params()
    where = chunk_conditions(scope, param)
    chars = sql.Literal(snippet_chars)
    query = sql.SQL(
        "SELECT c.id, c.record_id, c.workspace_id, c.source_id, src.name, src.kind,"
        " c.record_kind, r.external_id, r.external_url, r.occurred_at, st.cursor, c.kind,"
        " c.language, c.path, c.symbol, c.start_line, c.end_line, c.header,"
        " left(c.content, {chars}), char_length(c.content) > {chars},"
        " c.anchor, c.url, c.occurred_at"
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
            anchor=row[20],
            chunk_url=row[21],
            chunk_occurred_at=row[22],
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
