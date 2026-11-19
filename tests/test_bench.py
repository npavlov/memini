"""The benchmark's parts that need no database: statistics, the synthetic message set, queries made
from identifiers, argument parsing, the report. Every value is synthetic."""

import argparse
import json
import math
from pathlib import Path

import pytest

from memini.bench import (
    BenchFakeEmbedder,
    BenchReport,
    Latency,
    MessageSet,
    RepositoryError,
    format_report,
    identifier_words,
    message_batches,
    percentile,
    prepare_repository,
    write_report,
)
from memini.bench.corpus import person_names
from memini.bench.report import Corpus, DenseRecall, Phase, SearchTiming, Sizes
from memini.bench.repository import default_cache_dir, full_ref
from memini.cli import build_parser
from memini.cli.bench import parse_counts, parse_hnsw
from memini.ingest import DEFAULT_LIMITS, Batch, prepare_batch
from memini.payloads import parse_payload


def test_percentiles_interpolate_between_ranks() -> None:
    values = [0.004, 0.001, 0.003, 0.002]
    assert percentile(values, 0.0) == 0.001
    assert percentile(values, 1.0) == 0.004
    assert percentile(values, 0.5) == pytest.approx(0.0025)
    assert percentile([], 0.5) == 0.0
    latency = Latency.of(values)
    assert latency.count == 4
    assert latency.p50 == pytest.approx(2.5)
    assert latency.max == pytest.approx(4.0)
    assert latency.mean == pytest.approx(2.5)
    assert Latency.of([]).count == 0


def test_the_message_set_is_the_same_for_a_seed_and_valid_for_ingest() -> None:
    spec = MessageSet(threads=7, meetings=3, seed=5)
    first = [record for batch in message_batches(spec, batch_size=4) for record in batch]
    again = [record for batch in message_batches(spec, batch_size=10) for record in batch]
    assert first == again
    assert [record.kind for record in first] == ["thread"] * 7 + ["meeting"] * 3
    assert len({record.external_id for record in first}) == 10
    prepared = prepare_batch(Batch(records=first), DEFAULT_LIMITS)
    assert len(prepared.records) == 10
    for record in first:
        assert record.payload is not None
        parse_payload(record.kind, record.payload)
        assert record.external_url is not None
        assert record.external_url.startswith("https://")
        assert ".example.test/" in record.external_url
    other = [
        record
        for batch in message_batches(MessageSet(7, 3, seed=6), batch_size=4)
        for record in batch
    ]
    assert other != first


def test_the_message_set_names_invented_people_at_reserved_addresses() -> None:
    spec = MessageSet(threads=20, meetings=5)
    for batch in message_batches(spec, batch_size=50):
        for record in batch:
            assert record.payload is not None
            for person in record.payload["people"]:
                assert person["email"].endswith("@example.test")
    import random

    names = person_names(random.Random(1), 50)  # noqa: S311 - a test
    assert len(set(names)) == 50


def test_message_batches_refuse_bad_arguments() -> None:
    with pytest.raises(ValueError, match="at least one record"):
        list(message_batches(MessageSet(1, 1), batch_size=0))
    with pytest.raises(ValueError, match="0 or more"):
        MessageSet(threads=-1, meetings=0)


def test_identifiers_become_words() -> None:
    assert identifier_words("get_absolute_url") == "get absolute url"
    assert identifier_words("formatUptime") == "format uptime"
    assert identifier_words("_private__name") == "private name"


@pytest.mark.anyio
async def test_the_fake_embedder_gives_unit_vectors_of_its_dimension() -> None:
    embedder = BenchFakeEmbedder(16, batch_size=2)
    vectors = await embedder.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert all(len(vector) == 16 for vector in vectors)
    assert all(
        math.isclose(math.fsum(v * v for v in vector), 1.0, rel_tol=1e-5) for vector in vectors
    )
    assert await embedder.embed(["a"]) == vectors[:1]
    assert embedder.spec.model == "bench-fake"


def test_arguments_parse() -> None:
    assert parse_counts("1,8") == (1, 8)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_counts("0")
    settings = parse_hnsw("16:64,24:128")
    assert [(s.m, s.ef_construction) for s in settings] == [(16, 64), (24, 128)]
    with pytest.raises(argparse.ArgumentTypeError):
        parse_hnsw("16")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_hnsw("16:8")  # ef_construction below twice m
    args = build_parser().parse_args(["bench", "--no-repo", "--threads", "3", "--json", "-"])
    assert args.no_repo
    assert args.threads == 3
    assert str(args.json) == "-"


def test_a_repository_is_a_path_or_an_https_url(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="https"):
        prepare_repository("ssh://git.example.test/repo.git", "main", cache_dir=tmp_path)
    with pytest.raises(RepositoryError, match="user name"):
        prepare_repository("https://user:secret@git.example.test/r.git", "main", cache_dir=tmp_path)
    assert full_ref("main") == "refs/heads/main"
    assert full_ref("refs/tags/v1") == "refs/tags/v1"
    assert default_cache_dir({"XDG_CACHE_HOME": "/var/cache/x"}) == Path(
        "/var/cache/x/memini/bench"
    )
    assert default_cache_dir({"XDG_CACHE_HOME": "relative"}).parts[-3:] == (
        ".cache",
        "memini",
        "bench",
    )


def test_the_report_formats_and_round_trips_as_json(tmp_path: Path) -> None:
    latency = Latency.of([0.010, 0.020, 0.030])
    report = BenchReport(
        setup={"machine": {"cpus": 4}, "config": {"threads": 2}},
        corpus=Corpus(repository="/r", ref="refs/heads/main", commit="c" * 40, chunks=12),
        phases=[Phase(name="git sync", seconds=2.0, embed_seconds=0.5, items=4, chunks=12)],
        sizes=Sizes(
            database=1 << 20,
            chunk_total=1 << 19,
            chunk_heap=1 << 18,
            indexes={"chunk_pkey": 1 << 12},
            record_total=1 << 12,
            symbol_total=1 << 12,
        ),
        recall=[
            DenseRecall(
                m=16,
                ef_construction=64,
                ef_search=50,
                filtered=True,
                queries=3,
                k=10,
                recall=0.9,
                latency=latency,
            )
        ],
        search=[
            SearchTiming(
                mode="hybrid",
                concurrency=1,
                latency=latency,
                queries_per_second=50.0,
                errors=0,
                candidates=50,
            )
        ],
        peak_rss_bytes=200 << 20,
        notes=["a note"],
    )
    text = format_report(report)
    for part in ("git sync", "hybrid", "messages", "0.900", "chunk_pkey", "200.0 MiB", "a note"):
        assert part in text
    path = tmp_path / "report.json"
    write_report(path, report)
    data = json.loads(path.read_text())
    assert data["search"][0]["latency"]["p50"] == pytest.approx(20.0)
    assert data["corpus"]["chunks"] == 12
