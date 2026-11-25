"""Running the benchmark: a scratch index of a repository and a synthetic message set, then timings.

:func:`run_bench` does, in order:

1. Creates a scratch database on the configured Postgres server (``memini_bench_`` and 12 random
   hex digits, from ``template0``) and migrates it, as the retrieval eval does
   (:mod:`memini.evaluation.runner`). The user's database is only connected to for ``CREATE
   DATABASE``. The role needs ``CREATEDB``.
2. The repository (when one is given) as a repository of one workspace: the mirror is created from
   it (``mirror fetch``); every selected file is read from the mirror and chunked in a pass of its
   own, timed apart (``read`` and ``chunk``); then the service's own sync
   (:func:`memini.gitsync.run_git_sync`) reads, chunks, embeds and writes everything again
   (``git sync``). Its embedding time is measured around the embedder; the rest, less reading and
   chunking as the separate pass measured them, is the database writes (``write``, derived).
3. The synthetic message set (:mod:`memini.bench.corpus`) through :func:`memini.ingest.ingest`
   with the service's indexing hook, in batches of ``ingest_batch`` records (``messages``); its
   payloads are chunked once apart first, for the ``chunk`` column.
4. ``VACUUM ANALYZE`` of the index tables, as autovacuum would get to, then their sizes.
5. The configured embedding model on a sample of the corpus's chunk texts at each batch size asked
   for, and on single queries, when a sample embedder is given: the throughput and the time every
   chunk of the corpus would take at it (an extrapolation, said so in the report).
6. Search latency: each mode's queries (:mod:`memini.bench.queries`) through
   :func:`memini.search.search`, as the API calls it, a pooled connection per search, at each
   concurrency; hybrid also at each value of ``candidates`` asked for. Every query runs once
   first, untimed, so that the timings are of a warm cache, as a service that has been running
   for a while has: a first pass measured right after indexing was two to four times slower.
7. The dense retriever's statement (:func:`memini.search.retrieval.dense_statement`) against the
   exact nearest chunks (the same statement with index scans off), for the first
   ``recall_queries`` ranked queries, unfiltered and filtered to messages, at each ``ef_search``:
   recall at 10 and at the hybrid candidates, and the statement's latency. On the index the
   service built row by row first; then that index rebuilt with ``CREATE INDEX`` (timed, at the
   server's ``maintenance_work_mem``: what a restore pays); then each HNSW setting of the grid,
   built at ``maintenance_work_mem`` and measured the same way. A setting may build the index on
   a cast to ``halfvec`` (``16:64:halfvec``), which the service does not do: an experiment. Each
   setting's probes run once untimed before they are timed.
8. Drops the database and removes the temporary directory, unless ``keep``.

Every step's wall time and the process's peak resident memory after it go into the report.
"""

import os
import platform
import random
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

import anyio
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

from memini import __version__
from memini.auth import Principal, ReadFilter, WriteAccess, authorize_read
from memini.bench.corpus import MessageSet, message_batches
from memini.bench.measure import Latency, TimedEmbedder, peak_rss_bytes
from memini.bench.queries import BenchQuery, build_queries
from memini.bench.report import (
    BenchReport,
    DenseRecall,
    EmbeddingThroughput,
    HnswBuild,
    Phase,
    SearchTiming,
    Sizes,
)
from memini.bench.repository import PreparedRepository
from memini.catalog import CatalogError, ProjectDocument, run_put_project
from memini.chunking import FileChunks, chunk_file, chunk_markdown, chunk_payload
from memini.db import Pool, connect, migrate, open_pool
from memini.embeddings import Embedder, Vector
from memini.git import GitOptions, MirrorManager
from memini.gitsync import DEFAULT_LIMITS, GIT_SYNC, SyncLimits, run_git_sync
from memini.gitsync.rules import FileRules, file_kind
from memini.index import VectorIndex
from memini.ingest import Batch, PayloadIndexer, RecordIn, canonical_json, ingest
from memini.jobs import Job
from memini.search import (
    InvalidSearchError,
    Mode,
    SearchFilters,
    SearchOptions,
    SearchQuery,
    SearchTimeoutError,
    search,
)
from memini.search.model import DEFAULT_LIMIT
from memini.search.patterns import glob_regex
from memini.search.retrieval import ChunkScope, dense_statement
from memini.sources import NewGitSource, NewPushSource, create_git_source, create_push_source
from memini.workspaces import create_workspace, ensure_principal

__all__ = [
    "SCRATCH_PREFIX",
    "BenchConfig",
    "BenchSetupError",
    "HnswSetting",
    "run_bench",
]

type Conn = AsyncConnection[TupleRow]

SCRATCH_PREFIX: Final = "memini_bench_"
"""The name of every scratch database of the benchmark starts with this."""
PRINCIPAL: Final = "bench"
WORKSPACE: Final = "bench"
REPO_SOURCE: Final = "repo"
MESSAGES_SOURCE: Final = "messages"
SERVICE_HNSW: Final = (16, 64)
"""pgvector's defaults, which :meth:`memini.index.VectorIndex.create_statement` builds with."""
_RECORD_ID: Final = "0" * 64
"""The record id the separate chunking passes cut with: chunk ids are hashed with it, nothing
else depends on it."""
_MESSAGE_KINDS: Final = ("thread", "meeting")
BENCH_PROJECT: Final = "bench"
SMALL_DOMAIN: Final = "small"
SMALL_DOMAIN_CHUNKS: Final = 20
"""The fewest chunks the directory declared as the small domain should hold: two pages of
hits. The smallest directory with that many is taken; without one, the largest."""


class BenchSetupError(Exception):
    """The benchmark cannot build its scratch index: no git, no right to create a database."""


@dataclass(frozen=True, slots=True)
class HnswSetting:
    m: int
    ef_construction: int
    halfvec: bool = False
    """Build the index on a cast to ``halfvec`` (and search it so): half the bytes per vector."""

    @property
    def cast(self) -> str:
        return "halfvec" if self.halfvec else "vector"

    def __post_init__(self) -> None:
        if not 2 <= self.m <= 100:
            raise ValueError("m is from 2 to 100")
        if not 4 <= self.ef_construction <= 1000 or self.ef_construction < 2 * self.m:
            raise ValueError("ef_construction is from 4 to 1000 and at least twice m")


@dataclass(frozen=True, slots=True)
class _HalfvecIndex(VectorIndex):
    """The index of a key built on a cast to ``halfvec`` whatever the dimension: an experiment of
    the benchmark, not what the service builds (:class:`~memini.index.VectorIndex`)."""

    @property
    def cast_type(self) -> Literal["vector", "halfvec"]:
        return "halfvec"


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchConfig:
    """What a run builds and measures (besides the embedders)."""

    repository: PreparedRepository | None
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    messages: MessageSet = field(default_factory=lambda: MessageSet(threads=0, meetings=0))
    ingest_batch: int = 200
    sync_limits: SyncLimits = DEFAULT_LIMITS
    options: SearchOptions = field(default_factory=SearchOptions)
    queries: int = 200
    concurrency: tuple[int, ...] = (1, 8)
    candidates: tuple[int, ...] = ()
    """Values of ``search_candidates`` to time hybrid search at (concurrency 1), besides the
    configured one."""
    ef_search: tuple[int, ...] = ()
    """``hnsw.ef_search`` values to measure the dense statement at; empty: the service's."""
    recall_queries: int = 100
    hnsw: tuple[HnswSetting, ...] = ()
    """HNSW settings to build and measure besides the one the service built."""
    maintenance_work_mem: str = "1GB"
    """For the builds of the grid."""
    embedding_sample: int = 0
    embedding_batch_sizes: tuple[int, ...] = ()
    seed: int = 1

    def __post_init__(self) -> None:
        if self.ingest_batch < 1 or self.queries < 1 or self.recall_queries < 0:
            raise ValueError("batch and query counts are positive")
        if not self.concurrency or min(self.concurrency) < 1:
            raise ValueError("each concurrency is at least 1")

    @property
    def pool_size(self) -> int:
        return max(self.concurrency) + 2


def _quiet(text: str) -> None:
    del text


# --- The scratch database ------------------------------------------------------------------------


@asynccontextmanager
async def _scratch_database(server_url: str, *, keep: bool) -> AsyncGenerator[tuple[str, str]]:
    name = f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
    async with connect(server_url, autocommit=True) as admin:
        try:
            await admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
            )
        except errors.InsufficientPrivilege:
            raise BenchSetupError(
                "the benchmark builds its index in a scratch database, and the database role may"
                " not create one: grant it CREATEDB, or run it as a role that has it"
            ) from None
    try:
        yield name, make_conninfo(server_url, dbname=name)
    finally:
        if not keep:
            async with connect(server_url, autocommit=True) as admin:
                await admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


async def _scalar(conn: Conn, query: str, params: Sequence[Any] = ()) -> Any:
    cur = await conn.execute(query, params)  # pyright: ignore[reportArgumentType]
    row = await cur.fetchone()
    return None if row is None else row[0]


async def _postgres_setup(conn: Conn) -> dict[str, Any]:
    setup: dict[str, Any] = {
        "version": await _scalar(conn, "SHOW server_version"),
        "pgvector": await _scalar(
            conn, "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ),
    }
    for name in (
        "shared_buffers",
        "work_mem",
        "maintenance_work_mem",
        "effective_cache_size",
        "max_parallel_maintenance_workers",
        "max_parallel_workers_per_gather",
    ):
        setup[name] = await _scalar(conn, f"SHOW {name}")
    return setup


def _machine() -> dict[str, Any]:
    memory = 0
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, check=True
            ).stdout
            memory = int(out.strip())
        else:
            with open("/proc/meminfo", encoding="ascii") as meminfo:
                for line in meminfo:
                    if line.startswith("MemTotal:"):
                        memory = int(line.split()[1]) * 1024
                        break
    except (OSError, ValueError):
        memory = 0
    return {
        "system": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "cpus": os.cpu_count() or 0,
        "memory_gib": round(memory / 1024**3, 1),
        "python": platform.python_version(),
    }


# --- Building the index --------------------------------------------------------------------------


class _Reservoir:
    """A uniform sample of at most ``size`` texts out of every text offered, fixed by a seed."""

    def __init__(self, size: int, seed: int) -> None:
        self.size = size
        self.seen = 0
        self.texts: list[str] = []
        self._rng = random.Random(seed)  # noqa: S311 - a fixed sample, not cryptography

    def offer(self, text: str) -> None:
        if self.size <= 0:
            return
        self.seen += 1
        if len(self.texts) < self.size:
            self.texts.append(text)
            return
        slot = self._rng.randrange(self.seen)
        if slot < self.size:
            self.texts[slot] = text


def _phase(name: str, seconds: float, **values: Any) -> Phase:
    return Phase(name=name, seconds=seconds, peak_rss_bytes=peak_rss_bytes(), **values)


def _file_batches(
    files: Sequence[tuple[str, str, int]], limits: SyncLimits
) -> Iterator[list[tuple[str, str, int]]]:
    """Files (path, oid, size) in batches as the sync cuts them (``limits``)."""
    batch: list[tuple[str, str, int]] = []
    size = 0
    for file in files:
        if batch and (len(batch) >= limits.batch_files or size + file[2] > limits.batch_bytes):
            yield batch
            batch, size = [], 0
        batch.append(file)
        size += file[2]
    if batch:
        yield batch


def _chunk_one(record_id: str, path: str, content: bytes) -> FileChunks:
    if file_kind(path) == "document":
        return chunk_markdown(record_id, path, content)
    return chunk_file(record_id, path, content)


@dataclass(slots=True)
class _ChunkPass:
    read_seconds: float = 0.0
    chunk_seconds: float = 0.0
    code_files: int = 0
    documents: int = 0
    binary: int = 0
    chunks: int = 0
    symbols: int = 0


async def _chunk_pass(
    mirrors: MirrorManager,
    mirror: Any,
    files: Sequence[tuple[str, str, int]],
    limits: SyncLimits,
    sample: _Reservoir,
) -> _ChunkPass:
    """Read and chunk every selected file once, outside the database: the times the sync spends
    on the same work."""
    done = _ChunkPass()
    for batch in _file_batches(files, limits):
        started = time.perf_counter()
        blobs = await mirrors.read_blobs(mirror, sorted({oid for _, oid, _ in batch}))
        done.read_seconds += time.perf_counter() - started
        started = time.perf_counter()
        for path, oid, _ in batch:
            chunks = _chunk_one(_RECORD_ID, path, blobs[oid])
            if chunks.fallback == "binary":
                done.binary += 1
                continue
            if file_kind(path) == "document":
                done.documents += 1
            else:
                done.code_files += 1
            done.chunks += len(chunks.chunks)
            done.symbols += len(chunks.symbols)
            for chunk in chunks.chunks:
                sample.offer(f"{chunk.header}\n{chunk.content}")
        done.chunk_seconds += time.perf_counter() - started
    return done


async def _index_repository(
    conn: Conn,
    pool: Pool,
    timed: TimedEmbedder,
    access: WriteAccess,
    config: BenchConfig,
    repository: PreparedRepository,
    work: Path,
    report: BenchReport,
    sample: _Reservoir,
    say: Callable[[str], None],
) -> None:
    options = GitOptions(mirror_dir=work / "mirrors", local_roots=(repository.location.parent,))
    mirrors = MirrorManager(options)
    source = await create_git_source(
        conn,
        access,
        WORKSPACE,
        NewGitSource(
            name=REPO_SOURCE,
            location=str(repository.location),
            refs=(repository.ref,),
            include=config.include,
            exclude=config.exclude,
        ),
        options=options,
    )
    if source.git is None:  # pragma: no cover - create_git_source makes a repository
        raise RuntimeError("the repository source has no git configuration")
    say(f"mirroring {repository.origin} {repository.ref}")
    started = time.perf_counter()
    fetched = await mirrors.sync(source.id, source.git.location)
    report.phases.append(_phase("mirror fetch", time.perf_counter() - started))

    commit = await mirrors.resolve(fetched.mirror, repository.ref)
    entries = await mirrors.ls_tree(fetched.mirror, commit)
    rules = FileRules(source.git, options.max_blob_size)
    files = sorted(
        (entry.path, entry.oid, entry.size)
        for entry in entries
        if entry.size is not None and rules.skip_reason(entry.path, entry.mode, entry.size) is None
    )
    say(f"reading and chunking {len(files):,} files of {len(entries):,} at {commit[:12]}")
    started = time.perf_counter()
    chunked = await _chunk_pass(mirrors, fetched.mirror, files, config.sync_limits, sample)
    report.phases.append(
        _phase(
            "read and chunk",
            time.perf_counter() - started,
            read_seconds=chunked.read_seconds,
            chunk_seconds=chunked.chunk_seconds,
            items=len(files),
            chunks=chunked.chunks,
        )
    )

    say(f"syncing {chunked.chunks:,} chunks through the service's git sync")
    job = Job(
        id=uuid.uuid4(),
        workspace_id=source.workspace_id,
        owner_id=access.owner_id,
        source_id=source.id,
        stream_id=None,
        kind=GIT_SYNC,
        args={},
        attempt=1,
        max_attempts=1,
        worker="bench",
    )
    timed.reset()
    started = time.perf_counter()
    await run_git_sync(pool, mirrors, timed, job, limits=config.sync_limits)
    seconds = time.perf_counter() - started
    report.phases.append(
        _phase(
            "git sync",
            seconds,
            embed_seconds=timed.seconds,
            texts_embedded=timed.texts,
            read_seconds=chunked.read_seconds,
            chunk_seconds=chunked.chunk_seconds,
            write_seconds=max(
                0.0, seconds - timed.seconds - chunked.read_seconds - chunked.chunk_seconds
            ),
            items=len(files) - chunked.binary,
            chunks=chunked.chunks,
        )
    )
    report.corpus = replace(
        report.corpus,
        repository=repository.origin,
        ref=repository.ref,
        commit=commit,
        upstream_commit=repository.upstream_commit,
        files_listed=len(entries),
        files_selected=len(files),
        bytes_selected=sum(size for _, _, size in files),
        code_files=chunked.code_files,
        documents=chunked.documents,
        binary_files=chunked.binary,
        code_chunks=chunked.chunks,
        symbols=chunked.symbols,
    )


def _chunk_records(batch: Sequence[RecordIn], sample: _Reservoir) -> int:
    """Chunk a batch of synthetic records as the indexing hook will (their payloads before
    redaction, which changes nothing in synthetic text): the chunk count."""
    chunks = 0
    for item in batch:
        if item.payload is None:
            continue
        found = chunk_payload(_RECORD_ID, item.kind, canonical_json(item.payload))
        chunks += len(found.chunks)
        for chunk in found.chunks:
            sample.offer(f"{chunk.header}\n{chunk.content}")
    return chunks


async def _index_messages(
    conn: Conn,
    timed: TimedEmbedder,
    access: WriteAccess,
    config: BenchConfig,
    report: BenchReport,
    sample: _Reservoir,
    say: Callable[[str], None],
) -> None:
    spec = config.messages
    await create_push_source(conn, access, WORKSPACE, NewPushSource(name=MESSAGES_SOURCE))
    indexer = PayloadIndexer(timed)
    say(f"ingesting {spec.threads:,} threads and {spec.meetings:,} meetings")
    timed.reset()
    chunk_seconds = 0.0
    generate_seconds = 0.0
    chunks = 0
    started = time.perf_counter()
    batches = message_batches(spec, batch_size=config.ingest_batch)
    while True:
        moment = time.perf_counter()
        batch = next(batches, None)
        generate_seconds += time.perf_counter() - moment
        if batch is None:
            break
        moment = time.perf_counter()
        chunks += await anyio.to_thread.run_sync(_chunk_records, batch, sample)
        chunk_seconds += time.perf_counter() - moment
        await ingest(
            conn, access, WORKSPACE, MESSAGES_SOURCE, Batch(records=batch), indexer=indexer
        )
    # The ingest alone: generating the records and the separate chunking pass are left out.
    seconds = time.perf_counter() - started - generate_seconds - chunk_seconds
    report.phases.append(
        _phase(
            "messages",
            seconds,
            embed_seconds=timed.seconds,
            texts_embedded=timed.texts,
            chunk_seconds=chunk_seconds,
            write_seconds=max(0.0, seconds - timed.seconds - chunk_seconds),
            items=spec.records,
            chunks=chunks,
        )
    )
    report.corpus = replace(
        report.corpus, threads=spec.threads, meetings=spec.meetings, message_chunks=chunks
    )


# --- Sizes ---------------------------------------------------------------------------------------


async def _vacuum(conn: Conn) -> None:
    for table in ("record", "chunk", "symbol"):
        await conn.execute(sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(table)))


async def _sizes(conn: Conn) -> Sizes:
    cur = await conn.execute(
        "SELECT i.relname, pg_relation_size(i.oid) FROM pg_index x"
        " JOIN pg_class i ON i.oid = x.indexrelid WHERE x.indrelid = 'chunk'::regclass"
    )
    indexes = {row[0]: int(row[1]) for row in await cur.fetchall()}
    return Sizes(
        database=int(await _scalar(conn, "SELECT pg_database_size(current_database())")),
        chunk_total=int(await _scalar(conn, "SELECT pg_total_relation_size('chunk')")),
        chunk_heap=int(await _scalar(conn, "SELECT pg_table_size('chunk')")),
        indexes=indexes,
        record_total=int(await _scalar(conn, "SELECT pg_total_relation_size('record')")),
        symbol_total=int(await _scalar(conn, "SELECT pg_total_relation_size('symbol')")),
    )


# --- The embedding model -------------------------------------------------------------------------


async def _embedding_throughput(
    make: Callable[[int], Embedder],
    batch_sizes: Sequence[int],
    texts: Sequence[str],
    corpus_chunks: int,
) -> list[EmbeddingThroughput]:
    found: list[EmbeddingThroughput] = []
    mean_chars = sum(len(text) for text in texts) / len(texts)
    for batch_size in batch_sizes:
        embedder = make(batch_size)
        try:
            await embedder.embed(texts[:1])  # load the model
            started = time.perf_counter()
            await embedder.embed(list(texts))
            seconds = time.perf_counter() - started
        finally:
            await embedder.aclose()
        rate = len(texts) / seconds if seconds > 0 else 0.0
        found.append(
            EmbeddingThroughput(
                batch_size=batch_size,
                texts=len(texts),
                seconds=seconds,
                texts_per_second=rate,
                mean_chars=mean_chars,
                corpus_estimate_seconds=corpus_chunks / rate if rate > 0 else 0.0,
            )
        )
    return found


async def _query_embedding(embedder: Embedder, queries: Sequence[BenchQuery]) -> Latency:
    times: list[float] = []
    await embedder.embed([queries[0].text], kind="query")
    for query in queries:
        started = time.perf_counter()
        await embedder.embed([query.text], kind="query")
        times.append(time.perf_counter() - started)
    return Latency.of(times)


# --- Search --------------------------------------------------------------------------------------


async def _one_search(
    pool: Pool,
    embedder: Embedder,
    access: ReadFilter,
    query: BenchQuery,
    options: SearchOptions,
    filters: SearchFilters | None = None,
) -> bool:
    async with pool.connection() as conn:
        try:
            await search(
                conn,
                embedder,
                access,
                SearchQuery(
                    text=query.text,
                    mode=query.mode,
                    limit=DEFAULT_LIMIT,
                    filters=filters or SearchFilters(),
                ),
                options=options,
            )
        except (SearchTimeoutError, InvalidSearchError):
            return False
    return True


async def _time_searches(
    pool: Pool,
    embedder: Embedder,
    access: ReadFilter,
    queries: Sequence[BenchQuery],
    options: SearchOptions,
    concurrency: int,
    filters: SearchFilters | None = None,
) -> tuple[list[float], int, float]:
    """Each query once, ``concurrency`` at a time: latencies, errors, wall time."""
    latencies: list[float] = []
    errors_ = 0
    pending = iter(queries)

    async def worker() -> None:
        nonlocal errors_
        for query in pending:
            started = time.perf_counter()
            ok = await _one_search(pool, embedder, access, query, options, filters)
            latencies.append(time.perf_counter() - started)
            if not ok:
                errors_ += 1

    started = time.perf_counter()
    async with anyio.create_task_group() as group:
        for _ in range(concurrency):
            group.start_soon(worker)
    return latencies, errors_, time.perf_counter() - started


async def _small_domain(
    conn: Conn, write: WriteAccess, access: ReadFilter, say: Callable[[str], None]
) -> tuple[SearchFilters, str] | None:
    """Declare one small directory of the repository as a domain of the catalog, as
    ``put_project`` does for an agent: the filter that searches it, and what it selects, in
    words. ``None`` when the repository has no directory.

    The directory is the one with the fewest chunks among those with at least
    :data:`SMALL_DOMAIN_CHUNKS`; the domain holds it and everything below it.
    """
    cur = await conn.execute(
        "SELECT regexp_replace(c.path, '/[^/]*$', ''), count(*) FROM chunk c"
        " JOIN source s ON s.id = c.source_id"
        " WHERE s.name = %s AND c.path LIKE %s GROUP BY 1 ORDER BY 2, 1",
        (REPO_SOURCE, "%/%"),
    )
    # A directory whose name is itself a glob would select more than it names, and one with a
    # line break is no mask at all.
    counted = [(row[0], row[1]) for row in await cur.fetchall() if not set(row[0]) & set("*?\n\r")]
    if not counted:
        return None
    enough = [each for each in counted if each[1] >= SMALL_DOMAIN_CHUNKS]
    directory = enough[0][0] if enough else counted[-1][0]
    document = ProjectDocument.model_validate(
        {
            "title": "Benchmark",
            "services": [{"name": REPO_SOURCE, "repository": REPO_SOURCE}],
            "domains": [
                {
                    "name": SMALL_DOMAIN,
                    "code": [{"service": REPO_SOURCE, "paths": [f"/{directory}"]}],
                }
            ],
        }
    )
    try:
        result = await run_put_project(conn, write, access, WORKSPACE, BENCH_PROJECT, document)
    except CatalogError as error:  # a directory name the catalog refuses, a secret's shape say
        say(f"no small domain: {error.message}")
        return None
    cur = await conn.execute(
        "SELECT count(*) FILTER (WHERE c.path ~ %s), count(*) FROM chunk c", (_below(directory),)
    )
    row = await cur.fetchone()
    selected, total = (0, 0) if row is None else (row[0], row[1])
    share = selected / total if total else 0.0
    files = sum(mask.files for mask in result.masks)
    say(f"small domain: {directory} ({files} files, {selected} of {total} chunks)")
    return (
        SearchFilters(domains=(SMALL_DOMAIN,)),
        f"domain: {selected:,} chunks, {share:.2%} of the index",
    )


def _below(directory: str) -> str:
    """The expression the domain's mask is for Postgres, as search sends it."""
    return glob_regex(f"/{directory}")


async def _search_latency(
    pool: Pool,
    embedder: Embedder,
    access: ReadFilter,
    queries: dict[Mode, list[BenchQuery]],
    config: BenchConfig,
    report: BenchReport,
    say: Callable[[str], None],
    narrow: tuple[SearchFilters, str] | None = None,
) -> None:
    options = config.options
    say("one untimed pass of every query, to warm the caches")
    for items in queries.values():
        if items:
            await _time_searches(pool, embedder, access, items, options, 1)
    for concurrency in config.concurrency:
        for mode, items in queries.items():
            if not items:
                continue
            say(f"timing {len(items)} {mode} searches at concurrency {concurrency}")
            latencies, failed, wall = await _time_searches(
                pool, embedder, access, items, options, concurrency
            )
            report.search.append(
                SearchTiming(
                    mode=mode,
                    concurrency=concurrency,
                    latency=Latency.of(latencies),
                    queries_per_second=len(items) / wall if wall > 0 else 0.0,
                    errors=failed,
                    candidates=options.candidates,
                )
            )
    hybrid = queries.get("hybrid", [])
    for candidates in config.candidates:
        if not hybrid or candidates == options.candidates:
            continue
        varied = replace(options, candidates=candidates)
        say(f"timing {len(hybrid)} hybrid searches with {candidates} candidates")
        latencies, failed, wall = await _time_searches(pool, embedder, access, hybrid, varied, 1)
        report.search.append(
            SearchTiming(
                mode="hybrid",
                concurrency=1,
                latency=Latency.of(latencies),
                queries_per_second=len(hybrid) / wall if wall > 0 else 0.0,
                errors=failed,
                candidates=candidates,
            )
        )
    if narrow is None:
        return
    # A small domain in a large index: the filter of the catalog keeps a small share of the
    # chunks, which the dense retriever must find past everything else (migration 0018 gives
    # the planner an index by source and path for it).
    filters, selected = narrow
    for mode in ("hybrid", "dense", "lexical"):
        items = [query for query in queries.get(mode, []) if query.origin == "code"]
        if not items:
            continue
        say(f"timing {len(items)} {mode} searches of the small domain")
        await _time_searches(pool, embedder, access, items, options, 1, filters)  # warm
        latencies, failed, wall = await _time_searches(
            pool, embedder, access, items, options, 1, filters
        )
        report.search.append(
            SearchTiming(
                mode=mode,
                concurrency=1,
                latency=Latency.of(latencies),
                queries_per_second=len(items) / wall if wall > 0 else 0.0,
                errors=failed,
                candidates=options.candidates,
                filter=selected,
            )
        )


# --- The dense retriever against exact search ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Probe:
    vector: Vector
    filtered: bool
    exact: tuple[tuple[str, float], ...]
    """The exact nearest chunks (id, distance), nearest first, as many as the largest k."""


def _scope(
    access: ReadFilter, workspace: tuple[uuid.UUID, str], index: VectorIndex, *, filtered: bool
) -> ChunkScope:
    return ChunkScope(
        access=access,
        workspaces={workspace[0]: workspace[1]},
        index=index,
        source_ids=None,
        filters=SearchFilters(kinds=_MESSAGE_KINDS) if filtered else SearchFilters(),
    )


async def _dense_ids(
    conn: Conn, scope: ChunkScope, vector: Vector, k: int, *, ef_search: int | None
) -> list[tuple[str, float]]:
    """The (id, distance) rows the dense statement returns; ``ef_search`` None: exact, with index
    scans off."""
    async with conn.transaction():
        await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")  # as search does
        if ef_search is None:
            await conn.execute("SET LOCAL enable_indexscan = off")
        else:
            await conn.execute(
                sql.SQL(
                    "SET LOCAL hnsw.ef_search = {}; SET LOCAL hnsw.iterative_scan = relaxed_order"
                ).format(sql.Literal(ef_search))
            )
        rows = await dense_statement(scope, vector, k).rows(conn)
    return [(row[0], float(row[1])) for row in rows]


async def _exact_distances(
    conn: Conn, index: VectorIndex, vector: Vector, ids: Sequence[str]
) -> dict[str, float]:
    """The distances of chunks ``ids`` from ``vector`` at full precision (``vector``, whatever
    the index is built on)."""
    exact = VectorIndex(index.embedding_key, index.dimension)
    query = sql.SQL("SELECT c.id, {} FROM chunk c WHERE c.id = ANY(%s)").format(
        exact.distance(sql.Placeholder(), "c")
    )
    cur = await conn.execute(query, (vector, list(ids)))
    return {row[0]: float(row[1]) for row in await cur.fetchall()}


async def _probes(
    conn: Conn,
    embedder: Embedder,
    access: ReadFilter,
    workspace: tuple[uuid.UUID, str],
    index: VectorIndex,
    queries: Sequence[BenchQuery],
    *,
    k: int,
    messages: bool,
) -> list[_Probe]:
    vectors = await embedder.embed([query.text for query in queries], kind="query")
    probes: list[_Probe] = []
    for filtered in (False, True) if messages else (False,):
        scope = _scope(access, workspace, index, filtered=filtered)
        for vector in vectors:
            exact = await _dense_ids(conn, scope, vector, k, ef_search=None)
            probes.append(_Probe(vector, filtered, tuple(exact)))
    return probes


async def _recall(
    conn: Conn,
    access: ReadFilter,
    workspace: tuple[uuid.UUID, str],
    index: VectorIndex,
    probes: Sequence[_Probe],
    setting: HnswSetting,
    ef_searches: Sequence[int],
    ks: Sequence[int],
) -> list[DenseRecall]:
    found: list[DenseRecall] = []
    for filtered in sorted({probe.filtered for probe in probes}):
        scope = _scope(access, workspace, index, filtered=filtered)
        group = [probe for probe in probes if probe.filtered == filtered]
        for probe in group:  # untimed, to bring the index into the cache
            await _dense_ids(conn, scope, probe.vector, max(ks), ef_search=ef_searches[0])
        for ef_search in ef_searches:
            for k in ks:
                shares: list[float] = []
                strict: list[float] = []
                times: list[float] = []
                for probe in group:
                    expected = probe.exact[:k]
                    if not expected:
                        continue
                    started = time.perf_counter()
                    got = await _dense_ids(conn, scope, probe.vector, k, ef_search=ef_search)
                    times.append(time.perf_counter() - started)
                    wanted = {chunk_id for chunk_id, _ in expected}
                    returned = [chunk_id for chunk_id, _ in got]
                    strict.append(len(wanted.intersection(returned)) / len(expected))
                    # A chunk as near as the k-th exact one counts too: near-duplicate texts tie,
                    # and which of them the exact sort keeps is arbitrary.
                    others = [chunk_id for chunk_id in returned if chunk_id not in wanted]
                    near = (
                        await _exact_distances(conn, index, probe.vector, others) if others else {}
                    )
                    bound = expected[-1][1] + 1e-6
                    good = len(returned) - len(others) + sum(1 for d in near.values() if d <= bound)
                    shares.append(min(good, len(expected)) / len(expected))
                found.append(
                    DenseRecall(
                        m=setting.m,
                        ef_construction=setting.ef_construction,
                        cast=setting.cast,
                        ef_search=ef_search,
                        filtered=filtered,
                        queries=len(shares),
                        k=k,
                        recall=sum(shares) / len(shares) if shares else 0.0,
                        strict_recall=sum(strict) / len(strict) if strict else 0.0,
                        latency=Latency.of(times),
                    )
                )
    return found


async def _build_index(
    conn: Conn, index: VectorIndex, setting: HnswSetting | None, maintenance_work_mem: str | None
) -> tuple[float, str]:
    """Drop the index of ``index``'s key and create it over every row: with ``setting``, or as the
    service does (``None``). Returns the seconds and the ``maintenance_work_mem`` used."""
    await conn.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(index.name)))
    statement: sql.Composable = index.create_statement()
    if setting is not None:
        statement = sql.SQL(
            "CREATE INDEX {name} ON chunk USING hnsw ({column} {ops})"
            " WITH (m = {m}, ef_construction = {efc}) WHERE {where}"
        ).format(
            name=sql.Identifier(index.name),
            column=index.column(),
            ops=sql.SQL(f"{index.cast_type}_cosine_ops"),
            m=sql.Literal(setting.m),
            efc=sql.Literal(setting.ef_construction),
            where=index.predicate(),
        )
    async with conn.transaction():
        if maintenance_work_mem is not None:
            await conn.execute(
                sql.SQL("SET LOCAL maintenance_work_mem = {}").format(
                    sql.Literal(maintenance_work_mem)
                )
            )
        used = str(await _scalar(conn, "SHOW maintenance_work_mem"))
        started = time.perf_counter()
        await conn.execute(statement)
        seconds = time.perf_counter() - started
    await conn.execute(sql.SQL("ANALYZE chunk"))
    return seconds, used


async def _index_size(conn: Conn, index: VectorIndex) -> int:
    value = await _scalar(conn, "SELECT pg_relation_size(to_regclass(%s))", (index.name,))
    return int(value or 0)


async def _dense_quality(
    conn: Conn,
    embedder: Embedder,
    access: ReadFilter,
    workspace: tuple[uuid.UUID, str],
    queries: Sequence[BenchQuery],
    config: BenchConfig,
    report: BenchReport,
    say: Callable[[str], None],
) -> None:
    index = VectorIndex.of(embedder.spec)
    if not index.indexed or not queries:
        return
    ks = sorted({DEFAULT_LIMIT, config.options.candidates})
    ef_searches = config.ef_search or (max(config.options.candidates, DEFAULT_LIMIT),)
    messages = config.messages.records > 0
    say(f"exact nearest chunks of {len(queries)} queries (index scans off)")
    probes = await _probes(
        conn, embedder, access, workspace, index, queries, k=max(ks), messages=messages
    )
    service = HnswSetting(*SERVICE_HNSW)
    say("dense statement against exact, on the index built row by row")
    report.recall += await _recall(conn, access, workspace, index, probes, service, ef_searches, ks)
    size = await _index_size(conn, index)
    say("rebuilding the service's HNSW index with CREATE INDEX")
    seconds, used = await _build_index(conn, index, None, None)
    report.hnsw.append(
        HnswBuild(
            m=service.m,
            ef_construction=service.ef_construction,
            seconds=seconds,
            size_bytes=size,
            maintenance_work_mem=used,
            as_built=True,
        )
    )
    for setting in config.hnsw:
        say(f"HNSW m={setting.m} ef_construction={setting.ef_construction} on {setting.cast}")
        variant = _HalfvecIndex(index.embedding_key, index.dimension) if setting.halfvec else index
        seconds, used = await _build_index(conn, variant, setting, config.maintenance_work_mem)
        report.hnsw.append(
            HnswBuild(
                m=setting.m,
                ef_construction=setting.ef_construction,
                cast=setting.cast,
                seconds=seconds,
                size_bytes=await _index_size(conn, variant),
                maintenance_work_mem=used,
            )
        )
        report.recall += await _recall(
            conn, access, workspace, variant, probes, setting, ef_searches, ks
        )


# --- Running -------------------------------------------------------------------------------------


async def _counts(conn: Conn) -> tuple[int, int]:
    records = int(await _scalar(conn, "SELECT count(*) FROM record WHERE deleted_at IS NULL"))
    chunks = int(await _scalar(conn, "SELECT count(*) FROM chunk"))
    return records, chunks


async def run_bench(
    config: BenchConfig,
    embedder: Embedder,
    *,
    server_url: str,
    sample_embedder: Callable[[int], Embedder] | None = None,
    setup: dict[str, Any] | None = None,
    keep: bool = False,
    say: Callable[[str], None] = _quiet,
) -> BenchReport:
    """Run the benchmark with ``embedder`` for indexing and queries; see the module.

    ``sample_embedder`` makes the configured embedding model at a batch size, for its throughput
    on a sample (``config.embedding_sample`` texts at each of ``config.embedding_batch_sizes``)
    and its latency on single queries. ``setup`` adds to the report's description of the run.

    Raises:
        BenchSetupError: git is missing, or the role may not create a database.
        EmbeddingError: an embedding model failed.
        psycopg.Error: the server failed.
    """
    if config.repository is not None and shutil.which("git") is None:
        raise BenchSetupError("the benchmark needs the git executable on PATH")
    started = time.perf_counter()
    report = BenchReport(
        setup={
            "memini": __version__,
            "machine": _machine(),
            "embedder": {
                "description": embedder.spec.describe(),
                "batch_size": embedder.batch_size,
            },
            "search": {
                "candidates": config.options.candidates,
                "rrf_k": config.options.rrf_k,
                "dense_weight": config.options.dense_weight,
                "timeout": config.options.timeout,
            },
            "config": {
                "repository": None if config.repository is None else config.repository.origin,
                "ref": None if config.repository is None else config.repository.ref,
                "include": list(config.include),
                "exclude": list(config.exclude),
                "threads": config.messages.threads,
                "meetings": config.messages.meetings,
                "seed": config.seed,
                "sync_batch_files": config.sync_limits.batch_files,
                "ingest_batch": config.ingest_batch,
                "queries": config.queries,
                "concurrency": list(config.concurrency),
            },
            **(setup or {}),
        }
    )
    timed = TimedEmbedder(embedder)
    sample = _Reservoir(config.embedding_sample if sample_embedder else 0, config.seed)
    work = Path(tempfile.mkdtemp(prefix="memini-bench-")).resolve()
    try:
        async with _scratch_database(server_url, keep=keep) as (name, url):
            say(f"scratch database {name}" + (" (kept)" if keep else ""))
            await migrate(url)
            async with (
                connect(url, autocommit=True) as conn,
                open_pool(url, min_size=1, max_size=config.pool_size, timeout=60) as pool,
            ):
                report.setup["postgres"] = await _postgres_setup(conn)
                owner = await ensure_principal(conn, PRINCIPAL)
                workspace = await create_workspace(conn, owner_id=owner, name=WORKSPACE)
                write = WriteAccess.for_owner(owner)
                if config.repository is not None:
                    await _index_repository(
                        conn,
                        pool,
                        timed,
                        write,
                        config,
                        config.repository,
                        work,
                        report,
                        sample,
                        say,
                    )
                if config.messages.records:
                    await _index_messages(conn, timed, write, config, report, sample, say)
                say("VACUUM ANALYZE")
                await _vacuum(conn)
                records, chunks = await _counts(conn)
                report.corpus = replace(report.corpus, records=records, chunks=chunks)
                report.sizes = await _sizes(conn)

                principal = Principal(
                    principal_id=owner, token_id=uuid.UUID(int=0), scopes=frozenset({"read"})
                )
                access = authorize_read(principal)
                queries = await build_queries(
                    conn,
                    per_mode=config.queries,
                    seed=config.seed,
                    messages=config.messages.records > 0,
                )
                if sample_embedder is not None and sample.texts:
                    say(f"embedding model: {len(sample.texts)} sample texts")
                    sizes = config.embedding_batch_sizes or (embedder.batch_size,)
                    report.embedding = await _embedding_throughput(
                        sample_embedder, sizes, sample.texts, chunks
                    )
                    one = sample_embedder(sizes[0])
                    try:
                        report.query_embedding = await _query_embedding(
                            one,
                            queries.get("hybrid", [])[:50] or [BenchQuery("x", "hybrid", "code")],
                        )
                    finally:
                        await one.aclose()
                narrow = (
                    None
                    if config.repository is None
                    else await _small_domain(conn, write, access, say)
                )
                await _search_latency(pool, timed, access, queries, config, report, say, narrow)
                ranked = queries.get("dense", [])[: config.recall_queries]
                await _dense_quality(
                    conn,
                    timed,
                    access,
                    (workspace.id, workspace.name),
                    ranked,
                    config,
                    report,
                    say,
                )
    finally:
        if keep:
            say(f"kept the mirrors in {work}")
        else:
            await anyio.to_thread.run_sync(lambda: shutil.rmtree(work, ignore_errors=True))
    report.peak_rss_bytes = peak_rss_bytes()
    report.total_seconds = time.perf_counter() - started
    if embedder.spec.model == "bench-fake":
        report.notes.append(
            "indexed with the fake embedder: vectors carry no meaning, so dense and hybrid"
            " latency is the database's alone, and recall against exact search is a floor"
            " (random directions are the hardest case for HNSW)"
        )
    if report.embedding:
        report.notes.append(
            "corpus embedding times are extrapolated from the sample's rate, not measured"
        )
    return report
