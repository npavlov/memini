"""``memini eval`` against a real Postgres, with the FakeEmbedder: the committed suite against the
committed baseline, a worse lexical retriever that must fail the comparison, and a suite that
expects what the index does not hold. Acceptance of memini-a0t. Every value is synthetic."""

import json
import shutil
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.cli import main
from memini.cli.common import EXIT_FAILURE, EXIT_OK
from memini.cli.evaluate import EXIT_REGRESSION
from memini.config import environment_variables
from memini.evaluation import DEFAULT_LEXICAL, LEXICAL_VARIANTS, LexicalVariant, runner
from memini.evaluation.runner import SCRATCH_PREFIX
from memini.search.retrieval import ChunkScope, Params, Statement, chunk_conditions

pytestmark = [
    pytest.mark.integration,
    pytest.mark.git,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "eval" / "code" / "suite.toml"
BASELINE = ROOT / "eval" / "code" / "baseline-fake.json"


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


def scratch_databases(url: str) -> set[str]:
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE starts_with(datname, %s)", (SCRATCH_PREFIX,)
        ).fetchall()
    return {row[0] for row in rows}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, postgres_url: str) -> Iterator[str]:
    """The test server in ``MEMINI_DATABASE_URL``; no other setting from the shell. Checks that
    every scratch database the test made is gone afterwards: only those it made, since an eval
    running elsewhere on the same server may have one of its own at that moment."""
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


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> Run:
    capsys.readouterr()
    code = main(["eval", *argv])
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def test_the_committed_suite_meets_the_committed_baseline(
    server: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    del server
    report = tmp_path / "report.json"
    run = invoke(
        capsys,
        "--suite",
        str(SUITE),
        "--fake-embeddings",
        "--baseline",
        str(BASELINE),
        "--json",
        str(report),
    )
    assert run.code == EXIT_OK, run.err
    assert "no tracked metric fell" in run.out
    assert "questions that changed" not in run.out  # deterministic: the same numbers exactly
    for mode in ("lexical", "exact", "regex", "symbol"):
        assert f"\n{mode} " in run.out
    assert "dense" not in json.loads(report.read_text())["modes"]
    assert "scratch database memini_eval_" in run.err


def test_a_worse_lexical_retriever_fails_the_comparison(
    server: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del server
    prepared: list[bool] = []

    async def prepare(conn: AsyncConnection[TupleRow]) -> None:
        cur = await conn.execute("SELECT count(*) FROM chunk")
        row = await cur.fetchone()
        prepared.append(row is not None and row[0] > 0)

    def nothing(scope: ChunkScope, text: str, limit: int) -> Statement:
        """The same conditions as the service's retriever, and no rows: a lexical leg broken."""
        del text
        param = Params()
        query = sql.SQL(
            "SELECT c.id, 0.0::real FROM chunk c JOIN record r ON r.id = c.record_id"
            " WHERE false AND {} LIMIT {}"
        ).format(chunk_conditions(scope, param), sql.Literal(limit))
        return Statement(query, param.values)

    broken = LexicalVariant(
        name=DEFAULT_LEXICAL, description="finds nothing", statement=nothing, prepare=prepare
    )
    monkeypatch.setitem(LEXICAL_VARIANTS, DEFAULT_LEXICAL, broken)
    run = invoke(
        capsys,
        "--suite",
        str(SUITE),
        "--fake-embeddings",
        "--modes",
        "lexical,symbol",
        "--baseline",
        str(BASELINE),
    )
    assert run.code == EXIT_REGRESSION, run.err
    assert prepared == [True]
    assert "regressions:" in run.out
    assert "lexical  all" in run.out
    regressions = run.out.split("regressions:")[1].split("questions that changed")[0]
    assert {line.split()[0] for line in regressions.strip().splitlines()} == {"lexical"}
    assert "the baseline has the mode exact, which this run did not measure" in run.out


def test_a_variant_with_a_column_of_its_own_runs_on_the_scratch_database(
    server: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """tsvector-simple, the service's retriever before open question 1, adds its column in
    prepare; it is measured below the committed baseline of the service's."""
    del server
    report = tmp_path / "report.json"
    run = invoke(
        capsys,
        "--suite",
        str(SUITE),
        "--fake-embeddings",
        "--modes",
        "lexical",
        "--lexical",
        "tsvector-simple",
        "--baseline",
        str(BASELINE),
        "--json",
        str(report),
    )
    assert run.code == EXIT_REGRESSION, run.err
    data = json.loads(report.read_text())
    assert data["setup"]["lexical"] == "tsvector-simple"
    recall = data["modes"]["lexical"]["groups"]["all"]["recall"]["10"]
    assert recall == pytest.approx(0.61, abs=0.01)


SMALL_SUITE = """
format = 1
name = "pond"
version = 1

[[repository]]
name = "pond"
workspace = "garden"
path = "repos/pond"

[[question]]
id = "depth"
text = "measure"
language = "en"
kind = "symbol"
modes = ["symbol", "lexical"]
expect = [{ repository = "pond", path = "pond/depth.py", symbol = "measure" }]

[[question]]
id = "width"
text = "width"
language = "en"
kind = "symbol"
modes = ["symbol"]
expect = [{ repository = "pond", path = "pond/depth.py", symbol = "Pond.width" }]
"""


def small_suite(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "pond" / "pond"
    repo.mkdir(parents=True)
    (repo / "depth.py").write_text(
        '"""A pond, measured; a synthetic fixture."""\n\n\n'
        "def measure(pond):\n"
        '    """How deep the pond is."""\n'
        "    return pond.depth\n"
    )
    path = tmp_path / "suite.toml"
    path.write_text(SMALL_SUITE)
    return path


def test_an_expected_result_the_index_lacks_stops_the_run(
    server: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    del server
    run = invoke(capsys, "--suite", str(small_suite(tmp_path)), "--fake-embeddings")
    assert run.code == EXIT_FAILURE
    assert "not in the index: question width: pond:pond/depth.py#Pond.width" in run.err
    assert "1 expected results of the suite are not in the index" in run.err


def test_a_baseline_written_is_met_by_the_next_run(
    server: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    del server
    suite = small_suite(tmp_path)
    suite.write_text(SMALL_SUITE.split('[[question]]\nid = "width"')[0])
    baseline = tmp_path / "baseline.json"
    run = invoke(
        capsys,
        "--suite",
        str(suite),
        "--fake-embeddings",
        "--baseline",
        str(baseline),
        "--write-baseline",
    )
    assert run.code == EXIT_OK, run.err
    written = json.loads(baseline.read_text())
    assert written["modes"]["symbol"]["questions"]["depth"]["recall"] == {
        "1": 1.0,
        "5": 1.0,
        "10": 1.0,
    }
    run = invoke(capsys, "--suite", str(suite), "--fake-embeddings", "--baseline", str(baseline))
    assert run.code == EXIT_OK, run.err
    assert "no tracked metric fell" in run.out
