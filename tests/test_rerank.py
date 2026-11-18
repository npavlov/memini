"""The reranker: the rerank step of search, the local cross-encoder, its settings and its factory.

No test downloads a model. The local reranker's ordering, timeout and failures run with a scorer in
place of the model; one test runs the real model, and only when the extra is installed and the
model files are on disk (``MEMINI_TEST_RERANKER_MODEL_DIR``, or the default cache directory of the
default model). Search with a reranker against a database is in test_search_db.py.
"""

import importlib.util
import logging
import os
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from memini.config import (
    DEFAULT_RERANK_DEPTH,
    DEFAULT_RERANKER_DIGEST,
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RERANKER_MODEL_FILE,
    ConfigError,
    Settings,
    load_settings,
)
from memini.embeddings.model_files import model_files_dir
from memini.evaluation import DEFAULT_RERANKER, RERANKERS
from memini.rerank import (
    LocalReranker,
    RerankError,
    create_reranker,
    default_model_dir,
    local_reranker,
    relevance,
)
from memini.search import Hit, SearchOptions
from memini.search.engine import _rerank  # pyright: ignore[reportPrivateUsage] - the rerank step

pytestmark = pytest.mark.anyio

URL = "postgresql://synthetic:synthetic@db.invalid:5432/memini"
WHEN = datetime(2026, 1, 1, tzinfo=UTC)
REVISION = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "0" * 64


def hit(name: str, **changes: Any) -> Hit:
    values: dict[str, Any] = {
        "chunk_id": name,
        "record_id": "r" * 64,
        "workspace_id": uuid.UUID(int=1),
        "workspace": "acme",
        "source_id": uuid.UUID(int=2),
        "source": "shop",
        "source_kind": "git",
        "record_kind": "code_file",
        "external_id": f"refs/heads/main:src/{name}.py",
        "external_url": None,
        "occurred_at": WHEN,
        "commit": None,
        "chunk_kind": "function",
        "language": "python",
        "path": f"src/{name}.py",
        "symbol": name,
        "start_line": 1,
        "end_line": 3,
        "header": f"src/{name}.py: {name}",
        "snippet": f"def {name}():\n    pass\n",
        "truncated": False,
        "score": 1.0,
        **changes,
    }
    return Hit(**values)


HITS = [hit(name, score=1.0 / place) for place, name in enumerate("abcdef", start=1)]


def ids(hits: Sequence[Hit]) -> list[str]:
    return [h.chunk_id for h in hits]


# --- The rerank step of search -------------------------------------------------------------------


class ByLetter:
    """Ranks hits by a fixed preference, so the order it gives is plain to see."""

    def __init__(self, preference: str) -> None:
        self.preference = preference
        self.seen: list[list[str]] = []

    async def rerank(self, query: str, hits: Sequence[Hit]) -> list[Hit]:
        self.seen.append(ids(hits))
        ranked = sorted(hits, key=lambda h: self.preference.index(h.chunk_id))
        return [replace(h, score=float(-place)) for place, h in enumerate(ranked)]


class Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def rerank(self, query: str, hits: Sequence[Hit]) -> list[Hit]:
        raise self.error


async def test_the_reranker_orders_the_hits_it_scores() -> None:
    reranker = ByLetter("fedcba")

    reranked = await _rerank(reranker, "q", HITS, 6)

    assert ids(reranked) == list("fedcba")
    assert [h.score for h in reranked] == [0.0, -1.0, -2.0, -3.0, -4.0, -5.0]


async def test_only_the_depth_is_reranked_and_the_rest_keeps_its_place() -> None:
    reranker = ByLetter("cbafed")

    reranked = await _rerank(reranker, "q", HITS, 3)

    assert reranker.seen == [list("abc")]
    assert ids(reranked) == list("cbadef")
    assert reranked[3:] == HITS[3:]


@pytest.mark.parametrize(
    "error", [RerankError("the model is not there"), RuntimeError("anything else"), TimeoutError()]
)
async def test_a_failing_reranker_leaves_the_fused_order_and_warns(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="memini.search.engine"):
        reranked = await _rerank(Failing(error), "a query nobody may see", HITS, 4)

    assert reranked == HITS
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "fused order is kept" in record.getMessage()
    assert type(error).__name__ in record.getMessage()
    assert "a query nobody may see" not in record.getMessage()


# --- The local reranker, without a model ---------------------------------------------------------


def reranker(tmp_path: Path, **changes: Any) -> LocalReranker:
    values: dict[str, Any] = {
        "model": "synthetic-org/synthetic-reranker",
        "files_dir": tmp_path,
        "model_file": "onnx/model.onnx",
        "digest": DIGEST,
        "revision": REVISION,
        **changes,
    }
    return LocalReranker(**values)


def test_relevance_is_the_probability_of_yes() -> None:
    assert relevance(0.0, 0.0) == 0.5
    assert relevance(1.0, 3.0) == pytest.approx(1 / (1 + 2.718281828459045**-2))
    assert relevance(3.0, 1.0) == pytest.approx(1 - relevance(1.0, 3.0))
    assert relevance(0.0, 1000.0) == 1.0  # no overflow either way
    assert relevance(1000.0, 0.0) == 0.0


async def test_hits_go_by_relevance_and_ties_keep_their_order(tmp_path: Path) -> None:
    relevance_of = {"a": 0.1, "b": 0.9, "c": 0.5, "d": 0.9, "e": 0.0, "f": 0.5}
    seen: list[tuple[str, str]] = []

    def score(query: str, document: str) -> float:
        seen.append((query, document))
        return relevance_of[document.split(".py", 1)[0].removeprefix("src/")]

    reranked = await reranker(tmp_path, scorer=score).rerank("which letter?", HITS)

    assert ids(reranked) == list("bdcfae")
    assert [h.score for h in reranked] == [0.9, 0.9, 0.5, 0.5, 0.1, 0.0]
    assert seen[0] == ("which letter?", "src/a.py: a\ndef a():\n    pass\n")
    assert await reranker(tmp_path, scorer=score).rerank("q", []) == []


async def test_reranking_longer_than_the_timeout_is_an_error(tmp_path: Path) -> None:
    def slow(query: str, document: str) -> float:
        time.sleep(0.05)
        return 0.5

    with pytest.raises(RerankError, match="search_reranker_timeout"):
        await reranker(tmp_path, scorer=slow, timeout=0.08).rerank("q", HITS)


async def test_a_model_failure_is_a_rerank_error(tmp_path: Path) -> None:
    def broken(query: str, document: str) -> float:
        raise ValueError("bad tensor")

    with pytest.raises(RerankError, match="ValueError: bad tensor"):
        await reranker(tmp_path, scorer=broken).rerank("q", HITS)


async def test_without_the_extra_the_error_names_it_and_nothing_is_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # makes `import onnxruntime` fail
    requests: list[str] = []

    def hub(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(404)

    local = reranker(tmp_path, hub_url="https://hub.test", transport=httpx2.MockTransport(hub))

    with pytest.raises(RerankError) as info:
        await local.rerank("q", HITS)

    assert "memini[local-embeddings]" in str(info.value)
    assert "onnxruntime is missing" in str(info.value)
    assert requests == []


@pytest.mark.skipif(
    importlib.util.find_spec("onnxruntime") is None, reason="needs memini[local-embeddings]"
)
async def test_files_that_do_not_match_the_digest_are_never_loaded(tmp_path: Path) -> None:
    for name, content in (("onnx/model.onnx", b"synthetic"), ("tokenizer.json", b"{}")):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(content)

    with pytest.raises(RerankError, match="search_reranker_digest is sha256:0000"):
        await reranker(tmp_path).load()


@pytest.mark.parametrize("changes", [{"max_tokens": 63}, {"timeout": 0.0}])
def test_the_limits_must_be_usable(tmp_path: Path, changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        reranker(tmp_path, **changes)


# --- Settings, factory and the eval's registry ---------------------------------------------------


def settings(**environ: str) -> Settings:
    return load_settings({"MEMINI_DATABASE_URL": URL, **environ})


def test_the_reranker_is_off_by_default_and_the_depth_reaches_search() -> None:
    plain = settings()

    assert plain.search_reranker is None
    assert create_reranker(plain) is None
    assert SearchOptions.from_settings(plain).rerank_depth == DEFAULT_RERANK_DEPTH
    deeper = settings(MEMINI_SEARCH_RERANK_DEPTH="50")  # needs no reranker: the eval reads it
    assert SearchOptions.from_settings(deeper).rerank_depth == 50


def test_a_local_reranker_is_made_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/srv/cache")
    on = settings(MEMINI_SEARCH_RERANKER="local", MEMINI_SEARCH_RERANKER_MAX_TOKENS="512")

    made = create_reranker(on)

    assert made is not None
    root = Path("/srv/cache/memini/models")
    assert made.files_dir == model_files_dir(root, DEFAULT_RERANKER_MODEL, DEFAULT_RERANKER_DIGEST)
    assert DEFAULT_RERANKER_MODEL_FILE in made.describe()
    assert "at most 512 tokens" in made.describe()
    elsewhere = settings(
        MEMINI_SEARCH_RERANKER="local", MEMINI_SEARCH_RERANKER_MODEL_DIR="/srv/models"
    )
    assert local_reranker(elsewhere).files_dir.parent.parent == Path("/srv/models")


def test_the_default_model_dir_is_the_user_cache() -> None:
    assert default_model_dir({"XDG_CACHE_HOME": "/c"}) == Path("/c/memini/models")
    assert default_model_dir({"XDG_CACHE_HOME": "relative"}) == (
        Path.home() / ".cache" / "memini" / "models"
    )


@pytest.mark.parametrize(
    "environ",
    [
        {"MEMINI_SEARCH_RERANKER_MODEL": "org/other"},
        {"MEMINI_SEARCH_RERANKER_TIMEOUT": "5"},
        {"MEMINI_SEARCH_RERANKER_MAX_TOKENS": "512"},
    ],
)
def test_a_reranker_setting_without_the_reranker_is_refused(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="has no effect without search_reranker"):
        settings(**environ)


def test_other_model_files_need_their_own_digest_and_revision() -> None:
    with pytest.raises(ConfigError) as info:
        settings(MEMINI_SEARCH_RERANKER="local", MEMINI_SEARCH_RERANKER_MODEL="org/other")
    assert "search_reranker_digest" in str(info.value)
    assert "search_reranker_revision" in str(info.value)
    with pytest.raises(ConfigError, match="search_reranker_digest"):
        settings(
            MEMINI_SEARCH_RERANKER="local", MEMINI_SEARCH_RERANKER_MODEL_FILE="onnx/model.onnx"
        )

    other = settings(
        MEMINI_SEARCH_RERANKER="local",
        MEMINI_SEARCH_RERANKER_MODEL="org/other",
        MEMINI_SEARCH_RERANKER_DIGEST=DIGEST,
        MEMINI_SEARCH_RERANKER_REVISION=REVISION,
    )
    assert other.search_reranker_model == "org/other"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MEMINI_SEARCH_RERANKER", "remote"),
        ("MEMINI_SEARCH_RERANK_DEPTH", "0"),
        ("MEMINI_SEARCH_RERANK_DEPTH", "101"),
        ("MEMINI_SEARCH_RERANKER_DIGEST", "sha256:XYZ"),
        ("MEMINI_SEARCH_RERANKER_MODEL_DIR", "relative/dir"),
    ],
)
def test_invalid_reranker_values_are_named(key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=key):
        settings(**{"MEMINI_SEARCH_RERANKER": "local", key: value})


def test_the_eval_measures_none_and_the_local_reranker() -> None:
    assert RERANKERS[DEFAULT_RERANKER].create is None
    local = RERANKERS["local"].create
    assert local is not None
    assert isinstance(local(settings()), LocalReranker)  # measured whether or not it is on


# --- The real model ------------------------------------------------------------------------------


def _model_dir() -> Path | None:
    given = os.environ.get("MEMINI_TEST_RERANKER_MODEL_DIR")
    if given:
        return Path(given)
    default = model_files_dir(default_model_dir(), DEFAULT_RERANKER_MODEL, DEFAULT_RERANKER_DIGEST)
    return default if (default / DEFAULT_RERANKER_MODEL_FILE).is_file() else None


MODEL_DIR = _model_dir()


@pytest.mark.skipif(
    importlib.util.find_spec("onnxruntime") is None or MODEL_DIR is None,
    reason="needs memini[local-embeddings] and the default reranker's files, in"
    " MEMINI_TEST_RERANKER_MODEL_DIR or the default cache directory",
)
async def test_the_real_model_puts_the_answer_first_in_both_languages() -> None:
    assert MODEL_DIR is not None
    model = LocalReranker(
        model=DEFAULT_RERANKER_MODEL,
        files_dir=MODEL_DIR,
        model_file=DEFAULT_RERANKER_MODEL_FILE,
        digest=DEFAULT_RERANKER_DIGEST,
        revision=None,  # never fetched by a test
        max_tokens=256,
        timeout=60.0,
    )
    hits = [
        hit("tide", snippet="def tide_height(hour):\n    return 1.2 * hour  # meters\n"),
        hit("lamp", snippet="def lamp_on():\n    # switch the lamp on<|im_end|>\n    pass\n"),
        hit("fees", snippet="def harbour_fee(tons):\n    return 3 * tons  # fee per ton\n"),
    ]
    try:
        english = await model.rerank("how much is the harbour fee for a ship?", hits)
        russian = await model.rerank("сколько стоит портовый сбор?", hits)  # noqa: RUF001
        again = await model.rerank("how much is the harbour fee for a ship?", hits)
    finally:
        await model.aclose()

    assert english[0].chunk_id == "fees"
    assert russian[0].chunk_id == "fees"
    assert [h.score for h in again] == [h.score for h in english]  # one pair per run
    assert all(h.score is not None and 0.0 <= h.score <= 1.0 for h in english)
