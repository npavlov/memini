"""What a search takes and gives back: the query and its filters, the options, a hit.

Everything here is plain data. :func:`memini.search.search` runs a :class:`SearchQuery` and returns
:class:`Hit` objects; the MCP tools and the HTTP API turn them into their own shapes.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, Protocol, get_args
from uuid import UUID

from memini.chunking import LANGUAGES, Language
from memini.config import Settings
from memini.ids import RecordKind
from memini.search.errors import InvalidSearchError
from memini.search.patterns import glob_regex

__all__ = [
    "DEFAULT_CANDIDATES",
    "DEFAULT_DENSE_WEIGHT",
    "DEFAULT_LEXICAL_WEIGHT",
    "DEFAULT_LIMIT",
    "DEFAULT_RERANK_DEPTH",
    "DEFAULT_RRF_K",
    "DEFAULT_SNIPPET_CHARS",
    "DEFAULT_TIMEOUT",
    "MAX_FILTER_VALUES",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "MODES",
    "RANKED_MODES",
    "Hit",
    "Mode",
    "Reranker",
    "SearchFilters",
    "SearchOptions",
    "SearchQuery",
    "SourceKind",
    "SymbolDefinition",
    "SymbolMatch",
    "record_kind",
    "source_kind",
]

Mode = Literal["hybrid", "dense", "lexical", "exact", "regex", "symbol"]
"""How a query is matched.

- ``hybrid``: ``dense`` and ``lexical`` together, merged by reciprocal rank fusion. The default,
  and what a natural-language question should use.
- ``dense``: nearest chunks by the cosine distance of their vectors to the query's vector.
- ``lexical``: chunks that hold any word of the query, ranked by the lexical index.
- ``exact``: chunks whose content holds the query as a substring.
- ``regex``: chunks whose content matches the query as a Postgres regular expression.
- ``symbol``: code definitions by name or qualified name, from the symbol table.
"""

MODES: Final[tuple[Mode, ...]] = get_args(Mode)

RANKED_MODES: Final[frozenset[Mode]] = frozenset({"hybrid", "dense", "lexical"})
"""Modes that order hits by relevance and so can be reranked. The others order by place."""

SymbolMatch = Literal["exact", "prefix"]
"""How the ``symbol`` mode compares a name: whole, or as the start of the name."""

SourceKind = Literal["git", "push", "mcp"]
"""Kinds of source, as the ``source`` table holds them."""

_SOURCE_KINDS: Final[tuple[SourceKind, ...]] = get_args(SourceKind)

DEFAULT_LIMIT: Final = 10
MAX_LIMIT: Final = 100
"""The most hits one search returns."""
MAX_QUERY_CHARS: Final = 4096
"""The longest query text."""
MAX_FILTER_VALUES: Final = 64
"""The most values one filter takes (names, kinds, path globs ...)."""

DEFAULT_TIMEOUT: Final = 5.0
"""Seconds one statement of a search may run (``search_timeout``)."""
DEFAULT_CANDIDATES: Final = 50
"""Candidates each retriever of a hybrid search contributes (``search_candidates``)."""
DEFAULT_RRF_K: Final = 1
"""The constant k of reciprocal rank fusion (``search_rrf_k``). Cormack, Clarke and Buettcher
(2009) found 60 a robust choice for retrievers of like quality; with a dense retriever far ahead
of the lexical one, the retrieval eval found a small k with the dense ranks weighted higher
better (docs/requirements.md, section 9, Lexical search)."""
DEFAULT_DENSE_WEIGHT: Final = 3.0
"""The weight of the dense retriever's ranks in the fusion (``search_dense_weight``). With k = 1,
the lexical retriever's first hit scores 1/2, as the dense retriever's fifth does (3/6)."""
DEFAULT_LEXICAL_WEIGHT: Final = 1.0
"""The weight of the lexical retriever's ranks in the fusion (``search_lexical_weight``)."""
DEFAULT_SNIPPET_CHARS: Final = 2000
"""The most characters of a chunk's content a hit carries."""
DEFAULT_RERANK_DEPTH: Final = 50
"""How many fused hits a reranker gets to reorder."""


def _names(filter_name: str, values: Sequence[str | UUID] | None) -> tuple[str | UUID, ...] | None:
    if values is None or len(values) == 0:
        return None
    if isinstance(values, str):
        raise InvalidSearchError(f"the {filter_name} filter takes a list of names, not one string")
    _check_count(filter_name, values)
    for value in values:
        if value == "":
            raise InvalidSearchError(f"the {filter_name} filter takes names or ids, not ''")
    return tuple(dict.fromkeys(values))


def _check_count(filter_name: str, values: Sequence[object]) -> None:
    if len(values) > MAX_FILTER_VALUES:
        raise InvalidSearchError(
            f"the {filter_name} filter takes at most {MAX_FILTER_VALUES} values"
        )


def _one_of[T: str](
    filter_name: str, values: Sequence[str] | None, allowed: tuple[T, ...]
) -> tuple[T, ...] | None:
    if values is None or len(values) == 0:
        return None
    if isinstance(values, str):
        raise InvalidSearchError(f"the {filter_name} filter takes a list, not one string")
    _check_count(filter_name, values)
    found: list[T] = []
    for value in values:
        match = next((option for option in allowed if option == value), None)
        if match is None:
            raise InvalidSearchError(
                f"unknown value in the {filter_name} filter; expected some of {', '.join(allowed)}"
            )
        found.append(match)
    return tuple(dict.fromkeys(found))


_RECORD_KINDS: Final[tuple[RecordKind, ...]] = get_args(RecordKind)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchFilters:
    """What a search is limited to. Every filter is optional; given filters all apply (AND), and
    the values of one filter are alternatives (OR). An empty tuple is the same as ``None``.

    Names and ids: a ``str`` is a name, a :class:`~uuid.UUID` an id. A name that matches nothing
    the caller may read gives no hits, not an error, as the authorization filter does.
    """

    workspaces: tuple[str | UUID, ...] | None = None
    """Workspaces to search, within those the caller's token may read."""
    sources: tuple[str | UUID, ...] | None = None
    """Sources of any kind. Names are unique within a workspace, so one name may match a source
    in each workspace searched."""
    repositories: tuple[str | UUID, ...] | None = None
    """Git sources only: a repository is a source of kind ``git``."""
    kinds: tuple[RecordKind, ...] | None = None
    """Kinds of record: ``code_file``, ``thread``, ``meeting``, ``document``."""
    languages: tuple[Language, ...] | None = None
    """Languages a chunk was parsed as. A chunk of a file without a grammar has none."""
    paths: tuple[str, ...] = ()
    """Path globs (:func:`memini.search.patterns.glob_regex`); a chunk without a path (anything
    but code) never matches one."""
    since: datetime | None = None
    """Records whose ``occurred_at`` is at or after this time."""
    until: datetime | None = None
    """Records whose ``occurred_at`` is before this time."""
    persons: tuple[str | UUID, ...] | None = None
    """Records of a person. Accepted here, refused by :func:`memini.search.search` with
    :class:`UnsupportedFilterError` until persons are linked to records (milestone M2)."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspaces", _names("workspaces", self.workspaces))
        object.__setattr__(self, "sources", _names("sources", self.sources))
        object.__setattr__(self, "repositories", _names("repositories", self.repositories))
        object.__setattr__(self, "persons", _names("persons", self.persons))
        object.__setattr__(self, "kinds", _one_of("kinds", self.kinds, _RECORD_KINDS))
        object.__setattr__(self, "languages", _one_of("languages", self.languages, LANGUAGES))
        if isinstance(self.paths, str):
            raise InvalidSearchError("the paths filter takes a list of globs, not one string")
        _check_count("paths", self.paths)
        for glob in self.paths:
            glob_regex(glob)  # raises InvalidSearchError for a glob it cannot take
        object.__setattr__(self, "paths", tuple(dict.fromkeys(self.paths)))
        for name, moment in (("since", self.since), ("until", self.until)):
            if moment is not None and moment.utcoffset() is None:
                raise InvalidSearchError(f"{name} needs a time zone")
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise InvalidSearchError("since must be before until")


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchQuery:
    """One search: the text, how to match it, how many hits, and the filters."""

    text: str
    """A question or keywords (ranked modes), a substring (``exact``), a pattern (``regex``) or a
    name (``symbol``)."""
    mode: Mode = "hybrid"
    limit: int = DEFAULT_LIMIT
    """The most hits returned, 1 to :data:`MAX_LIMIT`."""
    filters: SearchFilters = field(default_factory=SearchFilters)
    case_sensitive: bool = True
    """For ``exact``, ``regex`` and ``symbol``. The ranked modes fold case anyway."""
    symbol_match: SymbolMatch = "exact"
    """For ``symbol``: the whole name, or names that start with the text."""

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise InvalidSearchError(f"unknown mode; expected one of {', '.join(MODES)}")
        if not self.text.strip():
            raise InvalidSearchError("the query text is empty")
        if len(self.text) > MAX_QUERY_CHARS:
            raise InvalidSearchError(f"the query text is longer than {MAX_QUERY_CHARS} characters")
        if "\x00" in self.text:
            raise InvalidSearchError("the query text holds a NUL character")
        if isinstance(self.limit, bool) or not 1 <= self.limit <= MAX_LIMIT:
            raise InvalidSearchError(f"limit is from 1 to {MAX_LIMIT}")
        if self.symbol_match not in get_args(SymbolMatch):
            raise InvalidSearchError("symbol_match is exact or prefix")


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchOptions:
    """Limits and constants of the service, not of one query. :meth:`from_settings` reads them
    from the ``search_*`` settings."""

    timeout: float = DEFAULT_TIMEOUT
    """Seconds one statement may run (``statement_timeout``); a statement that runs longer is
    stopped and the search raises :class:`SearchTimeoutError`."""
    candidates: int = DEFAULT_CANDIDATES
    """How many chunks each retriever of a hybrid search contributes to the fusion. Also the
    ``hnsw.ef_search`` of the dense retriever (at least 40, pgvector's default)."""
    rrf_k: int = DEFAULT_RRF_K
    """The k of reciprocal rank fusion: a chunk scores the sum of weight / (k + rank) over the
    retrievers that found it. A larger k flattens the difference between ranks."""
    dense_weight: float = DEFAULT_DENSE_WEIGHT
    """What a rank of the dense retriever counts in the fusion: it scores weight / (k + rank)."""
    lexical_weight: float = DEFAULT_LEXICAL_WEIGHT
    """What a rank of the lexical retriever counts in the fusion."""
    snippet_chars: int = DEFAULT_SNIPPET_CHARS
    """The most characters of a chunk's content a hit carries; longer content is cut and the hit
    is marked ``truncated``."""
    rerank_depth: int = DEFAULT_RERANK_DEPTH
    """How many fused hits a reranker reorders before the list is cut to the limit."""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.timeout) and self.timeout > 0):
            raise ValueError("timeout must be a positive number of seconds")
        for name in ("candidates", "rrf_k", "snippet_chars", "rerank_depth"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        for name in ("dense_weight", "lexical_weight"):
            weight: float = getattr(self, name)
            if not (math.isfinite(weight) and weight > 0):
                raise ValueError(f"{name} must be a positive number")

    @classmethod
    def from_settings(cls, settings: Settings) -> "SearchOptions":
        return cls(
            timeout=settings.search_timeout,
            candidates=settings.search_candidates,
            rrf_k=settings.search_rrf_k,
            dense_weight=settings.search_dense_weight,
            lexical_weight=settings.search_lexical_weight,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolDefinition:
    """A definition a ``symbol`` search found: where it is, whole, in its file."""

    name: str
    qualified_name: str
    """Names from the outermost enclosing definition, joined with ``.`` in every language."""
    kind: str
    """``class``, ``function``, ``method`` ... (:data:`memini.chunking.SymbolKind`)."""
    parent: str | None
    language: str | None
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Hit:
    """One chunk found, with what a consumer needs to weigh and cite it (principle 3): the kind of
    source and record, a locator, the time, and for code the repository and commit.

    A chunk of a thread, a meeting or a pushed document also says where it is inside its record
    (docs/requirements.md, section 5, Chunks of pushed records): its :attr:`anchor`, a link to that
    place when the source gave one (:attr:`chunk_url`), and when what it holds happened
    (:attr:`chunk_occurred_at`). :attr:`record_id` with :attr:`anchor` is a knowledge item's
    evidence as it is (:class:`memini.knowledge.EvidenceRef`).

    The snippet is the chunk's stored content as it is, cut to a length and nothing else: it is
    data, never instructions (principle 11). It may be anything a source held.
    """

    chunk_id: str
    record_id: str
    workspace_id: UUID
    workspace: str
    source_id: UUID
    source: str
    """The source's name; for code, the repository."""
    source_kind: SourceKind
    record_kind: RecordKind
    external_id: str
    """The record's id in its source: a ref and path for code, a thread key, a meeting id."""
    external_url: str | None
    """The record's URL in its source, if it has one."""
    occurred_at: datetime
    """Event time of the record: the commit of a code file, the latest message of a thread, a
    meeting's start. The ``since`` and ``until`` filters compare this one."""
    anchor: str | None = None
    """Where the chunk is in its record, in the form evidence names a place: ``msg-<id>`` (the
    first message of a thread chunk), ``seg-<id>`` (the first segment of a transcript window),
    ``summary``, ``summary/<trail>``, ``action-items`` or ``overview`` in a meeting, a heading
    trail in a pushed document. ``None`` for code, for a file of a repository, and for the text
    of a document before its first heading."""
    chunk_url: str | None = None
    """A link to the chunk's own place in the source, when the payload gave one (the ``url`` of
    the first message of a thread chunk)."""
    chunk_occurred_at: datetime | None = None
    """When what the chunk holds happened, where the record says so: the first message of a
    thread chunk, the start of a transcript window, or the start of the meeting for its summary
    and action items. ``None`` for code and documents, which have their record's time."""
    commit: str | None
    """For code: the commit the record is pinned at, when the git connector has recorded it."""
    chunk_kind: str
    """``function``, ``class``, ``window`` ... (:data:`memini.chunking.ChunkKind`)."""
    language: str | None
    path: str | None
    symbol: str | None
    """The qualified name of the definition the chunk is, is part of, or is inside."""
    start_line: int | None
    """First line of the chunk in its record, 1-based; ``None`` for a record without lines."""
    end_line: int | None
    header: str
    """One line naming the chunk: for code, the path and the qualified names it holds."""
    snippet: str
    """The chunk's content, at most :attr:`SearchOptions.snippet_chars` characters of it."""
    truncated: bool
    """Whether :attr:`snippet` is cut short."""
    score: float | None = None
    """What the hits are ordered by: the fused score (``hybrid``), cosine similarity
    (``dense``), the lexical rank (``lexical``); ``None`` where the order is by place."""
    dense_rank: int | None = None
    """1-based rank among the dense retriever's candidates; ``None`` if it did not find it."""
    lexical_rank: int | None = None
    """1-based rank among the lexical retriever's candidates; ``None`` if it did not find it."""
    match_line: int | None = None
    """``exact`` and ``regex``: the line of the first match in the record."""
    definition: SymbolDefinition | None = None
    """``symbol``: the definition found; the chunk is the one that holds its first line."""

    @property
    def repository(self) -> str | None:
        """The repository, for code: the name of the git source."""
        return self.source if self.source_kind == "git" else None

    @property
    def locator(self) -> str:
        """Where the hit is, in one string. A file of a repository (code or Markdown):
        ``repo@sha:path#L12-L30`` (``repo:path#...`` while the commit is not known). Anything
        else: ``source:external_id#anchor``, or ``source:external_id`` for a chunk without an
        anchor. A link to the place is :attr:`link`."""
        if self.source_kind == "git" and self.path is not None:
            at = f"@{self.commit}" if self.commit else ""
            lines = ""
            if self.start_line is not None and self.end_line is not None:
                lines = f"#L{self.start_line}-L{self.end_line}"
            return f"{self.source}{at}:{self.path}{lines}"
        place = "" if self.anchor is None else f"#{self.anchor}"
        return f"{self.source}:{self.external_id}{place}"

    @property
    def link(self) -> str | None:
        """The best URL of the hit: the chunk's own (a message), else the record's; ``None``
        when the source gave neither."""
        return self.chunk_url or self.external_url


class Reranker(Protocol):
    """Reorders the hits of a ranked search: a cross-encoder, say (optional; none ships yet).

    It gets at most :attr:`SearchOptions.rerank_depth` hits in fused order and returns them in
    its own order, with whatever score it gives (``dataclasses.replace(hit, score=...)``); hits it
    leaves out are dropped. The list is then cut to the query's limit.
    """

    async def rerank(self, query: str, hits: Sequence[Hit]) -> list[Hit]: ...


def source_kind(value: object) -> SourceKind:
    """``value`` as a :data:`SourceKind`; raises ``ValueError`` for anything else."""
    for kind in _SOURCE_KINDS:
        if value == kind:
            return kind
    raise ValueError(f"unexpected source kind {value!r}")


def record_kind(value: object) -> RecordKind:
    """``value`` as a :data:`~memini.ids.RecordKind`; raises ``ValueError`` for anything else."""
    for kind in _RECORD_KINDS:
        if value == kind:
            return kind
    raise ValueError(f"unexpected record kind {value!r}")
