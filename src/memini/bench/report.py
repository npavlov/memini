"""What a benchmark run found: plain data, a text table for people and JSON for files.

Times are in seconds, latencies in milliseconds (:class:`~memini.bench.measure.Latency`), sizes
in bytes. A derived figure says so in its name: ``write_seconds`` of a phase is its wall time less
what was measured apart (embedding, reading and chunking), not a measurement of its own.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from memini.bench.measure import Latency

__all__ = [
    "BenchReport",
    "Corpus",
    "DenseRecall",
    "EmbeddingThroughput",
    "HnswBuild",
    "Phase",
    "SearchTiming",
    "Sizes",
    "format_report",
    "write_report",
]


@dataclass(frozen=True, slots=True)
class Phase:
    """One step of building the index."""

    name: str
    seconds: float
    """Wall time of the step."""
    embed_seconds: float = 0.0
    """Of which in the embedder (model calls; for the fake embedder, hashing)."""
    texts_embedded: int = 0
    read_seconds: float = 0.0
    """Of which reading blobs from the mirror (the separate chunking pass)."""
    chunk_seconds: float = 0.0
    """Of which chunking (measured in a separate pass over the same input)."""
    write_seconds: float = 0.0
    """Derived: wall time less embedding, reading and chunking; the database writes (rows,
    lexical column, trigram and HNSW index inserts) and the bookkeeping around them."""
    items: int = 0
    """Files or records the step handled."""
    chunks: int = 0
    peak_rss_bytes: int = 0
    """The process's peak resident memory when the step ended."""


@dataclass(frozen=True, slots=True)
class Corpus:
    repository: str | None = None
    ref: str | None = None
    commit: str | None = None
    """The commit synced (for a cloned URL, the clone's own commit of the upstream tree)."""
    upstream_commit: str | None = None
    files_listed: int = 0
    """Entries of the tree."""
    files_selected: int = 0
    """Files the source's rules select (before binary files are found)."""
    bytes_selected: int = 0
    code_files: int = 0
    documents: int = 0
    binary_files: int = 0
    code_chunks: int = 0
    symbols: int = 0
    threads: int = 0
    meetings: int = 0
    message_chunks: int = 0
    records: int = 0
    """Live records in the index."""
    chunks: int = 0
    """Chunks in the index."""


@dataclass(frozen=True, slots=True)
class EmbeddingThroughput:
    """The configured embedding model on a sample of the corpus's chunk texts."""

    batch_size: int
    texts: int
    seconds: float
    texts_per_second: float
    mean_chars: float
    corpus_estimate_seconds: float
    """Every chunk of the corpus at this rate: an extrapolation."""


@dataclass(frozen=True, slots=True)
class HnswBuild:
    m: int
    ef_construction: int
    seconds: float
    """``CREATE INDEX`` over every vector of the corpus at once (a restore, a rebuild by hand)."""
    size_bytes: int
    maintenance_work_mem: str
    as_built: bool = False
    """The index the service built row by row, measured as it was before any rebuild."""
    cast: str = "vector"
    """What the index is built on: ``vector`` (the service's) or ``halfvec`` (an experiment)."""


@dataclass(frozen=True, slots=True)
class DenseRecall:
    """The HNSW scan against the exact nearest chunks, for the dense retriever's statement."""

    m: int
    ef_construction: int
    ef_search: int
    filtered: bool
    """Messages only (``kinds`` = thread, meeting): a filter that keeps a minority of the rows,
    which the iterative scan serves."""
    queries: int
    k: int
    recall: float
    """Mean share of the k returned chunks that are as near as the exact k-th one: the exact
    top k, or a chunk tied with it (near-duplicate texts tie, and which of them an exact sort
    keeps is arbitrary). Distances at full precision, also for a ``halfvec`` index."""
    latency: Latency
    cast: str = "vector"
    strict_recall: float = 0.0
    """Mean share of the exact top k ids that the index scan returned."""


@dataclass(frozen=True, slots=True)
class SearchTiming:
    mode: str
    concurrency: int
    latency: Latency
    queries_per_second: float
    errors: int
    candidates: int
    """``search_candidates`` (the dense and lexical candidates of hybrid; also ef_search)."""
    filter: str = ""
    """What the searches were narrowed to; empty for none. ``domain: …``: a small directory of
    the repository declared as a domain of the catalog, searched with the ``domains`` filter:
    the chunks it selects and their share of the index."""


@dataclass(frozen=True, slots=True)
class Sizes:
    database: int
    chunk_total: int
    """``chunk`` with its TOAST and every index."""
    chunk_heap: int
    """``chunk`` and its TOAST, without indexes."""
    indexes: dict[str, int]
    """Each index of ``chunk``, by name."""
    record_total: int
    symbol_total: int


@dataclass(slots=True)
class BenchReport:
    setup: dict[str, Any]
    corpus: Corpus = field(default_factory=Corpus)
    phases: list[Phase] = field(default_factory=list[Phase])
    embedding: list[EmbeddingThroughput] = field(default_factory=list[EmbeddingThroughput])
    query_embedding: Latency | None = None
    """The configured model embedding one query, when it was measured."""
    sizes: Sizes | None = None
    hnsw: list[HnswBuild] = field(default_factory=list[HnswBuild])
    recall: list[DenseRecall] = field(default_factory=list[DenseRecall])
    search: list[SearchTiming] = field(default_factory=list[SearchTiming])
    peak_rss_bytes: int = 0
    total_seconds: float = 0.0
    notes: list[str] = field(default_factory=list[str])

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def write_report(path: Path, report: BenchReport) -> None:
    """The report as JSON at ``path``, or on stdout for ``-``."""
    text = json.dumps(report.to_json(), indent=2, sort_keys=True, default=str) + "\n"
    if str(path) == "-":
        print(text, end="")
    else:
        path.write_text(text, encoding="utf-8")


def _mib(value: int) -> str:
    return f"{value / 1024**2:,.1f} MiB"


def _s(value: float) -> str:
    return f"{value:,.1f} s"


def _ms(value: float) -> str:
    return f"{value:,.1f}"


def format_report(report: BenchReport) -> str:
    """The report as text: setup, corpus, phases, embedding, sizes, HNSW, recall, search."""
    lines: list[str] = []
    setup = report.setup
    lines.append("Memini benchmark")
    for key in ("machine", "postgres", "embedder", "search", "config"):
        if key in setup:
            value: object = setup[key]
            text = (
                ", ".join(f"{k}={v}" for k, v in cast(dict[str, object], value).items())
                if isinstance(value, dict)
                else str(value)
            )
            lines.append(f"  {key}: {text}")

    c = report.corpus
    lines.append("")
    lines.append("Corpus")
    if c.repository is not None:
        upstream = f" (upstream {c.upstream_commit})" if c.upstream_commit else ""
        lines.append(f"  repository {c.repository} {c.ref} at {c.commit}{upstream}")
        lines.append(
            f"  files: {c.files_listed:,} listed, {c.files_selected:,} selected"
            f" ({_mib(c.bytes_selected)}), {c.code_files:,} code, {c.documents:,} documents,"
            f" {c.binary_files:,} binary"
        )
        lines.append(f"  code chunks {c.code_chunks:,}, symbols {c.symbols:,}")
    lines.append(
        f"  messages: {c.threads:,} threads, {c.meetings:,} meetings, {c.message_chunks:,} chunks"
    )
    lines.append(f"  index: {c.records:,} records, {c.chunks:,} chunks")

    lines.append("")
    lines.append("Indexing")
    lines.append(
        f"  {'phase':<16} {'wall':>10} {'embed':>10} {'read':>9} {'chunk':>9} {'write*':>10}"
        f" {'items':>8} {'chunks':>8} {'items/s':>8} {'peak RSS':>11}"
    )
    for p in report.phases:
        rate = p.items / p.seconds if p.seconds > 0 else 0.0
        lines.append(
            f"  {p.name:<16} {_s(p.seconds):>10} {_s(p.embed_seconds):>10}"
            f" {_s(p.read_seconds):>9} {_s(p.chunk_seconds):>9} {_s(p.write_seconds):>10}"
            f" {p.items:>8,} {p.chunks:>8,} {rate:>8,.0f} {_mib(p.peak_rss_bytes):>11}"
        )
    lines.append("  * write: derived, wall less embed, read and chunk")

    if report.embedding:
        lines.append("")
        lines.append("Embedding model (sample of the corpus's chunk texts)")
        for e in report.embedding:
            lines.append(
                f"  batch {e.batch_size:>4}: {e.texts:,} texts ({e.mean_chars:,.0f} chars on"
                f" average) in {_s(e.seconds)}: {e.texts_per_second:,.1f} texts/s; every chunk"
                f" of the corpus: {e.corpus_estimate_seconds / 60:,.1f} min (extrapolated)"
            )
    if report.query_embedding is not None:
        q = report.query_embedding
        lines.append(f"  one query: p50 {_ms(q.p50)} ms, p95 {_ms(q.p95)} ms ({q.count} queries)")

    if report.sizes is not None:
        s = report.sizes
        lines.append("")
        lines.append("Database")
        lines.append(
            f"  database {_mib(s.database)}; chunk {_mib(s.chunk_total)}"
            f" (rows {_mib(s.chunk_heap)}); record {_mib(s.record_total)};"
            f" symbol {_mib(s.symbol_total)}"
        )
        for name, size in sorted(s.indexes.items(), key=lambda item: -item[1]):
            lines.append(f"  index {name}: {_mib(size)}")

    if report.hnsw:
        lines.append("")
        lines.append("HNSW builds (CREATE INDEX over the whole corpus)")
        for h in report.hnsw:
            label = " (as built row by row; rebuilt)" if h.as_built else ""
            lines.append(
                f"  m={h.m} ef_construction={h.ef_construction} {h.cast}: {_s(h.seconds)},"
                f" {_mib(h.size_bytes)}, maintenance_work_mem {h.maintenance_work_mem}{label}"
            )

    if report.recall:
        lines.append("")
        lines.append("Dense retriever: HNSW against exact nearest chunks")
        lines.append(
            f"  {'m':>3} {'efc':>4} {'cast':>7} {'ef_search':>9} {'filter':>9} {'k':>3}"
            f" {'recall':>7} {'ids':>6} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8}"
        )
        for r in report.recall:
            lines.append(
                f"  {r.m:>3} {r.ef_construction:>4} {r.cast:>7} {r.ef_search:>9}"
                f" {'messages' if r.filtered else 'none':>9} {r.k:>3} {r.recall:>7.3f}"
                f" {r.strict_recall:>6.3f}"
                f" {_ms(r.latency.p50):>8} {_ms(r.latency.p95):>8} {_ms(r.latency.p99):>8}"
            )

    if report.search:
        lines.append("")
        lines.append("Search latency (memini.search.search, end to end, ms)")
        lines.append(
            f"  {'mode':<8} {'conc':>4} {'cand':>4} {'n':>5} {'p50':>8} {'p95':>8} {'p99':>8}"
            f" {'max':>8} {'qps':>8} {'errors':>6}  filter"
        )
        for t in report.search:
            lines.append(
                f"  {t.mode:<8} {t.concurrency:>4} {t.candidates:>4} {t.latency.count:>5}"
                f" {_ms(t.latency.p50):>8} {_ms(t.latency.p95):>8} {_ms(t.latency.p99):>8}"
                f" {_ms(t.latency.max):>8} {t.queries_per_second:>8,.1f} {t.errors:>6}"
                f"  {t.filter or 'none'}"
            )

    lines.append("")
    lines.append(
        f"Peak RSS of the process: {_mib(report.peak_rss_bytes)}; total {_s(report.total_seconds)}"
    )
    for note in report.notes:
        lines.append(f"Note: {note}")
    return "\n".join(lines)
