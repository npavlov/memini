"""The performance benchmark: indexing time, embedding throughput, sizes, HNSW recall and search
latency on a repository and a synthetic message set (``memini bench``).

- :mod:`memini.bench.repository`: the repository, a local path or an https URL cloned once.
- :mod:`memini.bench.corpus`: the synthetic threads and meetings.
- :mod:`memini.bench.queries`: the queries timed, made from what the index holds.
- :mod:`memini.bench.measure`: latency summaries, peak memory, the timed and fake embedders.
- :mod:`memini.bench.runner`: :func:`run_bench`, the steps in order.
- :mod:`memini.bench.report`: the report as text and JSON.

The numbers of the default run, the targets and the tuning knobs are in
docs/operations/performance.md.
"""

from memini.bench.corpus import MessageSet, message_batches
from memini.bench.measure import BenchFakeEmbedder, Latency, TimedEmbedder, percentile
from memini.bench.queries import BenchQuery, build_queries, identifier_words
from memini.bench.report import BenchReport, format_report, write_report
from memini.bench.repository import (
    DEFAULT_REF,
    DEFAULT_REPOSITORY,
    PreparedRepository,
    RepositoryError,
    default_cache_dir,
    prepare_repository,
)
from memini.bench.runner import (
    SCRATCH_PREFIX,
    BenchConfig,
    BenchSetupError,
    HnswSetting,
    run_bench,
)

__all__ = [
    "DEFAULT_REF",
    "DEFAULT_REPOSITORY",
    "SCRATCH_PREFIX",
    "BenchConfig",
    "BenchFakeEmbedder",
    "BenchQuery",
    "BenchReport",
    "BenchSetupError",
    "HnswSetting",
    "Latency",
    "MessageSet",
    "PreparedRepository",
    "RepositoryError",
    "TimedEmbedder",
    "build_queries",
    "default_cache_dir",
    "format_report",
    "identifier_words",
    "message_batches",
    "percentile",
    "prepare_repository",
    "run_bench",
    "write_report",
]
