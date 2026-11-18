"""Metrics of the service in the Prometheus text format: what the code counts, and how it is read.

The code counts events where they happen, in the counters and histograms of this module: HTTP
requests, MCP tool calls, refusals by the rate limits, runs of jobs, calls of the embedding model
and of an LLM, database statements. ``GET /metrics`` (:mod:`memini.api.metrics`) adds gauges it
reads when it is scraped (freshness, the job queue, the connection pool) and renders everything
with :func:`render`, in the text format 0.0.4 (:data:`CONTENT_TYPE`).

**Labels never carry content** (principle 11). A label value is one of a set the code knows: a
route template such as ``/v1/workspaces/{workspace}``, never the path of a request; a tool name, a
job kind, a status, a provider class. No label holds a workspace, source, stream or person name, an
id, a query or a token: metrics are often shipped to a monitoring service outside the store. Which
stream is behind is what ``memini status`` answers, with a token. As a last guard, a metric keeps
at most :data:`MAX_SERIES` label sets; a new one past that is counted under :data:`OTHER` in every
label, so a bug that put an unbounded value in a label cannot grow memory without end.

The metrics live in this process, in memory, from its start (principle 12: one process); a restart
starts the counters at zero, which Prometheus's ``rate()`` expects. This module imports nothing of
Memini, so any module can count without pulling in the HTTP application. It is written here rather
than taken from ``prometheus_client``: the format is a few lines of rules, and a registry of our
own has no global state of a library, types for strict checking, and the series limit above.
"""

import bisect
import math
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "CONTENT_TYPE",
    "DB_SLOW_STATEMENTS",
    "DB_STATEMENT_DURATION",
    "EMBEDDING_DURATION",
    "EMBEDDING_REQUESTS",
    "EMBEDDING_TEXTS",
    "HTTP_DURATION",
    "HTTP_REQUESTS",
    "LLM_DURATION",
    "LLM_REQUESTS",
    "LLM_TOKENS",
    "MAX_SERIES",
    "MCP_TOOL_CALLS",
    "MCP_TOOL_DURATION",
    "OTHER",
    "RATE_LIMIT_REFUSALS",
    "REGISTRY",
    "RUNS",
    "RUN_DURATION",
    "Counter",
    "Family",
    "Histogram",
    "Kind",
    "Registry",
    "Sample",
    "family",
    "gauge",
    "render",
]

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
"""The media type of :func:`render`'s output: the Prometheus text format."""

MAX_SERIES: Final = 1000
"""Label sets one metric keeps at most; see the module."""

OTHER: Final = "other"
"""The value of every label of a series past :data:`MAX_SERIES`."""

_NAME: Final = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL: Final = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_RESERVED_LABELS: Final = frozenset({"le", "quantile"})

Kind = Literal["counter", "gauge", "histogram"]
type Labels = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Sample:
    """One line of the output: the family's name plus ``suffix``, labels, value."""

    suffix: str
    labels: Labels
    value: float


@dataclass(frozen=True, slots=True)
class Family:
    """A metric as it is rendered: its name, type, help text and samples."""

    name: str
    kind: Kind
    help: str
    samples: tuple[Sample, ...]


def _check_names(name: str, labels: Sequence[str]) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError(f"not a metric name: {name!r}")
    for label in labels:
        if not _LABEL.fullmatch(label) or label.startswith("__") or label in _RESERVED_LABELS:
            raise ValueError(f"not a label name of {name}: {label!r}")
    if len(set(labels)) != len(labels):
        raise ValueError(f"a label of {name} is named twice")


class _Metric:
    """What counters and histograms share: their names, a lock, and the series limit."""

    kind: Kind

    def __init__(self, name: str, help: str, labels: Sequence[str]) -> None:
        _check_names(name, labels)
        self.name: Final = name
        self.help: Final = help
        self.labels: Final = tuple(labels)
        self._lock = threading.Lock()

    def _key(
        self, values: Mapping[str, str], known: Mapping[tuple[str, ...], object]
    ) -> tuple[str, ...]:
        """The series of ``values``; past :data:`MAX_SERIES` series, the one of :data:`OTHER`.
        Call it under the lock."""
        if set(values) != set(self.labels):
            raise ValueError(
                f"{self.name} takes the labels {list(self.labels)}, got {sorted(values)}"
            )
        key = tuple(str(values[label]) for label in self.labels)
        if key not in known and len(known) >= MAX_SERIES:
            return (OTHER,) * len(self.labels)
        return key

    def _labels(self, key: tuple[str, ...]) -> Labels:
        return tuple(zip(self.labels, key, strict=True))


class Counter(_Metric):
    """A count that only goes up, by label set. Name it ``…_total``."""

    kind = "counter"

    def __init__(self, name: str, help: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, /, **labels: str) -> None:
        """Add ``amount`` (0 or more) to the series of ``labels``."""
        if not amount >= 0:  # also refuses NaN
            raise ValueError("a counter only goes up")
        with self._lock:
            key = self._key(labels, self._values)
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        """The count of the series of ``labels``; 0 for one never counted."""
        key = tuple(str(labels[label]) for label in self.labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> Family:
        with self._lock:
            items = sorted(self._values.items())
        return Family(
            self.name,
            self.kind,
            self.help,
            tuple(Sample("", self._labels(key), value) for key, value in items),
        )


@dataclass(slots=True)
class _Observed:
    buckets: list[int]
    count: int = 0
    sum: float = 0.0


class Histogram(_Metric):
    """A distribution of observed values, such as durations in seconds, by label set."""

    kind = "histogram"

    def __init__(
        self, name: str, help: str, labels: Sequence[str] = (), *, buckets: Sequence[float]
    ) -> None:
        super().__init__(name, help, labels)
        bounds = [float(bound) for bound in buckets]
        if not bounds or bounds != sorted(set(bounds)) or not all(map(math.isfinite, bounds)):
            raise ValueError(f"the buckets of {name} are finite, distinct and in order")
        self.buckets: Final = tuple(bounds)
        self._series: dict[tuple[str, ...], _Observed] = {}

    def observe(self, value: float, /, **labels: str) -> None:
        """Count ``value`` in the series of ``labels``. A value that is not a number is ignored."""
        if math.isnan(value):
            return
        with self._lock:
            key = self._key(labels, self._series)
            observed = self._series.get(key)
            if observed is None:
                observed = self._series[key] = _Observed([0] * len(self.buckets))
            index = bisect.bisect_left(self.buckets, value)
            if index < len(self.buckets):
                observed.buckets[index] += 1
            observed.count += 1
            observed.sum += value

    def count(self, **labels: str) -> int:
        """How many values the series of ``labels`` observed."""
        key = tuple(str(labels[label]) for label in self.labels)
        with self._lock:
            observed = self._series.get(key)
            return 0 if observed is None else observed.count

    def collect(self) -> Family:
        with self._lock:
            items = sorted(
                (key, list(observed.buckets), observed.count, observed.sum)
                for key, observed in self._series.items()
            )
        samples: list[Sample] = []
        for key, buckets, count, total in items:
            labels = self._labels(key)
            cumulative = 0
            for bound, in_bucket in zip(self.buckets, buckets, strict=True):
                cumulative += in_bucket
                samples.append(Sample("_bucket", (*labels, ("le", _number(bound))), cumulative))
            samples.append(Sample("_bucket", (*labels, ("le", "+Inf")), count))
            samples.append(Sample("_sum", labels, total))
            samples.append(Sample("_count", labels, count))
        return Family(self.name, self.kind, self.help, tuple(samples))


class Registry:
    """The counters and histograms of the process, by name."""

    def __init__(self) -> None:
        self._metrics: dict[str, Counter | Histogram] = {}
        self._lock = threading.Lock()

    def _add[M: Counter | Histogram](self, metric: M) -> M:
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"a metric named {metric.name} exists")
            self._metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help: str, labels: Sequence[str] = ()) -> Counter:
        return self._add(Counter(name, help, labels))

    def histogram(
        self, name: str, help: str, labels: Sequence[str] = (), *, buckets: Sequence[float]
    ) -> Histogram:
        return self._add(Histogram(name, help, labels, buckets=buckets))

    def collect(self) -> list[Family]:
        with self._lock:
            metrics = list(self._metrics.values())
        return [metric.collect() for metric in metrics]


def family(
    name: str, kind: Kind, help: str, samples: Iterable[tuple[Mapping[str, str], float]] = ()
) -> Family:
    """A metric read at scrape time, such as a count of the database or a counter another library
    keeps: ``samples`` are its label sets and values. Not a histogram."""
    if kind == "histogram":
        raise ValueError("a histogram is kept with Histogram")
    rows = [(dict(labels), value) for labels, value in samples]
    _check_names(name, sorted({label for labels, _ in rows for label in labels}))
    return Family(
        name,
        kind,
        help,
        tuple(Sample("", tuple(sorted(labels.items())), value) for labels, value in rows),
    )


def gauge(name: str, help: str, samples: Iterable[tuple[Mapping[str, str], float]] = ()) -> Family:
    """A gauge read at scrape time: ``samples`` are its label sets and values."""
    return family(name, "gauge", help, samples)


# --- The text format ----------------------------------------------------------------------------


def _number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value.is_integer() and abs(value) < 2**53:
        return str(int(value))
    return repr(float(value))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def render(families: Iterable[Family]) -> str:
    """``families`` in the Prometheus text format 0.0.4, ending with a line break.

    Raises:
        ValueError: two families share a name.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for family in families:
        if family.name in seen:
            raise ValueError(f"two metrics are named {family.name}")
        seen.add(family.name)
        lines.append(f"# HELP {family.name} {_help(family.help)}")
        lines.append(f"# TYPE {family.name} {family.kind}")
        for sample in family.samples:
            labels = ",".join(f'{key}="{_escape(value)}"' for key, value in sample.labels)
            series = f"{family.name}{sample.suffix}" + (f"{{{labels}}}" if labels else "")
            lines.append(f"{series} {_number(sample.value)}")
    return "\n".join(lines) + "\n"


# --- The metrics the code counts ------------------------------------------------------------------

REGISTRY: Final = Registry()

_LATENCY: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
"""Seconds: requests, tool calls, calls of a model."""
_STATEMENT: Final = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0)
"""Seconds: one database statement."""
_RUN: Final = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, 4 * 3600.0)
"""Seconds: one run of a job."""

HTTP_REQUESTS: Final = REGISTRY.counter(
    "memini_http_requests_total",
    "HTTP requests answered, by route template, method and status code.",
    ("route", "method", "status"),
)
HTTP_DURATION: Final = REGISTRY.histogram(
    "memini_http_request_duration_seconds",
    "Time from a request's arrival to the end of its answer, by route template and method.",
    ("route", "method"),
    buckets=_LATENCY,
)
MCP_TOOL_CALLS: Final = REGISTRY.counter(
    "memini_mcp_tool_calls_total",
    "MCP tool calls, by tool and outcome: ok, error (a tool error the model sees), refused (an"
    " unknown tool, or one the token may not use; tool unknown when it does not exist).",
    ("tool", "outcome"),
)
MCP_TOOL_DURATION: Final = REGISTRY.histogram(
    "memini_mcp_tool_duration_seconds",
    "Time an MCP tool call took, by tool.",
    ("tool",),
    buckets=_LATENCY,
)
RATE_LIMIT_REFUSALS: Final = REGISTRY.counter(
    "memini_rate_limit_refusals_total",
    "Requests refused with 429 by the rate limits, by budget: read, write, unauthenticated.",
    ("budget",),
)
RUNS: Final = REGISTRY.counter(
    "memini_runs_total",
    "Runs of jobs that ended, by kind and status: succeeded, partial, failed, cancelled.",
    ("kind", "status"),
)
RUN_DURATION: Final = REGISTRY.histogram(
    "memini_run_duration_seconds",
    "Time a run of a job took, by kind.",
    ("kind",),
    buckets=_RUN,
)
EMBEDDING_REQUESTS: Final = REGISTRY.counter(
    "memini_embedding_requests_total",
    "Batches sent to the embedding model, by provider, kind of text (document, query) and"
    " outcome (ok, error).",
    ("provider", "kind", "outcome"),
)
EMBEDDING_TEXTS: Final = REGISTRY.counter(
    "memini_embedding_texts_total",
    "Texts embedded, by provider and kind of text.",
    ("provider", "kind"),
)
EMBEDDING_DURATION: Final = REGISTRY.histogram(
    "memini_embedding_request_duration_seconds",
    "Time one batch took at the embedding model, by provider.",
    ("provider",),
    buckets=_LATENCY,
)
LLM_REQUESTS: Final = REGISTRY.counter(
    "memini_llm_requests_total",
    "Requests to an LLM, by provider class (local, cloud) and outcome: ok, error (the endpoint"
    " failed or refused), output_error (a reply that does not match the schema).",
    ("provider", "outcome"),
)
LLM_TOKENS: Final = REGISTRY.counter(
    "memini_llm_tokens_total",
    "Tokens LLM endpoints reported, by provider class and direction (input, output).",
    ("provider", "direction"),
)
LLM_DURATION: Final = REGISTRY.histogram(
    "memini_llm_request_duration_seconds",
    "Time one LLM request took, retries included, by provider class.",
    ("provider",),
    buckets=_LATENCY,
)
DB_STATEMENT_DURATION: Final = REGISTRY.histogram(
    "memini_db_statement_duration_seconds",
    "Time database statements on the service's pool took, failed ones included.",
    buckets=_STATEMENT,
)
DB_SLOW_STATEMENTS: Final = REGISTRY.counter(
    "memini_db_slow_statements_total",
    "Database statements over log_slow_statement_threshold, each logged with its fingerprint.",
)
