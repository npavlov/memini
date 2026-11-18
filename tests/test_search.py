"""The search library without a database: query validation, path globs, patterns, rank fusion,
options and the hit's locator. Every value is synthetic. The SQL runs in test_search_db.py."""

import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from memini.config import ConfigError, load_settings
from memini.search import (
    MAX_LIMIT,
    Hit,
    InvalidSearchError,
    SearchFilters,
    SearchOptions,
    SearchQuery,
    glob_regex,
    pinned_commit,
    reciprocal_rank_fusion,
)
from memini.search.patterns import (
    MAX_LEXICAL_TERMS,
    globs_regex,
    lexical_terms,
    like_pattern,
    line_mode,
)

FAKE_URL = "postgresql://fake:fake@db.invalid:5432/memini"

# --- Path globs ----------------------------------------------------------------------------------


def matches(glob: str, path: str) -> bool:
    # The regex is an ARE; the parts glob_regex uses (\uXXXX, (?:...), [^/]) read alike in Python.
    return re.search(glob_regex(glob), path) is not None


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        # No slash but at the end: the name at any depth, and everything below a directory.
        ("*.py", "a.py", True),
        ("*.py", "src/memini/a.py", True),
        ("*.py", "src/a.pyc", False),
        ("*.py", "src/a.py.txt", False),
        ("tests", "tests/test_a.py", True),
        ("tests", "src/tests/test_a.py", True),
        ("tests", "src/latests/a.py", False),
        ("tests/", "src/tests/a.py", True),
        ("test_?.py", "tests/test_a.py", True),
        ("test_?.py", "tests/test_ab.py", False),
        # A slash at the start or in the middle: from the root.
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
        ("src/*.py", "lib/src/a.py", False),
        ("/src/*.py", "src/a.py", True),
        ("src/memini", "src/memini/search/model.py", True),
        ("src/memini", "src/memini2/model.py", False),
        ("src/memini/", "src/memini/model.py", True),
        # ** crosses directories; as a segment it may also match none.
        ("src/**/*.py", "src/a.py", True),
        ("src/**/*.py", "src/a/b/c.py", True),
        ("src/**/*.py", "lib/a.py", False),
        ("**/ledger.ts", "ledger.ts", True),
        ("**/ledger.ts", "web/static/ledger.ts", True),
        ("src/**", "src/a/b.py", True),
        ("src/a**", "src/ab/c.py", True),
        ("src/a**", "src/b.py", False),
        # Every other character stands for itself.
        ("*.[jt]s", "a.js", False),
        ("*.[jt]s", "a.[jt]s", True),
        ("a+b.py", "a+b.py", True),
        ("a+b.py", "aab.py", False),
        ("a.py", "abpy", False),
        ("(x)|y", "(x)|y", True),
        ("(x)|y", "y", False),
        ("src/огород/*.py", "src/огород/ogorod.py", True),
        ("src/огород/*.py", "src/огород2/ogorod.py", False),  # noqa: RUF001
        ("*.🌱", "seeds/a.🌱", True),
        ("Src/*.py", "src/a.py", False),
    ],
)
def test_path_globs(glob: str, path: str, expected: bool) -> None:
    assert matches(glob, path) is expected


def test_every_character_but_letters_digits_and_slash_is_escaped() -> None:
    assert glob_regex("a.b") == r"(?:^|/)a\u002eb(?:/.*)?$"
    assert glob_regex("x/🌱") == r"^x/\U0001f331(?:/.*)?$"


def test_several_globs_are_alternatives() -> None:
    pattern = globs_regex(("*.ts", "src/*.py"))
    assert re.search(pattern, "web/a.ts")
    assert re.search(pattern, "src/a.py")
    assert not re.search(pattern, "lib/a.py")


@pytest.mark.parametrize("glob", ["", "/", "//", "a\x00b", "a\nb", "a" * 513])
def test_a_glob_that_names_no_path_is_refused(glob: str) -> None:
    with pytest.raises(InvalidSearchError):
        glob_regex(glob)


# --- Patterns -----------------------------------------------------------------------------------


def test_like_patterns_escape_their_wildcards() -> None:
    assert like_pattern("plant.soil") == "%plant.soil%"
    assert like_pattern("50%_off\\") == "%50\\%\\_off\\\\%"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"^def \w+", r"(?n)^def \w+"),
        (r"(?i)^def", r"(?ni)^def"),
        (r"(?s)a.b", r"(?ns)a.b"),
        (r"(?:a|b)c", r"(?n)(?:a|b)c"),
        (r"***=a.b", r"***=a.b"),
        (r"***:a", r"***:a"),
    ],
)
def test_regexes_run_in_line_mode(pattern: str, expected: str) -> None:
    assert line_mode(pattern) == expected


def test_lexical_terms_are_the_words_once_each() -> None:
    assert lexical_terms("  water the  plant, water ") == ["water", "the", "plant,"]
    many = " ".join(f"w{i}" for i in range(100))
    assert len(lexical_terms(many)) == MAX_LEXICAL_TERMS


# --- Reciprocal rank fusion ----------------------------------------------------------------------


def test_fusion_adds_the_reciprocal_ranks_of_each_list() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "d"]], k=60)

    by_id = {f.id: f for f in fused}
    assert by_id["c"].score == pytest.approx(1 / 63 + 1 / 61)
    assert by_id["c"].ranks == (3, 1)
    assert by_id["a"].score == pytest.approx(1 / 61)
    assert by_id["a"].ranks == (1, None)
    assert by_id["d"].ranks == (None, 2)
    assert [f.id for f in fused] == ["c", "a", "b", "d"]


def test_fusion_dedupes_and_breaks_ties_deterministically() -> None:
    fused = reciprocal_rank_fusion([["b", "a", "b"], ["a", "b"]], k=60)

    # b counts once in the first list, at rank 1.
    assert [(f.id, f.ranks) for f in fused] == [("a", (2, 1)), ("b", (1, 2))]
    # Equal scores, equal best ranks: by id.
    assert [f.id for f in reciprocal_rank_fusion([["y"], ["x"]], k=60)] == ["x", "y"]
    # Equal scores, a better best rank first.
    tie = reciprocal_rank_fusion([["p", "q"], ["q", "p"], ["r"]], k=1)
    assert [f.id for f in tie][:2] == ["p", "q"]


def test_a_larger_k_flattens_the_top_ranks() -> None:
    # a is first in one list; b is fifth in both.
    rankings = [["a", "x1", "x2", "x3", "b"], ["y1", "y2", "y3", "y4", "b"]]

    assert reciprocal_rank_fusion(rankings, k=1)[0].id == "a"  # 1/2 > 2/6
    assert reciprocal_rank_fusion(rankings, k=60)[0].id == "b"  # 1/61 < 2/65


def test_fusion_needs_a_positive_k() -> None:
    with pytest.raises(ValueError, match="k must be"):
        reciprocal_rank_fusion([["a"]], k=0)
    assert reciprocal_rank_fusion([[], []], k=60) == []


def test_fusion_weighs_each_list() -> None:
    rankings = [["a", "b", "c"], ["c", "d"]]

    fused = reciprocal_rank_fusion(rankings, k=1, weights=(3, 1))

    by_id = {f.id: f for f in fused}
    assert by_id["a"].score == pytest.approx(3 / 2)
    assert by_id["c"].score == pytest.approx(3 / 4 + 1 / 2)
    assert by_id["d"].score == pytest.approx(1 / 3)
    assert [f.id for f in fused] == ["a", "c", "b", "d"]
    # Without weights, each list counts once: c, found by both, comes first.
    assert reciprocal_rank_fusion(rankings, k=1)[0].id == "c"
    assert reciprocal_rank_fusion(rankings, k=1, weights=(1, 1)) == reciprocal_rank_fusion(
        rankings, k=1
    )


@pytest.mark.parametrize("weights", [(1.0,), (1.0, 0.0), (1.0, -2.0), (float("nan"), 1.0)])
def test_fusion_needs_one_positive_weight_per_list(weights: tuple[float, ...]) -> None:
    with pytest.raises(ValueError, match="weight"):
        reciprocal_rank_fusion([["a"], ["b"]], k=1, weights=weights)


# --- Queries and filters -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "x" * 4097},
        {"text": "a\x00b"},
        {"text": "x", "limit": 0},
        {"text": "x", "limit": MAX_LIMIT + 1},
        {"text": "x", "limit": True},
        {"text": "x", "mode": "fuzzy"},
        {"text": "x", "symbol_match": "suffix"},
    ],
)
def test_an_invalid_query_is_refused(arguments: dict[str, object]) -> None:
    with pytest.raises(InvalidSearchError):
        SearchQuery(**arguments)  # type: ignore[arg-type]


def test_a_query_has_defaults() -> None:
    query = SearchQuery(text="water")

    assert (query.mode, query.limit, query.case_sensitive, query.symbol_match) == (
        "hybrid",
        10,
        True,
        "exact",
    )
    assert query.filters == SearchFilters()


@pytest.mark.parametrize(
    "arguments",
    [
        {"kinds": ("code_file", "email")},
        {"languages": ("cobol",)},
        {"kinds": "code_file"},
        {"sources": "greenhouse"},
        {"paths": "*.py"},
        {"paths": ("",)},
        {"workspaces": ("",)},
        {"repositories": tuple(f"r{i}" for i in range(65))},
        {"since": datetime(2026, 1, 1)},  # naive on purpose
        {"since": datetime(2026, 2, 1, tzinfo=UTC), "until": datetime(2026, 1, 1, tzinfo=UTC)},
        {"since": datetime(2026, 1, 1, tzinfo=UTC), "until": datetime(2026, 1, 1, tzinfo=UTC)},
    ],
)
def test_an_invalid_filter_is_refused(arguments: dict[str, object]) -> None:
    with pytest.raises(InvalidSearchError):
        SearchFilters(**arguments)  # type: ignore[arg-type]


def test_filters_drop_empty_lists_and_repeated_values() -> None:
    workspace = uuid.uuid4()
    filters = SearchFilters(
        workspaces=("acme", workspace, "acme"),
        sources=(),
        kinds=("code_file", "code_file"),
        languages=(),
        paths=("*.py", "*.py"),
        persons=(),
        since=datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=3))),
    )

    assert filters.workspaces == ("acme", workspace)
    assert filters.sources is None
    assert filters.kinds == ("code_file",)
    assert filters.languages is None
    assert filters.paths == ("*.py",)
    assert filters.persons is None


# --- Options and settings ------------------------------------------------------------------------


def test_options_come_from_the_search_settings() -> None:
    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_SEARCH_TIMEOUT": "0.5",
            "MEMINI_SEARCH_CANDIDATES": "120",
            "MEMINI_SEARCH_RRF_K": "30",
            "MEMINI_SEARCH_DENSE_WEIGHT": "2.5",
            "MEMINI_SEARCH_LEXICAL_WEIGHT": "0.5",
        }
    )

    options = SearchOptions.from_settings(settings)

    assert (options.timeout, options.candidates, options.rrf_k) == (0.5, 120, 30)
    assert (options.dense_weight, options.lexical_weight) == (2.5, 0.5)


def test_search_settings_have_defaults_and_bounds() -> None:
    defaults = SearchOptions.from_settings(load_settings({"MEMINI_DATABASE_URL": FAKE_URL}))
    assert defaults == SearchOptions()
    assert (defaults.timeout, defaults.candidates, defaults.rrf_k) == (5.0, 50, 1)
    assert (defaults.dense_weight, defaults.lexical_weight) == (3.0, 1.0)

    with pytest.raises(ConfigError) as refused:
        load_settings(
            {
                "MEMINI_DATABASE_URL": FAKE_URL,
                "MEMINI_SEARCH_TIMEOUT": "0",
                "MEMINI_SEARCH_CANDIDATES": "1001",
                "MEMINI_SEARCH_RRF_K": "zero",
                "MEMINI_SEARCH_DENSE_WEIGHT": "0",
                "MEMINI_SEARCH_LEXICAL_WEIGHT": "101",
            }
        )
    assert [problem.split(":")[0] for problem in refused.value.problems] == [
        "MEMINI_SEARCH_TIMEOUT",
        "MEMINI_SEARCH_CANDIDATES",
        "MEMINI_SEARCH_RRF_K",
        "MEMINI_SEARCH_DENSE_WEIGHT",
        "MEMINI_SEARCH_LEXICAL_WEIGHT",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"candidates": 0},
        {"rrf_k": 0},
        {"dense_weight": 0},
        {"lexical_weight": -1},
        {"lexical_weight": float("inf")},
    ],
)
def test_invalid_options_are_refused(arguments: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be"):
        SearchOptions(**arguments)  # type: ignore[arg-type]


# --- Hits ----------------------------------------------------------------------------------------


def hit(**changes: object) -> Hit:
    values: dict[str, object] = {
        "chunk_id": "c" * 64,
        "record_id": "r" * 64,
        "workspace_id": uuid.uuid4(),
        "workspace": "acme",
        "source_id": uuid.uuid4(),
        "source": "greenhouse",
        "source_kind": "git",
        "record_kind": "code_file",
        "external_id": "main:src/greenhouse.py",
        "external_url": None,
        "occurred_at": datetime(2026, 1, 2, tzinfo=UTC),
        "commit": None,
        "chunk_kind": "function",
        "language": "python",
        "path": "src/greenhouse.py",
        "symbol": "water",
        "start_line": 8,
        "end_line": 15,
        "header": "src/greenhouse.py: water",
        "snippet": "def water(plant, litres):",
        "truncated": False,
    }
    values.update(changes)
    return Hit(**values)  # type: ignore[arg-type]


def test_a_code_hit_is_located_by_repository_commit_path_and_lines() -> None:
    assert hit().locator == "greenhouse:src/greenhouse.py#L8-L15"
    assert hit().repository == "greenhouse"
    assert hit(commit="0" * 40).locator == f"greenhouse@{'0' * 40}:src/greenhouse.py#L8-L15"


def test_a_code_hit_has_no_anchor_and_its_link_is_the_records() -> None:
    code = hit(commit="0" * 40)
    assert (code.anchor, code.chunk_url, code.chunk_occurred_at, code.link) == (
        None,
        None,
        None,
        None,
    )
    linked = hit(external_url="https://forge.example.invalid/greenhouse/blob/main/a.py")
    assert linked.locator == "greenhouse:src/greenhouse.py#L8-L15"
    assert linked.link == "https://forge.example.invalid/greenhouse/blob/main/a.py"


def test_another_hit_is_located_by_its_record_and_anchor() -> None:
    document = hit(
        source="notes",
        source_kind="push",
        record_kind="document",
        external_id="decision-watering",
        external_url="https://notes.example.invalid/1",
        path=None,
    )
    assert document.repository is None
    # The text before a document's first heading has no anchor: the record is the place.
    assert document.locator == "notes:decision-watering"
    assert document.link == "https://notes.example.invalid/1"
    section = hit(
        source="notes",
        source_kind="push",
        record_kind="document",
        external_id="decision-watering",
        path=None,
        anchor="Watering > Peppers",
    )
    assert section.locator == "notes:decision-watering#Watering > Peppers"
    assert section.link is None


def test_a_message_hit_links_to_its_message_and_keeps_the_records_time() -> None:
    record_time = datetime(2026, 3, 3, 18, 1, tzinfo=UTC)
    message_time = datetime(2026, 3, 2, 12, 10, tzinfo=UTC)
    message = hit(
        source="chat",
        source_kind="push",
        record_kind="thread",
        external_id="greenhouse/1772355600.000100",
        external_url="https://chat.example.invalid/t/1772355600000100",
        occurred_at=record_time,
        path=None,
        anchor="msg-1772440200.000300",
        chunk_url="https://chat.example.invalid/m/1772440200000300",
        chunk_occurred_at=message_time,
    )
    assert message.locator == "chat:greenhouse/1772355600.000100#msg-1772440200.000300"
    assert message.link == "https://chat.example.invalid/m/1772440200000300"
    assert (message.occurred_at, message.chunk_occurred_at) == (record_time, message_time)
    # A message without a URL of its own: the thread's.
    no_url = hit(**{**_fields(message), "chunk_url": None})
    assert no_url.link == "https://chat.example.invalid/t/1772355600000100"


def _fields(found: Hit) -> dict[str, Any]:
    return {name: getattr(found, name) for name in Hit.__dataclass_fields__}


# --- The pinned commit of a code hit -------------------------------------------------------------

_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_a_git_hit_carries_the_commit_of_its_stream_pin() -> None:
    assert pinned_commit("git", {"commit": _SHA, "rules": "r1"}) == _SHA


@pytest.mark.parametrize(
    ("kind", "cursor"),
    [
        ("git", None),  # no complete sync yet
        ("git", {"commit": _SHA, "rules": "r1", "partial": True}),  # records may be past the pin
        ("git", {"commit": "not-a-sha", "rules": "r1"}),
        ("git", {"rules": "r1"}),
        ("push", {"commit": _SHA, "rules": "r1"}),  # not a git source
    ],
)
def test_the_commit_is_unknown_without_a_complete_pin(kind: Any, cursor: object) -> None:
    assert pinned_commit(kind, cursor) is None
