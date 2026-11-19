"""``memini bench``: the performance benchmark, one command (:mod:`memini.bench`).

An operator command, as ``eval`` is: it builds a scratch database on the configured Postgres
server (the role needs CREATEDB), syncs a repository and a synthetic message set into it through
the service's own code, times indexing and every search mode, and drops it again. It never reads
or writes the configured database itself. The text report goes to stdout, progress to stderr,
``--json PATH`` writes the report as JSON. docs/operations/performance.md has the numbers of the
default run and what they mean.

The index is built with a fake embedder of the default model's dimension unless ``--embedder
configured`` asks for the configured one: the real model takes most of an hour on a corpus of this
size, and nothing else depends on what the vectors mean. ``--embedding-sample N`` measures the
configured model apart, on N of the corpus's chunk texts, and extrapolates.

Exit codes (besides those of every command): 0 done; 1 the repository cannot be used, a clone
failed or the role may not create a database; 2 wrong arguments, or ``--embedder configured`` or
``--embedding-sample`` without an embedding provider.
"""

import argparse
from pathlib import Path
from typing import Final

from memini.bench import (
    DEFAULT_REF,
    DEFAULT_REPOSITORY,
    BenchConfig,
    BenchFakeEmbedder,
    BenchSetupError,
    HnswSetting,
    MessageSet,
    RepositoryError,
    default_cache_dir,
    format_report,
    prepare_repository,
    run_bench,
    write_report,
)
from memini.cli.common import EXIT_OK, EXIT_USAGE, CommandError, DatabaseUrl, message
from memini.config import Settings
from memini.embeddings import Embedder, create_embedder
from memini.gitsync import SyncLimits
from memini.search import SearchOptions

__all__ = ["DESCRIPTION", "HELP", "bench_command", "configure_parser"]

HELP: Final = "benchmark indexing and search on a repository and synthetic messages"
DESCRIPTION: Final = (
    "Build a scratch database on the configured Postgres server (created and dropped by the"
    " command; the role needs CREATEDB), sync a repository and a synthetic message set into it"
    " through the service's code, and report indexing time, embedding throughput, database and"
    " index sizes, HNSW recall against exact search, search latency of every mode at each"
    " concurrency, and the process's peak memory. An https repository is cloned once, shallow,"
    " into the cache directory. Indexes with a fake embedder of dimension 1024 unless --embedder"
    " configured. See docs/operations/performance.md."
)

DEFAULT_THREADS: Final = 5000
DEFAULT_MEETINGS: Final = 1000
DEFAULT_FAKE_DIMENSION: Final = 1024
"""The dimension of the default model (Qwen3-Embedding 0.6B)."""


def _ints(value: str, *, low: int = 1, high: int = 100_000) -> tuple[int, ...]:
    try:
        numbers = tuple(int(part) for part in value.split(",") if part.strip())
    except ValueError:
        raise argparse.ArgumentTypeError("a comma-separated list of numbers, e.g. 1,8") from None
    if not numbers or any(not low <= n <= high for n in numbers):
        raise argparse.ArgumentTypeError(f"each number is from {low} to {high}")
    return numbers


def parse_counts(value: str) -> tuple[int, ...]:
    return _ints(value)


def parse_ef_search(value: str) -> tuple[int, ...]:
    return _ints(value, low=1, high=1000)


def parse_hnsw(value: str) -> tuple[HnswSetting, ...]:
    """``16:64,24:128,16:64:halfvec``: m and ef_construction, and ``halfvec`` for an index on a
    cast to half precision."""
    settings: list[HnswSetting] = []
    for part in value.split(","):
        if not part.strip():
            continue
        fields = part.strip().split(":")
        halfvec = len(fields) == 3 and fields[2] == "halfvec"
        try:
            if len(fields) not in (2, 3) or (len(fields) == 3 and not halfvec):
                raise ValueError("two numbers, and optionally halfvec")
            settings.append(HnswSetting(int(fields[0]), int(fields[1]), halfvec=halfvec))
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"an HNSW setting is m:ef_construction[:halfvec], e.g. 16:64 ({error})"
            ) from None
    if not settings:
        raise argparse.ArgumentTypeError("give at least one m:ef_construction")
    return tuple(settings)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Add the arguments of ``memini bench`` to its parser."""
    corpus = parser.add_argument_group("corpus")
    corpus.add_argument(
        "--repo",
        metavar="PATH_OR_URL",
        default=DEFAULT_REPOSITORY,
        help=f"a local git repository or an https URL (default: {DEFAULT_REPOSITORY})",
    )
    corpus.add_argument(
        "--ref",
        metavar="REF",
        help=f"the branch or tag to sync (default: {DEFAULT_REF} for the default repository,"
        " main otherwise)",
    )
    corpus.add_argument(
        "--no-repo", action="store_true", help="no repository: the message set alone"
    )
    corpus.add_argument(
        "--include", metavar="GLOB", action="append", default=[], help="the source's include globs"
    )
    corpus.add_argument(
        "--exclude", metavar="GLOB", action="append", default=[], help="the source's exclude globs"
    )
    corpus.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"synthetic threads (default: {DEFAULT_THREADS})",
    )
    corpus.add_argument(
        "--meetings",
        type=int,
        default=DEFAULT_MEETINGS,
        help=f"synthetic meetings (default: {DEFAULT_MEETINGS})",
    )
    corpus.add_argument("--seed", type=int, default=1, help="of the message set and the queries")
    corpus.add_argument(
        "--cache-dir",
        metavar="DIR",
        type=Path,
        help="where an https repository is cloned (default: ~/.cache/memini/bench)",
    )

    indexing = parser.add_argument_group("indexing")
    indexing.add_argument(
        "--embedder",
        choices=("fake", "configured"),
        default="fake",
        help="index with a fake embedder (default) or the configured embedding provider",
    )
    indexing.add_argument(
        "--fake-dimension",
        type=int,
        default=DEFAULT_FAKE_DIMENSION,
        help=f"dimension of the fake embedder (default: {DEFAULT_FAKE_DIMENSION})",
    )
    indexing.add_argument(
        "--embedding-sample",
        type=int,
        default=0,
        metavar="N",
        help="also time the configured embedding model on N of the corpus's chunk texts and on"
        " single queries (default: 0, not timed)",
    )
    indexing.add_argument(
        "--embedding-batch-sizes",
        type=parse_counts,
        metavar="N,...",
        help="batch sizes for --embedding-sample (default: MEMINI_EMBEDDING_BATCH_SIZE)",
    )
    indexing.add_argument(
        "--sync-batch-files",
        type=int,
        default=SyncLimits().batch_files,
        help=f"files per transaction of the git sync (default: {SyncLimits().batch_files})",
    )
    indexing.add_argument(
        "--ingest-batch",
        type=int,
        default=200,
        help="records per ingest batch (default: 200)",
    )

    measuring = parser.add_argument_group("search")
    measuring.add_argument(
        "--queries", type=int, default=200, help="queries per mode (default: 200)"
    )
    measuring.add_argument(
        "--concurrency",
        type=parse_counts,
        default=(1, 8),
        metavar="N,...",
        help="concurrent searches (default: 1,8)",
    )
    measuring.add_argument(
        "--candidates",
        type=parse_counts,
        default=(),
        metavar="N,...",
        help="also time hybrid search with these search_candidates values",
    )
    measuring.add_argument(
        "--ef-search",
        type=parse_ef_search,
        default=(),
        metavar="N,...",
        help="hnsw.ef_search values for the recall of the dense statement (default: the"
        " service's, max(search_candidates, 10))",
    )
    measuring.add_argument(
        "--recall-queries",
        type=int,
        default=100,
        help="queries for the recall against exact search (default: 100; 0: none)",
    )
    measuring.add_argument(
        "--hnsw",
        type=parse_hnsw,
        default=(),
        metavar="M:EFC[:halfvec],...",
        help="also build the HNSW index with these settings and measure each; :halfvec builds it"
        " on a cast to halfvec, which the service does not do",
    )
    measuring.add_argument(
        "--maintenance-work-mem",
        default="1GB",
        help="maintenance_work_mem for the builds of --hnsw (default: 1GB)",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--json",
        metavar="PATH",
        type=Path,
        help="also write the report as JSON to PATH (- for stdout, instead of the table)",
    )
    output.add_argument(
        "--keep",
        action="store_true",
        help="keep the scratch database and the mirrors, for inspection",
    )
    parser.set_defaults(command=bench_command)


async def bench_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.threads < 0 or args.meetings < 0 or args.queries < 1 or args.fake_dimension < 1:
        raise CommandError("counts and the dimension are positive", EXIT_USAGE)
    configured = settings.embedding_provider is not None
    if (args.embedder == "configured" or args.embedding_sample > 0) and not configured:
        raise CommandError(
            "--embedder configured and --embedding-sample need an embedding provider: set"
            " MEMINI_EMBEDDING_PROVIDER",
            EXIT_USAGE,
        )
    repository = None
    if not args.no_repo:
        ref = args.ref or (DEFAULT_REF if args.repo == DEFAULT_REPOSITORY else "main")
        cache = args.cache_dir or default_cache_dir()
        message(f"preparing {args.repo} {ref}")
        try:
            repository = prepare_repository(args.repo, ref, cache_dir=cache)
        except RepositoryError as error:
            raise CommandError(str(error)) from None
        if repository.cloned:
            message(f"cloned into {repository.location}")
    config = BenchConfig(
        repository=repository,
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        messages=MessageSet(threads=args.threads, meetings=args.meetings, seed=args.seed),
        ingest_batch=args.ingest_batch,
        sync_limits=SyncLimits(batch_files=args.sync_batch_files),
        options=SearchOptions.from_settings(settings),
        queries=args.queries,
        concurrency=args.concurrency,
        candidates=args.candidates,
        ef_search=args.ef_search,
        recall_queries=args.recall_queries,
        hnsw=args.hnsw,
        maintenance_work_mem=args.maintenance_work_mem,
        embedding_sample=args.embedding_sample,
        embedding_batch_sizes=args.embedding_batch_sizes or (settings.embedding_batch_size,),
        seed=args.seed,
    )

    def sample_embedder(batch_size: int) -> Embedder:
        return create_embedder(settings.model_copy(update={"embedding_batch_size": batch_size}))

    embedder: Embedder = (
        create_embedder(settings)
        if args.embedder == "configured"
        else BenchFakeEmbedder(args.fake_dimension, batch_size=settings.embedding_batch_size)
    )
    message(
        f"benchmark with {embedder.spec.describe()} on the server at {DatabaseUrl(settings).target}"
    )
    try:
        report = await run_bench(
            config,
            embedder,
            server_url=settings.database_url.get_secret_value(),
            sample_embedder=sample_embedder if args.embedding_sample > 0 else None,
            keep=args.keep,
            say=message,
        )
    except BenchSetupError as error:
        raise CommandError(str(error)) from None
    finally:
        await embedder.aclose()
    to_stdout = args.json is not None and str(args.json) == "-"
    if not to_stdout:
        print(format_report(report))
    if args.json is not None:
        write_report(args.json, report)
        if not to_stdout:
            message(f"wrote the report to {args.json}")
    return EXIT_OK
