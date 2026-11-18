"""The decision eval without a database: the sets and their labels, the extractor's input,
matching, the metrics on the oracle stub and on broken copies of it, reports and baselines, and
``memini eval decisions``. Acceptance of memini-f9a: the command runs on a stub extractor and
prints the three metrics. Every value here is synthetic."""

import asyncio
import json
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest

from comms_fixtures import final_records, names_of, people, person_of
from memini.cli import build_parser, evaluate, evaluate_decisions, main
from memini.cli.common import EXIT_OK, EXIT_USAGE
from memini.cli.evaluate import EXIT_REGRESSION
from memini.config import environment_variables
from memini.evaluation.decisions import (
    EXTRACTORS,
    DecisionEvalError,
    DecisionReport,
    DecisionReportError,
    DecisionSet,
    DecisionSetError,
    Pipeline,
    VariantContext,
    compare,
    format_comparison,
    format_report,
    load_set,
    load_sets,
    read_report,
    run_decision_eval,
    similarity,
    write_report,
)
from memini.evaluation.decisions.matching import Target, match_record, terms
from memini.evaluation.decisions.report import ExtractionSummary
from memini.evaluation.decisions.stubs import KeywordStub, OracleStub
from memini.extraction import (
    Candidate,
    ProposedItem,
    RecordInput,
    RecordRef,
    Supersedes,
    Verdict,
    record_input,
)
from memini.ingest import stored_form
from memini.payloads import normalize

type Json = dict[str, Any]

ROOT = Path(__file__).resolve().parents[1]
COMMS = ROOT / "eval" / "comms"
CASES = ROOT / "eval" / "decisions" / "cases"
BASELINE = ROOT / "eval" / "decisions" / "baseline-keyword.json"
SETS = (COMMS, CASES)

WHEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@cache
def sets() -> tuple[DecisionSet, ...]:
    return load_sets(SETS)


def the_set(name: str) -> DecisionSet:
    return next(each for each in sets() if each.name == name)


def run(pipeline: Pipeline, data: Sequence[DecisionSet] | None = None) -> DecisionReport:
    return asyncio.run(run_decision_eval(data or sets(), pipeline, variant="test"))


def raw_labels(root: Path) -> Json:
    return cast(Json, json.loads((root / "labels" / "decisions.json").read_text("utf-8")))


# --- The sets ------------------------------------------------------------------------------------


def test_the_sets_cover_reversals_duplicates_answers_and_chatter_in_both_languages() -> None:
    every = sets()
    assert [each.name for each in every] == ["comms", "cases"]
    assert len({each.workspace for each in every}) == 2
    items = [item for each in every for item in each.items]
    assert {item.kind for item in items} == {"decision", "open_question"}
    for language in ("en", "ru"):
        assert {i.kind for i in items if i.language == language} == {"decision", "open_question"}

    def languages_of(relation: str) -> set[str]:
        return {
            each.item(link.new).language
            for each in every
            for link in each.links
            if link.relation == relation
        }

    assert languages_of("replaces") == {"en", "ru"}
    assert languages_of("answers") == {"en", "ru"}
    restated_across = [
        r
        for each in every
        for r in each.restatements
        if each.languages[r.ref] != each.item(r.item).language
    ]
    assert restated_across, "a restatement in the other language than its item"
    assert all(each.traps for each in every)
    chatter = [entry for each in every for entry in each.no_items]
    assert {the_set("comms").languages[e.ref] for e in the_set("comms").no_items} == {"en", "ru"}
    assert {the_set("cases").languages[e.ref] for e in the_set("cases").no_items} == {"en", "ru"}
    assert len(chatter) >= 15

    cases = cast(Json, raw_labels(CASES)["cases"])
    assert {
        "implicit_reversal",
        "implicit_reversal_ru",
        "decision_as_a_question",
        "answered_question_ru",
        "proposal_dropped",
        "hedged_proposals",
        "cross_language_duplicate",
    } <= set(cases)
    cases_set = the_set("cases")
    for key in ("implicit_reversal", "implicit_reversal_ru"):
        link = next(link for link in cases_set.links if link.new == cases[key]["by"])
        assert (link.old, link.relation) == (cases[key]["replaced"], "replaces")


ADDRESS = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
LINK = re.compile(r"https?://([\w.-]+)")


def test_the_hard_cases_are_synthetic_and_spoken_by_the_cast() -> None:
    marker = (CASES / "SYNTHETIC.md").read_text(encoding="utf-8")
    assert marker.startswith("# SYNTHETIC DATA\n")
    assert raw_labels(CASES)["labels"] is True
    domains: set[str] = set()
    for path in CASES.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            domains |= set(ADDRESS.findall(text)) | set(LINK.findall(text))
    assert domains
    assert {d for d in domains if not d.endswith((".example.test", ".example.org"))} == set()

    for path in sorted((CASES / "batches").rglob("*.json")):
        body = cast(Json, json.loads(path.read_text("utf-8")))
        for item in cast("list[Json]", body["records"]):
            for name in names_of(item["kind"], item["payload"]):
                assert person_of(name) is not None, f"{path.name}: {name!r} is nobody"
    keys = {str(p["key"]) for p in cast("list[Json]", people()["people"])}
    for item in the_set("cases").items:
        assert set(item.participants) <= keys, item.id


def test_the_comms_set_holds_the_live_threads_and_meetings_of_the_timeline() -> None:
    expected = {
        tuple(ref)
        for ref, record in final_records().items()
        if ref.kind in ("thread", "meeting") and not record.deleted
    }
    held = {(r.ref.source, r.ref.kind, r.ref.external_id) for r in the_set("comms").records}
    assert held == expected
    edited = the_set("comms").record(RecordRef("chat", "thread", "C0BEACON01/1789466700.000100"))
    assert "port 7701" in edited.text  # the later version of an edited message


def test_an_item_is_said_at_its_first_anchor() -> None:
    for each in sets():
        for item in each.items:
            first = item.evidence[0]
            part = each.record(first.ref).part(first.anchor)
            assert part is not None
            assert part.at == item.decided_at, item.id


def test_the_extractor_reads_the_text_ingest_stores() -> None:
    cases = the_set("cases")
    for path in sorted((CASES / "batches").rglob("*.json")):
        body = cast(Json, json.loads(path.read_text("utf-8")))
        source = path.parent.name
        for item in cast("list[Json]", body["records"]):
            record = cases.record(RecordRef(source, item["kind"], item["external_id"]))
            payload = cast(Json, item["payload"])
            assert record.text == normalize(item["kind"], payload)
            assert record.content_hash == stored_form(item["kind"], payload)[1]
            lines = record.text.split("\n")
            for part in record.parts:
                first = part.text.splitlines()[0]
                held = lines[part.rows.first : part.rows.last + 1]
                assert any(first in line for line in held[:2]), (record.ref, part.anchor)
                assert 0 <= part.rows.first <= part.rows.last < len(lines)


def test_a_record_input_names_its_parts() -> None:
    thread = record_input(
        workspace="harbor",
        ref=RecordRef("chat", "thread", "C0TEST/1"),
        occurred_at=WHEN,
        payload={
            "channel": "beacon",
            "messages": [
                {"id": "m1", "author": "U03LEV", "sent_at": "2026-10-01T09:00:00Z", "text": "Hi"},
                {"id": "m2", "author": "U01MIRA", "sent_at": "2026-10-01T09:05:00Z", "text": "Ok"},
            ],
        },
    )
    assert [(p.anchor, p.kind, p.who, p.text) for p in thread.parts] == [
        ("m1", "message", "U03LEV", "Hi"),
        ("m2", "message", "U01MIRA", "Ok"),
    ]
    assert thread.title == "beacon"
    meeting = record_input(
        workspace="harbor",
        ref=RecordRef("meetings", "meeting", "notes-test"),
        occurred_at=WHEN,
        payload={
            "title": "Sync",
            "started_at": "2026-10-01T09:00:00Z",
            "summary": "## Decisions\n\n- One.\n",
            "action_items": [{"text": "Do it", "owner": "Lev Zarechny"}],
            "transcript": [{"id": "s01", "speaker": "Lev Zarechny", "start": 90, "text": "One."}],
        },
    )
    assert [(p.anchor, p.kind) for p in meeting.parts] == [
        ("summary", "summary"),
        ("action-items", "action-items"),
        ("s01", "segment"),
    ]
    segment = meeting.part("s01")
    assert segment is not None
    assert segment.at == datetime(2026, 10, 1, 9, 1, 30, tzinfo=UTC)
    assert meeting.anchors == {"summary", "action-items", "s01"}
    with pytest.raises(ValueError, match="threads"):
        record_input(
            workspace="harbor",
            ref=RecordRef("docs", "document", "wiki/x"),
            occurred_at=WHEN,
            payload={"title": "X", "text": ""},
        )


def _drop_no_item(labels: Json) -> None:
    labels["no_items"].pop()


def _bad_anchor(labels: Json) -> None:
    labels["items"][0]["evidence"][0]["anchor"] = "no-such-message"


def _wrong_status(labels: Json) -> None:
    labels["items"][0]["status"] = "active"  # d-hold-30, which d-hold-2h replaces


def _labelled_and_empty(labels: Json) -> None:
    evidence = labels["items"][0]["evidence"][0]
    labels["no_items"].append(
        {key: evidence[key] for key in ("source", "record_kind", "external_id")}
    )


def _question_answers(labels: Json) -> None:
    labels["links"][1] = {"from": "q-on-call-allowance", "to": "d-tag-rc4", "relation": "answers"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_drop_no_item, "is not labelled"),
        (_bad_anchor, "not an anchor"),
        (_wrong_status, "the links make it superseded"),
        (_labelled_and_empty, "holds labels too"),
        (_question_answers, "decision answers an open question"),
    ],
)
def test_labels_that_disagree_with_the_records_are_refused(
    tmp_path: Path, change: Callable[[Json], None], message: str
) -> None:
    copy = tmp_path / "cases"
    shutil.copytree(CASES, copy)
    labels = raw_labels(copy)
    change(labels)
    (copy / "labels" / "decisions.json").write_text(json.dumps(labels), encoding="utf-8")
    with pytest.raises(DecisionSetError, match=message):
        load_set(copy)


def test_a_set_without_files_or_twice_named_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DecisionSetError, match="not a directory"):
        load_set(tmp_path / "nothing")
    with pytest.raises(DecisionSetError, match="no batches"):
        load_set(tmp_path)
    copy = tmp_path / "comms"
    shutil.copytree(CASES, copy)
    with pytest.raises(DecisionSetError, match="same name"):
        load_sets([COMMS, copy])


# --- Matching ------------------------------------------------------------------------------------


def test_terms_drop_function_words_and_cut_words_to_their_stems() -> None:
    assert terms("Beacon sends the packets as CBOR") == {"beaco", "send", "packe", "cbor"}
    assert terms("v2.3 on 1 October") == {"v2", "3", "1", "octob"}
    assert terms("Логи стенда храним 90 дней") == {"логи", "стенд", "храни", "90", "дней"}
    assert similarity("Beacon sends packets as CBOR.", "beacon SENDS a packet as cbor") == 1.0
    assert similarity("Beacon sends packets as CBOR.", "Tide tables in UTC only.") == 0.0
    assert similarity("", "Anything") == 0.0
    assert similarity("the stand keeps logs 90 days", "the stand keeps logs 30 days") == 0.8


def target(kind: str, label: str, anchors: set[str], statement: str = "X") -> Target:
    return Target(cast(Any, kind), label, "decision", statement, frozenset(anchors))


def proposed(statement: str, *anchors: str, kind: str = "decision") -> ProposedItem:
    return ProposedItem(cast(Any, kind), statement, WHEN, anchors)


def test_evidence_decides_before_the_statement() -> None:
    targets = [
        target("item", "d-no-cache", {"m4", "m3"}, "The planner reads the table without a cache."),
        target("trap", "t-cache", {"m1"}, "The planner puts a cache in front of the table."),
    ]
    # Cited where the trap is, though worded like the item: the trap.
    found = match_record([proposed("The planner reads the table without a cache.", "m1")], targets)
    assert [(m.outcome, m.by) for m in found] == [("trap", "anchor")]
    # Cited nowhere labelled, worded like the item: the item, by its statement.
    found = match_record([proposed("The planner reads the table, no cache.", "m2")], targets)
    assert [(m.outcome, m.by) for m in found] == [("item", "statement")]
    # Cited nowhere labelled, worded like nothing: no match.
    found = match_record([proposed("Lunch at noon.", "m2")], targets)
    assert [m.outcome for m in found] == ["none"]


def test_kinds_must_agree() -> None:
    found = match_record(
        [proposed("Is there a cache?", "m4", kind="open_question")],
        [target("item", "d-no-cache", {"m4"})],
    )
    assert [(m.outcome, m.kind_conflict) for m in found] == [("none", True)]


def test_a_label_found_twice_in_a_record_counts_once() -> None:
    found = match_record(
        [proposed("A", "s02"), proposed("A again", "s02", "summary")],
        [target("item", "d-a", {"s02", "summary"})],
    )
    assert [m.outcome for m in found] == ["duplicate", "item"]
    assert {m.target for m in found} == {target("item", "d-a", {"s02", "summary"})}


def test_each_target_takes_the_proposal_whose_anchors_are_most_like_its_own() -> None:
    targets = [
        target("restatement", "d-json", {"summary", "s02"}),
        target("restatement", "d-date", {"summary", "s04", "s05"}),
    ]
    found = match_record([proposed("one", "summary"), proposed("two", "summary", "s02")], targets)
    assert [(m.outcome, m.target and m.target.label) for m in found] == [
        ("restatement", "d-date"),
        ("restatement", "d-json"),
    ]


# --- The run -------------------------------------------------------------------------------------


def oracle() -> OracleStub:
    return OracleStub(sets())


def every_number(report: DecisionReport) -> list[float]:
    numbers: list[float] = []
    for summary in report.extraction.values():
        numbers += [summary.precision, summary.recall]
    for summary in report.supersession.values():
        numbers += [summary.accuracy, summary.classifier_accuracy]
    return numbers


def test_the_oracle_scores_one_on_every_metric() -> None:
    stub = oracle()
    report = run(Pipeline(stub, stub))
    assert (report.precision, report.recall, report.accuracy) == (1.0, 1.0, 1.0)
    assert set(every_number(report)) == {1.0}
    items = sum(len(each.items) for each in sets())
    restatements = sum(len(each.restatements) for each in sets())
    every = report.extraction["all"]
    assert (every.items, every.found, every.false, every.restatements) == (
        items,
        items,
        0,
        restatements,
    )
    assert report.supersession["all"].cases == items + restatements
    assert set(report.supersession) >= {
        "relation:new",
        "relation:duplicate",
        "relation:replaces",
        "relation:answers",
    }
    assert report.setup["extractor"] == "stub-oracle/1"


DROPPED = "comms/q-north-basin"
CHATTER = RecordRef("chat", "thread", "C0GENERAL1/1789135200.000100")  # cake, in Russian
BROKEN = "d-utc-only"  # answers q-local-csv


class BrokenOracle:
    """The oracle with three faults: it misses one item, adds a false one, and calls the answer
    to an open question new."""

    name = "test-broken-oracle/1"

    def __init__(self) -> None:
        self.oracle = oracle()
        self.comms = the_set("comms")

    async def extract(self, record: RecordInput) -> list[ProposedItem]:
        dropped = self.comms.item(DROPPED.split("/")[1]).statement
        items = [i for i in await self.oracle.extract(record) if i.statement != dropped]
        if record.ref == CHATTER:
            first = record.parts[0]
            assert first.at is not None
            items.append(ProposedItem("decision", "Cake every Friday.", first.at, (first.anchor,)))
        return items

    async def classify(
        self, item: ProposedItem, record: RecordInput, candidates: Sequence[Candidate]
    ) -> Verdict:
        if item.statement == self.comms.item(BROKEN).statement:
            return Verdict()
        return await self.oracle.classify(item, record, candidates)


def test_a_broken_oracle_scores_what_its_faults_predict() -> None:
    stub = BrokenOracle()
    report = run(Pipeline(stub, stub))
    every = sets()
    items = [item for each in every for item in each.items]
    n = len(items)
    cases = n + sum(len(each.restatements) for each in every)
    questions = sum(1 for i in items if i.kind == "open_question")
    decisions = n - questions
    russian = sum(1 for i in items if i.language == "ru")
    english = n - russian
    answers = sum(1 for each in every for link in each.links if link.relation == "answers")
    new = sum(1 for each in every for i in each.items if not each.links_from(i.id))
    comms = len(the_set("comms").items)

    def summary(group: str) -> ExtractionSummary:
        return report.extraction[group]

    assert report.precision == pytest.approx((n - 1) / n)  # n - 1 found, 1 false
    assert report.recall == pytest.approx((n - 1) / n)
    assert summary("kind:open_question").recall == pytest.approx((questions - 1) / questions)
    assert summary("kind:open_question").precision == 1.0
    assert summary("kind:decision").precision == pytest.approx(decisions / (decisions + 1))
    assert summary("kind:decision").recall == 1.0
    assert summary("language:ru").precision == pytest.approx(russian / (russian + 1))
    assert summary("language:en").recall == pytest.approx((english - 1) / english)
    assert summary("set:comms").precision == pytest.approx((comms - 1) / comms)
    assert summary("set:cases").precision == summary("set:cases").recall == 1.0

    supersession = report.supersession["all"]
    assert (supersession.cases, supersession.extracted, supersession.correct) == (
        cases,
        cases - 1,
        cases - 2,
    )
    assert report.accuracy == pytest.approx((cases - 2) / cases)
    assert supersession.classifier_accuracy == pytest.approx((cases - 2) / (cases - 1))
    assert report.supersession["relation:answers"].correct == answers - 1
    assert report.supersession["relation:new"].correct == new - 1
    assert report.supersession["relation:duplicate"].accuracy == 1.0

    assert report.items[DROPPED].found is False
    assert report.cases[DROPPED].got is None
    broken = report.cases[f"comms/{BROKEN}"]
    assert (broken.expected, broken.got, broken.reached, broken.correct) == (
        "answers q-local-csv",
        "new",
        True,
        False,
    )
    false = [p for p in report.proposals if p["outcome"] == "none"]
    assert [(p["record"], p["statement"]) for p in false] == [(str(CHATTER), "Cake every Friday.")]


def test_a_verdict_that_names_no_candidate_counts_wrong() -> None:
    stub = oracle()
    replaced = the_set("comms").item("d-json-gzip").statement

    class Invalid:
        async def classify(
            self, item: ProposedItem, record: RecordInput, candidates: Sequence[Candidate]
        ) -> Verdict:
            if item.statement == replaced:
                return Verdict(supersedes=(Supersedes("c999", "replaces"),))
            return await stub.classify(item, record, candidates)

    report = run(Pipeline(stub, Invalid()))
    case = report.cases["comms/d-json-gzip"]
    assert (case.got, case.correct) == ("replaces ?c999", False)
    assert report.accuracy < 1.0
    assert any("no candidates" in note for note in report.notes)
    assert any("invalid" in p for p in report.proposals)


def test_a_failing_extractor_stops_the_run() -> None:
    class Failing:
        name = "test-failing/1"

        async def extract(self, record: RecordInput) -> list[ProposedItem]:
            raise RuntimeError("model unreachable")

    with pytest.raises(DecisionEvalError, match="extractor failed on chat/thread/"):
        run(Pipeline(Failing(), oracle()))


def test_a_verdict_is_new_a_duplicate_or_links() -> None:
    assert Verdict().label == "new"
    assert Verdict(duplicate_of="c1").label == "duplicate"
    assert Verdict(supersedes=(Supersedes("c1", "replaces"),)).label == "replaces"
    mixed = Verdict(supersedes=(Supersedes("c1", "replaces"), Supersedes("c2", "answers")))
    assert (mixed.label, mixed.named) == ("mixed", {"c1", "c2"})
    with pytest.raises(ValueError, match="duplicate"):
        Verdict(duplicate_of="c1", supersedes=(Supersedes("c2", "replaces"),))
    with pytest.raises(ValueError, match="once"):
        Verdict(supersedes=(Supersedes("c1", "replaces"), Supersedes("c1", "answers")))
    with pytest.raises(ValueError, match="at least one"):
        ProposedItem("decision", "A", WHEN, ())


def test_the_keyword_stub_is_deterministic_and_holds_the_committed_baseline() -> None:
    first = run(EXTRACTORS["keyword"].create(VariantContext(sets())))
    second = run(EXTRACTORS["keyword"].create(VariantContext(sets())))
    assert first.to_json() == second.to_json()
    for metric in (first.precision, first.recall, first.accuracy):
        assert 0 < metric < 1
    baseline = read_report(BASELINE)
    assert baseline.setup["extractor"] == KeywordStub.name
    comparison = compare(first, baseline, margin=0.02)
    assert comparison.regressions == (), format_comparison(comparison, str(BASELINE))


# --- Reports -------------------------------------------------------------------------------------


def test_a_report_survives_json(tmp_path: Path) -> None:
    stub = BrokenOracle()
    report = run(Pipeline(stub, stub))
    path = tmp_path / "report.json"
    write_report(path, report)
    again = read_report(path)
    assert again.to_json() == report.to_json()
    text = format_report(again)
    for line in ("extraction precision", "extraction recall", "supersession accuracy"):
        assert line in text


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "not JSON"),
        ("[]", "not a report"),
        ('{"format": 1, "kind": "decisions"}', "not a report"),
    ],
)
def test_a_bad_baseline_is_refused(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(DecisionReportError, match=message):
        read_report(path)
    with pytest.raises(DecisionReportError, match="cannot read"):
        read_report(tmp_path / "missing.json")
    with pytest.raises(DecisionReportError, match="not a report of memini eval decisions"):
        read_report(ROOT / "eval" / "code" / "baseline-fake.json")


def test_compare_flags_what_fell_by_more_than_the_margin() -> None:
    stub = oracle()
    perfect = run(Pipeline(stub, stub))
    broken_stub = BrokenOracle()
    broken = run(Pipeline(broken_stub, broken_stub))
    fell = compare(broken, perfect, margin=0.02)
    assert ("all", "recall") in {(c.group, c.metric) for c in fell.regressions}
    assert ("supersession all", "accuracy") in {(c.group, c.metric) for c in fell.regressions}
    assert f"item {DROPPED}: missed now" in fell.changed
    assert not compare(broken, perfect, margin=0.5).regressions
    rose = compare(perfect, broken, margin=0.02)
    assert not rose.regressions
    assert rose.improvements
    text = format_comparison(rose, "base.json")
    assert "write the baseline again" in text
    other = replace(perfect, setup={**perfect.setup, "extractor": "another/1"})
    assert (
        "the baseline was measured with other extractor"
        in compare(other, perfect, margin=0.02).notes
    )


# --- The command ---------------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", "postgresql://fake:fake@db.invalid:5432/memini")
    return monkeypatch


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    sets_args = ["--set", str(COMMS), "--set", str(CASES)]
    code = main(["eval", "decisions", *sets_args, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_command_runs_a_stub_and_prints_the_three_metrics(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del clean_env
    code, out, err = command(capsys, "--extractor", "oracle")
    assert code == EXIT_OK, err
    assert re.search(r"^extraction precision +1\.00 ", out, re.MULTILINE)
    assert re.search(r"^extraction recall +1\.00 ", out, re.MULTILINE)
    assert re.search(r"^supersession accuracy +1\.00 ", out, re.MULTILINE)
    assert "with oracle" in err

    code, out, err = command(capsys, "--baseline", str(BASELINE))
    assert code == EXIT_OK, err
    assert "no tracked metric fell by more than the margin" in out


def test_the_command_exits_with_4_on_a_regression_beyond_the_margin(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    del clean_env
    baseline = tmp_path / "oracle.json"
    code, _, err = command(
        capsys, "--extractor", "oracle", "--baseline", str(baseline), "--write-baseline"
    )
    assert (code, f"wrote the baseline to {baseline}" in err) == (EXIT_OK, True)
    assert read_report(baseline).precision == 1.0

    code, out, _ = command(capsys, "--baseline", str(baseline))
    assert code == EXIT_REGRESSION == 4
    assert "regressions:" in out

    code, _, _ = command(capsys, "--baseline", str(baseline), "--margin", "0.99")
    assert code == EXIT_OK

    report = tmp_path / "report.json"
    code, out, _ = command(capsys, "--extractor", "oracle", "--json", str(report))
    assert code == EXIT_OK
    assert read_report(report).accuracy == 1.0
    code, out, _ = command(capsys, "--extractor", "oracle", "--json", "-")
    assert json.loads(out)["metrics"] == {
        "precision": 1.0,
        "recall": 1.0,
        "supersession_accuracy": 1.0,
    }


def test_the_command_refuses_bad_input_before_it_runs(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    del clean_env
    code, _, err = command(capsys, "--write-baseline")
    assert (code, "--write-baseline needs --baseline" in err) == (EXIT_USAGE, True)

    code, _, err = command(capsys, "--set", str(tmp_path))
    assert (code, "invalid set" in err) == (EXIT_USAGE, True)

    bad = tmp_path / "baseline.json"
    bad.write_text("[]", encoding="utf-8")
    code, _, err = command(capsys, "--baseline", str(bad))
    assert (code, "invalid baseline" in err) == (EXIT_USAGE, True)

    code, _, err = command(capsys, "--extractor", "gpt")
    assert (code, "invalid choice" in err) == (EXIT_USAGE, True)


def test_eval_without_a_subcommand_is_still_the_retrieval_eval() -> None:
    parser = build_parser()
    assert parser.parse_args(["eval", "--fake-embeddings"]).command is evaluate.eval_command
    args = parser.parse_args(["eval", "decisions", "--extractor", "oracle"])
    assert args.command is evaluate_decisions.decisions_command
    assert args.extractor == "oracle"
