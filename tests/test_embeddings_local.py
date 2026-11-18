"""The local provider: model files, their digest, fetching them pinned, and the optional extra.

No test downloads a model: fetching runs against an in-process fake hub (httpx2.MockTransport) with
small synthetic files. One test runs a real model, and only when the extra is installed and
``MEMINI_TEST_LOCAL_MODEL_DIR`` points at a model already on disk.
"""

import hashlib
import importlib.util
import math
import os
import sys
from pathlib import Path

import httpx2
import pytest

from memini.embeddings import EmbeddingError, EmbeddingSpec, LocalEmbedder
from memini.embeddings.model_files import (
    MODEL_FILES_DOMAIN,
    TOKENIZER_FILE,
    digest_of_files,
    ensure_model_files,
    files_digest,
    model_files_dir,
)
from memini.ids import hash_fields

pytestmark = pytest.mark.anyio

MODEL = "synthetic-org/synthetic-model"
REVISION = "0123456789abcdef0123456789abcdef01234567"
MODEL_FILE = "onnx/model.onnx"
NAMES = (MODEL_FILE, TOKENIZER_FILE)
HUB = "https://hub.test"
# Synthetic stand-ins for the files of a model; their content is never parsed here.
CONTENT = {MODEL_FILE: b"synthetic onnx bytes", TOKENIZER_FILE: b'{"synthetic": "tokenizer"}'}


def digest_of(content: dict[str, bytes]) -> str:
    return files_digest({name: hashlib.sha256(data).hexdigest() for name, data in content.items()})


DIGEST = digest_of(CONTENT)
WRONG_DIGEST = "sha256:" + "0" * 64


def local_spec(digest: str = DIGEST, dimension: int = 4) -> EmbeddingSpec:
    return EmbeddingSpec(
        provider="local",
        model=MODEL,
        dimension=dimension,
        digest=digest,
        pooling="mean",
        max_tokens=128,
    )


def put(directory: Path, content: dict[str, bytes]) -> None:
    for name, data in content.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class FakeHub:
    """Serves files at {HUB}/{MODEL}/resolve/{REVISION}/{name}, by a redirect as the hub does."""

    def __init__(self, content: dict[str, bytes] = CONTENT) -> None:
        self.content = content
        self.requests: list[str] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.requests.append(url)
        prefix = f"{HUB}/{MODEL}/resolve/{REVISION}/"
        if url.startswith(prefix):
            return httpx2.Response(302, headers={"Location": f"{HUB}/storage/{url[len(prefix) :]}"})
        name = url.removeprefix(f"{HUB}/storage/")
        if name in self.content:
            return httpx2.Response(200, content=self.content[name])
        return httpx2.Response(404)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)


async def ensure(
    directory: Path, hub: FakeHub, *, digest: str = DIGEST, revision: str | None = REVISION
) -> None:
    await ensure_model_files(
        directory=directory,
        model=MODEL,
        names=NAMES,
        digest=digest,
        revision=revision,
        hub_url=HUB,
        transport=hub.transport(),
    )


# --- The digest ----------------------------------------------------------------------------------


def test_the_digest_covers_paths_and_contents_in_path_order() -> None:
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in CONTENT.items()}
    fields = [
        field for name in sorted(hashes) for field in (name.encode(), bytes.fromhex(hashes[name]))
    ]

    assert "sha256:" + hash_fields(MODEL_FILES_DOMAIN, *fields) == DIGEST
    assert files_digest(dict(reversed(hashes.items()))) == DIGEST
    assert digest_of({"onnx/model_quantized.onnx": CONTENT[MODEL_FILE]}) != digest_of(
        {MODEL_FILE: CONTENT[MODEL_FILE]}
    )
    assert digest_of(CONTENT | {TOKENIZER_FILE: b"{}"}) != DIGEST


async def test_the_digest_of_files_on_disk(tmp_path: Path) -> None:
    put(tmp_path, CONTENT)

    assert await digest_of_files(tmp_path, NAMES) == DIGEST


def test_each_pinned_digest_has_its_own_directory(tmp_path: Path) -> None:
    assert model_files_dir(tmp_path, "org/name", "sha256:" + "0123456789abcdef" * 4) == (
        tmp_path / "org--name" / "sha256-0123456789abcdef"
    )


# --- Present files are verified; missing files are fetched, then verified ------------------------


async def test_matching_files_are_accepted_without_a_download(tmp_path: Path) -> None:
    put(tmp_path, CONTENT)
    hub = FakeHub()

    await ensure(tmp_path, hub)

    assert hub.requests == []


async def test_files_that_do_not_match_are_refused_and_left_alone(tmp_path: Path) -> None:
    tampered = CONTENT | {MODEL_FILE: b"tampered onnx bytes"}
    put(tmp_path, tampered)
    hub = FakeHub()

    with pytest.raises(EmbeddingError) as info:
        await ensure(tmp_path, hub)

    message = str(info.value)
    assert f"have digest {digest_of(tampered)}, but embedding_digest is {DIGEST}" in message
    assert "refusing to load them" in message
    assert hub.requests == []
    assert (tmp_path / MODEL_FILE).read_bytes() == b"tampered onnx bytes"


async def test_missing_files_without_a_revision_are_an_error(tmp_path: Path) -> None:
    with pytest.raises(EmbeddingError) as info:
        await ensure(tmp_path / "absent", FakeHub(), revision=None)

    message = str(info.value)
    assert "MEMINI_EMBEDDING_REVISION" in message
    assert str(tmp_path / "absent") in message


async def test_missing_files_are_fetched_at_the_pinned_revision(tmp_path: Path) -> None:
    hub = FakeHub()
    target = tmp_path / "models" / "pinned"

    await ensure(target, hub)

    assert [url for url in hub.requests if "/resolve/" in url] == [
        f"{HUB}/{MODEL}/resolve/{REVISION}/{name}" for name in NAMES
    ]
    assert {name: (target / name).read_bytes() for name in NAMES} == CONTENT
    assert sorted(path.name for path in target.parent.iterdir()) == ["pinned"]  # no staging left


async def test_fetched_files_that_do_not_match_are_never_put_in_place(tmp_path: Path) -> None:
    hub = FakeHub(CONTENT | {TOKENIZER_FILE: b'{"substituted": true}'})
    target = tmp_path / "models" / "pinned"

    with pytest.raises(EmbeddingError, match="refusing to use them"):
        await ensure(target, hub)

    assert not target.exists()
    assert list(target.parent.iterdir()) == []


async def test_a_failed_download_names_the_file_and_leaves_nothing(tmp_path: Path) -> None:
    hub = FakeHub({MODEL_FILE: CONTENT[MODEL_FILE]})  # no tokenizer on the hub
    target = tmp_path / "models" / "pinned"

    with pytest.raises(EmbeddingError, match=f"cannot fetch {TOKENIZER_FILE} of model .*HTTP 404"):
        await ensure(target, hub)

    assert list(target.parent.iterdir()) == []


# --- The embedder --------------------------------------------------------------------------------


async def test_without_the_extra_the_error_names_it_and_nothing_is_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # makes `import onnxruntime` fail
    hub = FakeHub()
    embedder = LocalEmbedder(
        local_spec(), files_dir=tmp_path, revision=REVISION, hub_url=HUB, transport=hub.transport()
    )

    with pytest.raises(EmbeddingError) as info:
        await embedder.embed(["synthetic"])

    assert "memini[local-embeddings]" in str(info.value)
    assert "onnxruntime is missing" in str(info.value)
    assert hub.requests == []


@pytest.mark.parametrize(
    "spec",
    [
        EmbeddingSpec(provider="openai_compatible", model="m", dimension=4),
        EmbeddingSpec(provider="local", model=MODEL, dimension=4, pooling="cls", max_tokens=8),
    ],
)
def test_the_embedder_needs_a_local_spec_with_a_digest(spec: EmbeddingSpec, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"local|digest"):
        LocalEmbedder(spec, files_dir=tmp_path)


# --- A real model, only when one is on disk ------------------------------------------------------

LOCAL_MODEL_DIR = os.environ.get("MEMINI_TEST_LOCAL_MODEL_DIR")


@pytest.mark.skipif(
    importlib.util.find_spec("onnxruntime") is None or not LOCAL_MODEL_DIR,
    reason="needs memini[local-embeddings] and MEMINI_TEST_LOCAL_MODEL_DIR (a directory with"
    " tokenizer.json and the model file; MEMINI_TEST_LOCAL_MODEL_FILE, default onnx/model.onnx;"
    " MEMINI_TEST_LOCAL_MODEL_DIMENSION, default 384)",
)
async def test_a_real_model_embeds_deterministically() -> None:
    directory = Path(LOCAL_MODEL_DIR or "")
    model_file = os.environ.get("MEMINI_TEST_LOCAL_MODEL_FILE", "onnx/model.onnx")
    dimension = int(os.environ.get("MEMINI_TEST_LOCAL_MODEL_DIMENSION", "384"))
    digest = await digest_of_files(directory, (model_file, TOKENIZER_FILE))
    spec = local_spec(digest=digest, dimension=dimension)
    texts = ["The cat sits on the mat.", "A cat is on a rug.", "Revenue grew by ten percent."]

    async with LocalEmbedder(spec, files_dir=directory, model_file=model_file, batch_size=2) as one:
        vectors = await one.embed(texts)
        alone = await one.embed(texts[:1])

    assert [len(vector) for vector in vectors] == [dimension] * 3
    assert all(math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-5) for v in vectors)
    assert alone[0] == vectors[0]  # a text's vector does not depend on its batch

    def similarity(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert similarity(vectors[0], vectors[1]) > similarity(vectors[0], vectors[2])
