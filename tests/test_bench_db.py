"""``memini bench`` end to end on a tiny repository and a few synthetic records, with the fake
embedder: the smoke run that keeps the command from rotting. The real benchmark is too slow for a
test run (docs/operations/performance.md). Every value is synthetic."""

import json
import shutil
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import pytest

from git_helpers import SourceRepo
from memini.bench import SCRATCH_PREFIX, runner
from memini.cli import main
from memini.cli.common import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from memini.config import environment_variables

pytestmark = [
    pytest.mark.integration,
    pytest.mark.git,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

MODULE = '''"""Tide tables."""


class TideTable:
    """Heights by hour."""

    def height(self, hour: int) -> float:
        return 1.5 * hour


def format_uptime(seconds: int) -> str:
    return f"{seconds // 60} min"
'''

README = """# Harbour

## Install

Run the installer.

## Tides

The `TideTable` gives heights.
"""


def scratch_databases(url: str) -> set[str]:
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE starts_with(datname, %s)", (SCRATCH_PREFIX,)
        ).fetchall()
    return {row[0] for row in rows}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, postgres_url: str) -> Iterator[str]:
    """The test server in ``MEMINI_DATABASE_URL``; no other setting from the shell. Checks that
    every scratch database the test made is gone afterwards: only those it made, since a benchmark
    running elsewhere on the same server (another test worker) may have one of its own then."""
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", postgres_url)
    created: list[str] = []
    original = runner._scratch_database  # pyright: ignore[reportPrivateUsage]

    @asynccontextmanager
    async def recording(server_url: str, *, keep: bool) -> AsyncGenerator[tuple[str, str]]:
        async with original(server_url, keep=keep) as (name, url):
            created.append(name)
            yield name, url

    monkeypatch.setattr(runner, "_scratch_database", recording)
    yield postgres_url
    assert not scratch_databases(postgres_url) & set(created)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = SourceRepo(tmp_path / "repos" / "harbour")
    repo.commit(
        {
            "harbour/tides.py": MODULE,
            "harbour/more.py": MODULE.replace("TideTable", "SwellTable"),
            "README.md": README,
        }
    )
    return repo.path


def test_a_smoke_run_measures_every_step_and_mode(
    server: str, repository: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del server
    report_path = tmp_path / "bench.json"
    code = main(
        [
            "bench",
            "--repo",
            str(repository),
            "--threads",
            "6",
            "--meetings",
            "2",
            "--fake-dimension",
            "8",
            "--queries",
            "3",
            "--concurrency",
            "1,2",
            "--candidates",
            "5",
            "--ef-search",
            "10,40",
            "--recall-queries",
            "2",
            "--hnsw",
            "4:16,4:16:halfvec",
            "--maintenance-work-mem",
            "64MB",
            "--json",
            str(report_path),
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    assert "Search latency" in captured.out
    assert "scratch database memini_bench_" in captured.err

    report = json.loads(report_path.read_text())
    corpus = report["corpus"]
    assert corpus["code_files"] == 2
    assert corpus["documents"] == 1
    assert corpus["threads"] == 6
    assert corpus["meetings"] == 2
    assert corpus["records"] == 3 + 8
    assert corpus["chunks"] == corpus["code_chunks"] + corpus["message_chunks"]
    assert [phase["name"] for phase in report["phases"]] == [
        "mirror fetch",
        "read and chunk",
        "git sync",
        "messages",
    ]
    assert all(phase["peak_rss_bytes"] > 0 for phase in report["phases"])
    timings = {(t["mode"], t["concurrency"], t["candidates"]) for t in report["search"]}
    for mode in ("hybrid", "dense", "lexical", "exact", "regex", "symbol"):
        assert (mode, 1, 50) in timings
        assert (mode, 2, 50) in timings
    assert ("hybrid", 1, 5) in timings
    assert all(t["errors"] == 0 for t in report["search"])
    # A small domain of the catalog: the ranked modes once more, narrowed to one directory.
    narrowed = [t for t in report["search"] if t["filter"]]
    assert [t["mode"] for t in narrowed] == ["hybrid", "dense", "lexical"]
    assert all(t["filter"].startswith("domain: ") for t in narrowed)
    assert "small domain: harbour" in captured.err
    assert {(r["m"], r["ef_search"], r["filtered"], r["k"]) for r in report["recall"]} >= {
        (16, 10, False, 10),
        (16, 40, True, 50),
        (4, 40, False, 10),
    }
    assert [(h["m"], h["as_built"], h["cast"]) for h in report["hnsw"]] == [
        (16, True, "vector"),
        (4, False, "vector"),
        (4, False, "halfvec"),
    ]
    assert {r["cast"] for r in report["recall"]} == {"vector", "halfvec"}
    assert report["sizes"]["database"] > 0
    assert report["peak_rss_bytes"] > 0


def test_a_repository_without_the_ref_is_refused(
    server: str, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del server
    code = main(["bench", "--repo", str(repository), "--ref", "missing", "--threads", "0"])
    assert code == EXIT_FAILURE
    assert "refs/heads/missing" in capsys.readouterr().err


def test_a_sample_of_the_model_needs_a_provider(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    del server
    code = main(["bench", "--no-repo", "--embedding-sample", "5"])
    assert code == EXIT_USAGE
    assert "MEMINI_EMBEDDING_PROVIDER" in capsys.readouterr().err
