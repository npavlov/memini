"""The embedder interface, the shared steps of every provider, and embedding only what is new.

Everything runs on :class:`FakeEmbedder`; no model, no network. Every text is synthetic.
"""

import math
from array import array
from collections.abc import Mapping, Sequence

import pytest

from memini.embeddings import (
    EmbeddingError,
    EmbeddingSpec,
    FakeEmbedder,
    Vector,
    embed_missing,
    text_hash,
)
from memini.embeddings.base import BatchingEmbedder, finish_vector
from memini.embeddings.fake import fake_vector

pytestmark = pytest.mark.anyio

SPEC = EmbeddingSpec(provider="openai_compatible", model="fake-embedder", dimension=8)


def texts(n: int) -> list[str]:
    return [f"synthetic chunk number {i}" for i in range(n)]


def is_float32(vector: Vector) -> bool:
    return array("f", vector).tolist() == vector


class VectorStore:
    """Stands in for the vectors of an index: a lookup by text hash, filled after each write."""

    def __init__(self) -> None:
        self.vectors: dict[str, Vector] = {}
        self.lookups: list[list[str]] = []

    async def lookup(self, hashes: Sequence[str]) -> Mapping[str, Vector]:
        self.lookups.append(list(hashes))
        return {key: self.vectors[key] for key in hashes if key in self.vectors}

    def store(self, vectors: Mapping[str, Vector]) -> None:
        self.vectors.update(vectors)


# --- Vectors -------------------------------------------------------------------------------------


async def test_vectors_have_the_dimension_unit_length_and_float32_values() -> None:
    fake = FakeEmbedder(SPEC)

    vectors = await fake.embed(texts(3))

    assert [len(vector) for vector in vectors] == [8, 8, 8]
    for vector in vectors:
        assert math.isclose(math.fsum(x * x for x in vector), 1.0, rel_tol=1e-6)
        assert is_float32(vector)


async def test_vectors_are_deterministic_and_keep_the_order_of_the_texts() -> None:
    fake = FakeEmbedder(SPEC, batch_size=2)
    batch = texts(5)

    together = await fake.embed(batch)
    alone = [(await fake.embed([text]))[0] for text in batch]

    assert together == alone
    assert len({tuple(vector) for vector in together}) == 5


@pytest.mark.parametrize(
    ("count", "batch_size", "calls"),
    [(0, 4, 0), (1, 4, 1), (4, 4, 1), (5, 4, 2), (10, 4, 3), (10, 1, 10), (64, 32, 2)],
)
async def test_n_texts_take_ceil_n_over_batch_size_model_calls(
    count: int, batch_size: int, calls: int
) -> None:
    fake = FakeEmbedder(SPEC, batch_size=batch_size)

    vectors = await fake.embed(texts(count))

    assert len(vectors) == count
    assert len(fake.batches) == calls == math.ceil(count / batch_size)
    assert all(len(batch) <= batch_size for batch in fake.batches)
    assert fake.texts == texts(count)


async def test_documents_and_queries_get_their_prefixes() -> None:
    spec = EmbeddingSpec(
        provider="openai_compatible", model="fake", dimension=8, document_prefix="passage: "
    )
    fake = FakeEmbedder(spec, query_prefix="query: ")

    document = await fake.embed(["synthetic text"])
    query = await fake.embed(["synthetic text"], kind="query")

    assert fake.texts == ["passage: synthetic text", "query: synthetic text"]
    assert document != query


async def test_an_empty_text_or_a_bare_string_is_refused_before_the_model() -> None:
    fake = FakeEmbedder(SPEC)

    with pytest.raises(ValueError, match="empty text"):
        await fake.embed(["fine", ""])
    with pytest.raises(ValueError, match="not one string"):
        await fake.embed("a string is a sequence of strings")

    assert fake.batches == []


class _Broken(BatchingEmbedder):
    """Returns whatever the test puts in ``answer``."""

    def __init__(self, answer: list[list[float]]) -> None:
        super().__init__(SPEC, batch_size=8)
        self.answer = answer

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        return self.answer


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ([[1.0] * 4], "dimension 4, but embedding_dimension is 8; set MEMINI_EMBEDDING_DIMENSION"),
        ([[0.0] * 8], "zero vector"),
        ([[math.nan] + [1.0] * 7], "not finite"),
        ([[math.inf] + [1.0] * 7], "not finite"),
        ([[1.0] * 8, [1.0] * 8], "returned 2 vectors for 1 texts"),
    ],
)
async def test_unusable_model_output_is_an_embedding_error(
    answer: list[list[float]], message: str
) -> None:
    with pytest.raises(EmbeddingError, match=message):
        await _Broken(answer).embed(["synthetic"])


def test_finish_vector_scales_to_unit_length() -> None:
    vector = finish_vector([3.0, 4.0], EmbeddingSpec("openai_compatible", "m", 2))

    assert vector == array("f", [0.6, 0.8]).tolist()


async def test_the_fake_can_fail_and_records_close() -> None:
    fake = FakeEmbedder(SPEC, fail_with=EmbeddingError("synthetic failure"))

    with pytest.raises(EmbeddingError, match="synthetic failure"):
        await fake.embed(["synthetic"])
    async with fake:
        pass

    assert fake.closed
    assert len(fake.batches) == 1


def test_fake_vectors_are_fixed() -> None:
    """Pinned, so a test that stores fake vectors can rely on them across versions."""
    assert fake_vector("synthetic", 2) == [0.046844482421875, -0.225067138671875]


# --- Spec ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"provider": "openai_compatible", "model": "", "dimension": 8},
        {"provider": "openai_compatible", "model": "m", "dimension": 0},
        {"provider": "openai_compatible", "model": "m", "dimension": 16001},
        {"provider": "local", "model": "org/m", "dimension": 8},
        {"provider": "local", "model": "org/m", "dimension": 8, "pooling": "mean"},
        {"provider": "openai_compatible", "model": "m", "dimension": 8, "pooling": "mean"},
    ],
)
def test_an_inconsistent_spec_is_refused(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"embedding|pooling"):
        EmbeddingSpec(**arguments)  # pyright: ignore[reportArgumentType]


def test_a_spec_describes_itself_without_secrets() -> None:
    spec = EmbeddingSpec(
        provider="local",
        model="org/model",
        dimension=384,
        digest="sha256:" + "ab" * 32,
        pooling="mean",
        max_tokens=512,
        document_prefix="passage: ",
    )

    assert spec.describe() == (
        "model org/model (local, dimension 384, digest sha256:ababababababa..., mean pooling,"
        " 512 tokens, document prefix 'passage: ')"
    )


# --- Embedding only what is new (principle 6) ----------------------------------------------------


def test_text_hash_is_sha256_of_the_utf8_text() -> None:
    assert text_hash("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert text_hash("Grüße") != text_hash("Grusse")


async def test_unchanged_content_is_never_re_embedded() -> None:
    """Acceptance check: a counting fake shows that only new or changed texts reach the model."""
    fake = FakeEmbedder(SPEC, batch_size=2)
    store = VectorStore()
    first = ["def alpha(): pass", "def beta(): pass", "def gamma(): pass"]

    result = await embed_missing(fake, first, store.lookup)
    store.store(result.vectors)
    assert (result.embedded, result.reused) == (3, 0)
    assert fake.texts == first
    assert len(fake.batches) == 2

    fake.reset()
    again = await embed_missing(fake, first, store.lookup)
    assert (again.embedded, again.reused) == (0, 3)
    assert fake.batches == []
    assert again.vectors == result.vectors

    fake.reset()
    changed = [
        "def alpha(): pass",
        "def beta(): return 1",
        "def gamma(): pass",
        "def delta(): pass",
    ]
    third = await embed_missing(fake, changed, store.lookup)
    assert fake.texts == ["def beta(): return 1", "def delta(): pass"]
    assert (third.embedded, third.reused) == (2, 2)
    assert third.vector("def alpha(): pass") == result.vector("def alpha(): pass")


async def test_a_repeated_text_is_embedded_once() -> None:
    fake = FakeEmbedder(SPEC)

    result = await embed_missing(fake, ["same", "other", "same", "same"])

    assert fake.texts == ["same", "other"]
    assert result.embedded == 2
    assert set(result.vectors) == {text_hash("same"), text_hash("other")}


async def test_reused_and_new_vectors_equal_what_the_model_returns() -> None:
    fake = FakeEmbedder(SPEC)
    store = VectorStore()
    store.store((await embed_missing(fake, ["one", "two"])).vectors)

    result = await embed_missing(fake, ["one", "two", "three"], store.lookup)

    direct = await FakeEmbedder(SPEC).embed(["one", "two", "three"])
    assert [result.vector(text) for text in ("one", "two", "three")] == direct


async def test_the_lookup_is_asked_once_with_each_distinct_hash() -> None:
    store = VectorStore()

    await embed_missing(FakeEmbedder(SPEC), ["a", "b", "a"], store.lookup)
    await embed_missing(FakeEmbedder(SPEC), [], store.lookup)

    assert store.lookups == [[text_hash("a"), text_hash("b")]]


async def test_vectors_the_lookup_was_not_asked_for_are_ignored() -> None:
    async def lookup(hashes: Sequence[str]) -> Mapping[str, Vector]:
        return {text_hash("unrelated"): [1.0] + [0.0] * 7}

    result = await embed_missing(FakeEmbedder(SPEC), ["asked"], lookup)

    assert set(result.vectors) == {text_hash("asked")}


async def test_a_lookup_from_another_vector_space_is_refused() -> None:
    async def lookup(hashes: Sequence[str]) -> Mapping[str, Vector]:
        return {key: [1.0, 0.0] for key in hashes}

    with pytest.raises(ValueError, match="scoped to the current embedding key"):
        await embed_missing(FakeEmbedder(SPEC), ["synthetic"], lookup)


async def test_queries_can_go_through_embed_missing_with_their_prefix() -> None:
    fake = FakeEmbedder(SPEC, query_prefix="query: ")

    await embed_missing(fake, ["find me"], kind="query")

    assert fake.texts == ["query: find me"]
