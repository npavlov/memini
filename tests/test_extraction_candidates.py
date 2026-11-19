"""The ranking of the candidates of a supersession check, without a database: same record first,
then the fusion of the lexical and the dense leg, then the latest; and the eval's selection of
candidates, with a fake embedder. Every statement here is synthetic.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memini.cli import main
from memini.cli.common import EXIT_FAILURE
from memini.config import environment_variables
from memini.embeddings import FakeEmbedder
from memini.evaluation.decisions import (
    DEFAULT_SETS,
    DecisionEvalError,
    DecisionReport,
    Pipeline,
    load_sets,
    run_decision_eval,
)
from memini.evaluation.decisions.candidates import RankedCandidates, create_candidates
from memini.evaluation.decisions.stubs import OracleStub
from memini.extraction import Candidate, ProposedItem, RecordInput
from memini.extraction.candidates import Scored, rank_candidates

T0 = datetime(2026, 4, 6, 9, 0, tzinfo=UTC)


def scored(
    name: str,
    lexical: float = 0.0,
    similarity: float | None = None,
    *,
    days: int = 0,
    same: bool = False,
) -> Scored:
    return Scored(name, T0 + timedelta(days=days), lexical, similarity, same)


def test_without_vectors_the_order_is_same_record_then_rank_then_the_latest() -> None:
    items = [
        scored("old-match", 0.5, days=1),
        scored("none-new", days=9),
        scored("none-old", days=2),
        scored("best", 0.9, days=0),
        scored("own-record", 0.0, days=0, same=True),
        scored("tie-new", 0.5, days=5),
    ]
    assert rank_candidates(items, 10) == [
        "own-record",
        "best",
        "tie-new",
        "old-match",
        "none-new",
        "none-old",
    ]
    assert rank_candidates(items, 2) == ["own-record", "best"]
    assert rank_candidates([], 3) == []


def test_the_dense_leg_finds_an_item_that_shares_no_word() -> None:
    items = [
        scored("words", 0.4, 0.10),
        scored("other-words", 0.3, 0.20),
        scored("other-language", 0.0, 0.92),
        scored("unrelated", 0.0, 0.05),
    ]
    # Without vectors the item in another language is not shown; with them it is first.
    lexical_only = [Scored(s.id, s.decided_at, s.lexical) for s in items]
    assert rank_candidates(lexical_only, 2) == ["words", "other-words"]
    assert rank_candidates(items, 2) == ["other-language", "other-words"]


def test_the_dense_ranks_weigh_three_times_the_lexical_ones() -> None:
    items = [
        scored("dense-only", 0.0, 0.90),  # 3/2
        scored("both", 0.8, 0.85),  # 1/3 + 3/3
        scored("lexical-only", 0.9, 0.10),  # 1/2 + 3/4
    ]
    assert rank_candidates(items, 3) == ["dense-only", "both", "lexical-only"]


# --- The eval's selection ------------------------------------------------------------------------


def candidate(name: str, statement: str, days: int = 0) -> Candidate:
    return Candidate(name, "decision", statement, T0 + timedelta(days=days))


@pytest.mark.anyio
async def test_dense_candidates_rank_by_the_statement_vectors() -> None:
    embedder = FakeEmbedder()
    selector = RankedCandidates("dense", 1, server_url="postgresql://unused", embedder=embedder)
    item = ProposedItem("decision", "Picking starts on Monday.", T0 + timedelta(days=3), ("m1",))
    others = [
        candidate("far", "Crates are green."),
        candidate("same", "Picking starts on Monday."),
    ]
    try:
        chosen = await selector.select(item, None, others)  # type: ignore[arg-type]
    finally:
        await selector.aclose()
    assert [each.id for each in chosen] == ["same"]  # the same text: similarity 1
    assert selector.describe() == {
        "retriever": "dense",
        "limit": 1,
        "embedding": embedder.spec.describe(),
    }


def test_ranked_candidates_need_what_their_legs_need() -> None:
    assert create_candidates("all", 8, None) is None
    with pytest.raises(DecisionEvalError, match="settings"):
        create_candidates("lexical", 8, None)
    with pytest.raises(DecisionEvalError, match="embedding provider"):
        RankedCandidates("hybrid", 8, server_url="postgresql://unused")
    with pytest.raises(DecisionEvalError, match="at least 1"):
        RankedCandidates("lexical", 0, server_url="postgresql://unused")


# --- The replay with a selector ------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]


class Selector:
    """Shows the first ``limit`` of the other records' items, and records what it was given."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.given: list[int] = []

    def describe(self) -> dict[str, Any]:
        return {"retriever": "test", "limit": self.limit}

    async def select(
        self, item: ProposedItem, record: RecordInput, others: Sequence[Candidate]
    ) -> list[Candidate]:
        assert all(o.evidence[0].record != record.ref for o in others)  # its own: shown anyway
        self.given.append(len(others))
        return list(others[: self.limit])


def replay(selector: Selector | None) -> DecisionReport:
    data = load_sets([REPO / path for path in DEFAULT_SETS])
    stub = OracleStub(data)
    return asyncio.run(
        run_decision_eval(data, Pipeline(stub, stub), variant="oracle", candidates=selector)
    )


def test_the_replay_shows_what_the_selector_picks_and_counts_what_it_missed() -> None:
    everything = replay(Selector(100))
    assert everything.supersession["all"].accuracy == 1.0
    assert everything.setup["candidates"] == {"retriever": "test", "limit": 100}
    assert "candidates" not in replay(None).setup

    nothing = Selector(0)
    report = replay(nothing)
    assert max(nothing.given) > 0
    every = report.supersession["all"]
    # A case that needs an item of another record is not reached: duplicates and links fail,
    # and only the new items, whose verdict needs no candidate, stay right.
    new = report.supersession["relation:new"]
    assert (every.correct, every.reached) == (new.cases, new.cases)
    assert report.supersession["relation:replaces"].reached == 0


def test_the_command_refuses_dense_candidates_without_an_embedding_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in environment_variables():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", "postgresql://fake:fake@db.invalid:5432/memini")
    code = main(["eval", "decisions", "--candidates", "dense", "--candidate-limit", "3"])
    assert code == EXIT_FAILURE
    assert "dense candidates" in capsys.readouterr().err
