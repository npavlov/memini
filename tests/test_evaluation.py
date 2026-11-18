"""The retrieval eval without a database: metrics, the suite loader, matching hits with expected
results, reports and baselines, and the checks ``memini eval`` makes before it builds anything.
Every value here is synthetic."""

import argparse
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memini.cli import main
from memini.cli.common import EXIT_USAGE
from memini.cli.evaluate import parse_ks, parse_modes
from memini.config import environment_variables
from memini.evaluation import (
    EvalConfig,
    Expected,
    Ranked,
    Report,
    ReportError,
    SuiteError,
    compare,
    format_comparison,
    format_report,
    load_suite,
    read_report,
    recall_at_k,
    reciprocal_rank,
    write_report,
)
from memini.evaluation.metrics import first_ranks, tie_groups
from memini.evaluation.report import GroupSummary, ModeResult, QuestionResult
from memini.evaluation.runner import Target, header_trails, hit_matches, summarize
from memini.search import Hit, SearchOptions, SymbolDefinition

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "eval" / "code" / "suite.toml"
BASELINE = ROOT / "eval" / "code" / "baseline-fake.json"


def hits(*matches: set[int], scores: list[float] | None = None) -> list[Ranked]:
    return [
        Ranked(frozenset(m), None if scores is None else scores[i]) for i, m in enumerate(matches)
    ]


# --- Metrics -------------------------------------------------------------------------------------


def test_recall_counts_the_expected_results_found_within_k() -> None:
    ranked = hits(set(), {0}, set(), {1})
    assert recall_at_k(ranked, 2, 1) == 0.0
    assert recall_at_k(ranked, 2, 2) == 0.5
    assert recall_at_k(ranked, 2, 4) == 1.0
    assert recall_at_k(ranked, 3, 10) == pytest.approx(2 / 3)


def test_a_result_matched_twice_counts_once() -> None:
    ranked = hits({0}, {0}, {0, 1})
    assert recall_at_k(ranked, 2, 2) == 0.5
    assert recall_at_k(ranked, 2, 3) == 1.0
    assert reciprocal_rank(ranked) == 1.0
    assert first_ranks(ranked, 2) == (1, 3)


def test_reciprocal_rank_is_of_the_first_hit_that_matches_anything() -> None:
    assert reciprocal_rank(hits(set(), set(), {1})) == pytest.approx(1 / 3)
    assert reciprocal_rank(hits(set(), set())) == 0.0
    assert reciprocal_rank([]) == 0.0
    assert reciprocal_rank(hits(set(), set(), {0}), depth=2) == 0.0


def test_hits_without_a_score_are_never_tied() -> None:
    ranked = hits({0}, set(), set())
    assert [len(group) for _, group in tie_groups(ranked)] == [1, 1, 1]


def test_a_tie_at_the_top_is_averaged_over_its_orders() -> None:
    # Two hits share rank 1; one of them matches. Either is first with probability 1/2.
    ranked = hits(set(), {0}, set(), scores=[0.5, 0.5, 0.1])
    assert recall_at_k(ranked, 1, 1) == pytest.approx(0.5)
    assert recall_at_k(ranked, 1, 2) == 1.0
    assert reciprocal_rank(ranked) == pytest.approx((1 + 1 / 2) / 2)


def test_the_order_search_returns_ties_in_does_not_matter() -> None:
    first = hits({0}, set(), set(), set(), scores=[0.3, 0.3, 0.3, 0.3])
    last = hits(set(), set(), set(), {0}, scores=[0.3, 0.3, 0.3, 0.3])
    for k in (1, 2, 3, 4):
        assert recall_at_k(first, 1, k) == pytest.approx(recall_at_k(last, 1, k))
        assert recall_at_k(first, 1, k) == pytest.approx(k / 4)
    assert reciprocal_rank(first) == pytest.approx(reciprocal_rank(last))
    assert reciprocal_rank(first) == pytest.approx((1 + 1 / 2 + 1 / 3 + 1 / 4) / 4)


def test_a_tie_that_straddles_k_counts_the_chance_that_a_match_falls_within() -> None:
    # Rank 1 alone; then a group of 4 tied hits at ranks 2 to 5, two of which match result 0.
    ranked = hits(set(), {0}, {0}, set(), set(), scores=[0.9, 0.4, 0.4, 0.4, 0.4])
    # k = 3: 2 of the 4 fall within; missing both matches: C(2, 2) / C(4, 2) = 1/6.
    assert recall_at_k(ranked, 1, 3) == pytest.approx(5 / 6)
    # The first match in the group is at place 1, 2 or 3 of it: 3/6, 2/6, 1/6.
    expected = (3 / 6) / 2 + (2 / 6) / 3 + (1 / 6) / 4
    assert reciprocal_rank(ranked) == pytest.approx(expected)
    # Within depth 3 only places 1 and 2 of the group count.
    assert reciprocal_rank(ranked, depth=3) == pytest.approx((3 / 6) / 2 + (2 / 6) / 3)


def test_a_tie_wholly_within_k_finds_what_it_holds() -> None:
    ranked = hits({1}, {0}, scores=[0.2, 0.2])
    assert recall_at_k(ranked, 2, 2) == 1.0
    assert recall_at_k(ranked, 2, 1) == pytest.approx(0.5)


def test_metric_arguments_are_checked() -> None:
    with pytest.raises(ValueError, match="at least one"):
        recall_at_k([], 0, 1)
    with pytest.raises(ValueError, match="k must"):
        recall_at_k([], 1, 0)


# --- The suite -----------------------------------------------------------------------------------


def test_the_committed_suite_loads() -> None:
    suite = load_suite(SUITE)
    assert len(suite.questions) >= 60
    assert {q.language for q in suite.questions} == {"en", "ru"}
    assert {q.kind for q in suite.questions} == {"code", "document", "symbol"}
    assert {r.workspace for r in suite.repositories} == {"harbor", "mill"}
    assert suite.modes() == ("hybrid", "dense", "lexical", "exact", "regex", "symbol")
    assert sum("cross-language" in q.tags for q in suite.questions) >= 10
    assert len(suite.digest) == 64
    assert load_suite(SUITE).digest == suite.digest


GOOD_QUESTION = """
[[question]]
id = "q1"
text = "How is the water measured?"
language = "en"
kind = "code"
modes = ["lexical"]
expect = [{ repository = "pond", path = "pond/depth.py", symbol = "measure" }]
"""


def write_suite(tmp_path: Path, questions: str = GOOD_QUESTION, head: str | None = None) -> Path:
    repo = tmp_path / "repos" / "pond"
    (repo / "pond").mkdir(parents=True, exist_ok=True)
    (repo / "pond" / "depth.py").write_text("def measure(pond):\n    return pond.depth\n")
    (repo / "README.md").write_text("# Pond\n\n## Depth\n\nMeasured daily.\n")
    head = head or (
        'format = 1\nname = "tiny"\nversion = 1\n\n'
        '[[repository]]\nname = "pond"\nworkspace = "garden"\npath = "repos/pond"\n'
    )
    path = tmp_path / "suite.toml"
    path.write_text(head + questions)
    return path


def test_a_small_suite_loads(tmp_path: Path) -> None:
    suite = load_suite(write_suite(tmp_path))
    (question,) = suite.questions
    assert question.expect == (Expected("pond", "pond/depth.py", symbol="measure"),)
    assert suite.repositories[0].files == ("README.md", "pond/depth.py")


def test_the_digest_covers_the_fixture_files(tmp_path: Path) -> None:
    path = write_suite(tmp_path)
    before = load_suite(path).digest
    (tmp_path / "repos" / "pond" / "README.md").write_text("# Pond\n\nChanged.\n")
    assert load_suite(path).digest != before


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (('language = "en"', 'language = "fr"'), "language must be one of en, ru"),
        (('kind = "code"', 'kind = "prose"'), "kind must be one of"),
        (('modes = ["lexical"]', 'modes = ["fuzzy"]'), "unknown mode fuzzy"),
        (('modes = ["lexical"]', "modes = []"), "at least one mode"),
        (('modes = ["lexical"]', 'modes = ["lexical", "symbol"]'), "only a question of kind"),
        (('id = "q1"', 'id = "Q 1"'), "id must be"),
        (('text = "How is the water measured?"', 'text = "  "'), "text must be a non-empty"),
        (('path = "pond/depth.py"', 'path = "pond/missing.py"'), "is not a file of repository"),
        (('repository = "pond"', 'repository = "lake"'), "no repository named lake"),
        (('symbol = "measure"', 'heading = "Depth"'), "is not a Markdown document"),
        (('symbol = "measure"', 'symbol = "measure", lines = [1, 2]'), "at most one of"),
        (('symbol = "measure"', "lines = [2, 1]"), "lines must be"),
        (('symbol = "measure"', "lines = [1, 9]"), "has 2 lines"),
        (('symbol = "measure"', 'symbol = "measure", weight = 2'), "unknown key weight"),
        (('kind = "code"', 'kind = "document"'), "expects Markdown documents"),
        (('modes = ["lexical"]', 'modes = ["lexical"]\ntags = ["Big Tag"]'), "tag 'Big Tag'"),
        (
            ('modes = ["lexical"]', 'modes = ["lexical"]\nfilters = { repositories = ["lake"] }'),
            "repositories names lake",
        ),
        (
            ('modes = ["lexical"]', 'modes = ["lexical"]\nfilters = { kinds = ["essay"] }'),
            "kinds names essay",
        ),
        (
            ('modes = ["lexical"]', 'modes = ["lexical"]\nfilters = { paths = [""] }'),
            "filters: paths must be a list of non-empty strings",
        ),
        (('modes = ["lexical"]', 'modes = ["lexical"]\nnote = "x"'), "unknown key note"),
    ],
)
def test_a_bad_question_is_refused_with_its_place(
    tmp_path: Path, change: tuple[str, str], message: str
) -> None:
    path = write_suite(tmp_path, GOOD_QUESTION.replace(*change))
    with pytest.raises(SuiteError, match="question") as caught:
        load_suite(path)
    assert message in str(caught.value)
    assert str(path) in str(caught.value)


def test_a_document_question_names_a_heading_or_lines(tmp_path: Path) -> None:
    question = GOOD_QUESTION.replace('kind = "code"', 'kind = "document"').replace(
        'path = "pond/depth.py", symbol = "measure"', 'path = "README.md", heading = "Depth"'
    )
    (q,) = load_suite(write_suite(tmp_path, question)).questions
    assert q.expect[0].heading == "Depth"
    bad = question.replace('heading = "Depth"', 'symbol = "Depth"')
    with pytest.raises(SuiteError, match="has no symbols"):
        load_suite(write_suite(tmp_path, bad))
    comma = question.replace('heading = "Depth"', 'heading = "Depth, width"')
    with pytest.raises(SuiteError, match="cannot be told apart"):
        load_suite(write_suite(tmp_path, comma))


def test_a_symbol_question_names_symbols(tmp_path: Path) -> None:
    question = GOOD_QUESTION.replace('kind = "code"', 'kind = "symbol"').replace(
        ', symbol = "measure"', ""
    )
    with pytest.raises(SuiteError, match="give symbol"):
        load_suite(write_suite(tmp_path, question))


def test_duplicates_are_refused(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="the id q1 is used twice"):
        load_suite(write_suite(tmp_path, GOOD_QUESTION * 2))
    twice = GOOD_QUESTION.replace(
        'expect = [{ repository = "pond", path = "pond/depth.py", symbol = "measure" }]',
        'expect = [{ repository = "pond", path = "pond/depth.py", symbol = "measure" },'
        ' { repository = "pond", path = "pond/depth.py", symbol = "measure" }]',
    )
    with pytest.raises(SuiteError, match="is expected twice"):
        load_suite(write_suite(tmp_path, twice))


@pytest.mark.parametrize(
    ("head", "message"),
    [
        ('format = 2\nname = "x"\nversion = 1\n', "format 2 is not supported"),
        ('name = "x"\nversion = 1\n', "format is missing"),
        ('format = 1\nname = "x"\nversion = 0\n', "version must be at least 1"),
        ('format = 1\nname = "x"\nversion = 1\nowner = "y"\n', "unknown key owner"),
        ('format = 1\nname = "x"\nversion = 1\n', "repository is missing"),
        (
            'format = 1\nname = "x"\nversion = 1\n[[repository]]\nname = "pond"\n'
            'workspace = "Garden"\npath = "repos/pond"\n',
            "workspace name is",
        ),
        (
            'format = 1\nname = "x"\nversion = 1\n[[repository]]\nname = "pond"\n'
            'workspace = "garden"\npath = "repos/lake"\n',
            "path repos/lake is not a directory",
        ),
        (
            'format = 1\nname = "x"\nversion = 1\n'
            '[[repository]]\nname = "pond"\nworkspace = "garden"\npath = "repos/pond"\n'
            '[[repository]]\nname = "pond"\nworkspace = "garden"\npath = "repos/pond"\n',
            "the name pond is used twice",
        ),
        ("format = 1\nname = \n", "not a valid TOML file"),
    ],
)
def test_a_bad_suite_is_refused(tmp_path: Path, head: str, message: str) -> None:
    with pytest.raises(SuiteError) as caught:
        load_suite(write_suite(tmp_path, GOOD_QUESTION, head=head))
    assert message in str(caught.value)


def test_a_missing_suite_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="cannot read the suite"):
        load_suite(tmp_path / "nothing.toml")


# --- Matching hits -------------------------------------------------------------------------------


def hit(
    path: str = "pond/depth.py",
    lines: tuple[int, int] = (1, 2),
    header: str | None = None,
    source: str = "pond",
    definition: SymbolDefinition | None = None,
) -> Hit:
    return Hit(
        chunk_id="0" * 64,
        record_id="1" * 64,
        workspace_id=uuid.UUID(int=1),
        workspace="garden",
        source_id=uuid.UUID(int=2),
        source=source,
        source_kind="git",
        record_kind="code_file",
        external_id=f"refs/heads/main:{path}",
        external_url=None,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        commit=None,
        chunk_kind="function",
        language="python",
        path=path,
        symbol=None,
        start_line=lines[0],
        end_line=lines[1],
        header=header or path,
        snippet="",
        truncated=False,
        definition=definition,
    )


def test_a_symbol_matches_a_hit_that_overlaps_its_lines() -> None:
    target = Target(Expected("pond", "pond/depth.py", symbol="measure"), spans=((10, 20),))
    assert hit_matches(hit(lines=(15, 30)), target)
    assert hit_matches(hit(lines=(1, 10)), target)
    assert not hit_matches(hit(lines=(21, 30)), target)
    assert not hit_matches(hit(lines=(15, 30), source="lake"), target)
    assert not hit_matches(hit(path="pond/other.py", lines=(15, 30)), target)


def test_in_symbol_mode_the_definition_found_is_matched() -> None:
    target = Target(Expected("pond", "pond/depth.py", symbol="measure"), spans=((10, 20),))
    found = SymbolDefinition(
        name="measure",
        qualified_name="measure",
        kind="function",
        parent=None,
        language="python",
        path="pond/depth.py",
        start_line=12,
        end_line=18,
    )
    assert hit_matches(hit(lines=(1, 40), definition=found), target)
    elsewhere = replace(found, start_line=30, end_line=35)
    assert not hit_matches(hit(lines=(1, 40), definition=elsewhere), target)


def test_a_heading_matches_the_end_of_a_trail() -> None:
    header = "README.md: Pond, Pond > Depth, Pond > Depth > Winter"
    assert header_trails(header, "README.md") == ("Pond", "Pond > Depth", "Pond > Depth > Winter")
    for heading, found in (("Depth", True), ("Depth > Winter", True), ("Pond", True)):
        target = Target(Expected("pond", "README.md", heading=heading))
        assert hit_matches(hit(path="README.md", header=header), target) is found
    for heading in ("Summer", "Pond > Winter", "epth"):
        target = Target(Expected("pond", "README.md", heading=heading))
        assert not hit_matches(hit(path="README.md", header=header), target)
    assert header_trails("README.md", "README.md") == ()


def test_a_file_without_a_place_matches_any_of_its_hits() -> None:
    target = Target(Expected("pond", "pond/depth.py"))
    assert hit_matches(hit(lines=(100, 120)), target)


# --- Reports and baselines -----------------------------------------------------------------------


def report(lexical_recall: float = 0.5, rr: float = 0.5, chunker: str = "c1") -> Report:
    question = QuestionResult(recall={1: lexical_recall, 5: lexical_recall}, rr=rr, ranks=(2, None))
    groups = {
        "all": GroupSummary(n=1, recall={1: lexical_recall, 5: lexical_recall}, mrr=rr),
        "language:ru": GroupSummary(n=1, recall={1: lexical_recall, 5: lexical_recall}, mrr=rr),
    }
    return Report(
        setup={
            "suite": {"name": "tiny", "version": 1, "digest": "d", "questions": 1},
            "embedding": {"model": "fake"},
            "chunker": chunker,
            "lexical": "tsvector-simple",
            "reranker": "none",
            "search": {"candidates": 50},
        },
        ks=(1, 5),
        depth=50,
        modes={"lexical": ModeResult(groups=groups, questions={"q1": question})},
    )


def test_a_report_survives_json(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    write_report(path, report())
    again = read_report(path)
    assert again.to_json() == report().to_json()
    assert path.read_text().endswith("\n")
    assert json.loads(path.read_text())["modes"]["lexical"]["questions"]["q1"]["ranks"] == [2, None]


def test_the_committed_baseline_is_a_report() -> None:
    baseline = read_report(BASELINE)
    assert baseline.setup["embedding"]["fake"] is True
    assert set(baseline.modes) == {"lexical", "exact", "regex", "symbol"}
    assert baseline.setup["suite"]["digest"] == load_suite(SUITE).digest


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "not JSON"),
        ("[]", "not a report"),
        ('{"format": 9}', "format 9"),
        (
            '{"format": 1, "setup": {}, "k": [1], "depth": 5, "modes": {"fuzzy": {}}}',
            "unknown mode",
        ),
    ],
)
def test_a_bad_baseline_is_refused(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(content)
    with pytest.raises(ReportError, match=message):
        read_report(path)


def test_a_drop_beyond_the_margin_is_a_regression() -> None:
    comparison = compare(report(0.45), report(0.5), margin=0.02)
    assert {(c.group, c.metric) for c in comparison.regressions} == {
        ("all", "R@1"),
        ("all", "R@5"),
        ("language:ru", "R@1"),
        ("language:ru", "R@5"),
    }
    assert [(q.question, q.metric) for q in comparison.questions] == [("q1", "R@5")]
    text = format_comparison(comparison, "base.json")
    assert "regressions:" in text
    assert "0.50  ->  0.45  (-0.05)" in text


def test_a_drop_within_the_margin_is_not() -> None:
    comparison = compare(report(0.49), report(0.5), margin=0.02)
    assert comparison.regressions == ()
    assert "no tracked metric fell" in format_comparison(comparison, "base.json")


def test_a_gain_beyond_the_margin_is_an_improvement() -> None:
    comparison = compare(report(0.6, rr=0.7), report(0.5), margin=0.02)
    assert comparison.regressions == ()
    assert {c.metric for c in comparison.improvements} == {"R@1", "R@5", "MRR"}
    assert "--write-baseline" in format_comparison(comparison, "base.json")


def test_a_different_setup_is_noted_not_refused() -> None:
    comparison = compare(report(chunker="c2"), report(chunker="c1"), margin=0.02)
    assert comparison.notes == ("the baseline was measured with another chunker",)
    assert comparison.regressions == ()


def test_modes_and_ks_the_run_lacks_are_noted() -> None:
    base = report()
    current = replace(report(), ks=(1,), modes={})
    comparison = compare(current, base, margin=0.02)
    assert any("has k [1, 5]" in note for note in comparison.notes)
    assert any("MRR is not compared" in note for note in comparison.notes)
    assert any("mode lexical" in note for note in comparison.notes)


def test_the_table_has_a_row_per_mode_and_group() -> None:
    text = format_report(replace(report(), notes=("something to know",)))
    assert "note: something to know" in text
    rows = [line.split() for line in text.splitlines() if "language:ru" in line or "all " in line]
    assert ["lexical", "all", "1", "0.50", "0.50", "0.50"] in rows
    assert ["language:ru", "1", "0.50", "0.50", "0.50"] in rows


def test_groups_are_means_over_their_questions() -> None:
    suite = load_suite(SUITE)
    questions = suite.questions[:2]
    results = {
        questions[0].id: QuestionResult(recall={1: 1.0}, rr=1.0, ranks=(1,)),
        questions[1].id: QuestionResult(recall={1: 0.0}, rr=0.25, ranks=(None,)),
    }
    groups = summarize(questions, results, (1,))
    assert groups["all"] == GroupSummary(n=2, recall={1: 0.5}, mrr=0.625)
    assert "kind:document" not in groups


def test_the_config_checks_its_values() -> None:
    config = EvalConfig(modes=("lexical",), ks=(1, 5, 10), options=SearchOptions(candidates=30))
    assert config.depth == 30
    assert EvalConfig(modes=("lexical",), ks=(1, 80)).depth == 80
    for ks in ((), (5, 1), (0, 1), (1, 101)):
        with pytest.raises(ValueError, match="k"):
            EvalConfig(modes=("lexical",), ks=ks)
    with pytest.raises(ValueError, match="mode"):
        EvalConfig(modes=())


# --- The command, before it builds anything ------------------------------------------------------


def test_k_and_modes_are_parsed() -> None:
    assert parse_ks("10,1,5,5") == (1, 5, 10)
    assert parse_modes("symbol,lexical") == ("lexical", "symbol")
    assert parse_modes("all") == ("hybrid", "dense", "lexical", "exact", "regex", "symbol")
    for bad in ("", "a", "0", "101"):
        with pytest.raises(argparse.ArgumentTypeError, match="k"):
            parse_ks(bad)
    with pytest.raises(argparse.ArgumentTypeError, match="modes"):
        parse_modes("lexical,fuzzy")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", "postgresql://fake:fake@db.invalid:5432/memini")
    return monkeypatch


def test_the_command_refuses_bad_input_before_it_connects(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    del clean_env

    def run(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        code = main(["eval", *argv])
        return code, capsys.readouterr().err

    code, err = run("--suite", str(SUITE), "--fake-embeddings", "--write-baseline")
    assert (code, "--write-baseline needs --baseline" in err) == (EXIT_USAGE, True)

    bad_suite = tmp_path / "bad.toml"
    bad_suite.write_text("format = 1\n")
    code, err = run("--suite", str(bad_suite), "--fake-embeddings")
    assert (code, "invalid suite" in err) == (EXIT_USAGE, True)

    bad_baseline = tmp_path / "baseline.json"
    bad_baseline.write_text("[]")
    code, err = run("--suite", str(SUITE), "--fake-embeddings", "--baseline", str(bad_baseline))
    assert (code, "invalid baseline" in err) == (EXIT_USAGE, True)

    code, err = run("--suite", str(SUITE))
    assert (code, "MEMINI_EMBEDDING_PROVIDER" in err) == (EXIT_USAGE, True)

    code, err = run("--suite", str(SUITE), "--fake-embeddings", "--k", "0")
    assert code == EXIT_USAGE

    code, err = run("--suite", str(SUITE), "--lexical", "nothing", "--fake-embeddings")
    assert (code, "invalid choice" in err) == (EXIT_USAGE, True)
