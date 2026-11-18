"""From settings to an embedder: none configured, an OpenAI-compatible endpoint, a local model."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from memini.config import Settings, load_settings
from memini.embeddings import (
    EmbeddingError,
    EmbeddingsNotConfiguredError,
    EmbeddingSpec,
    LocalEmbedder,
    OpenAICompatibleEmbedder,
    create_embedder,
    embedding_spec,
)
from memini.embeddings.model_files import model_files_dir
from memini.logs import clear_secrets, redact

pytestmark = pytest.mark.anyio

FAKE_URL = "postgresql://fake:fake@db.invalid:5432/memini"
FAKE_KEY = "sk-fake-factory-key-not-real-0011"
DIGEST = "sha256:" + "cd" * 32


@pytest.fixture(autouse=True)
def _forget_secrets() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    clear_secrets()


def test_without_a_provider_both_refuse_with_the_setting_to_set(settings: Settings) -> None:
    with pytest.raises(EmbeddingsNotConfiguredError, match="set MEMINI_EMBEDDING_PROVIDER"):
        embedding_spec(settings)
    with pytest.raises(EmbeddingsNotConfiguredError):
        create_embedder(settings)


async def test_an_openai_compatible_provider(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_EMBEDDING_PROVIDER": "openai_compatible",
            "MEMINI_EMBEDDING_MODEL": "synthetic-embed:v1",
            "MEMINI_EMBEDDING_DIMENSION": "768",
            "MEMINI_EMBEDDING_DIGEST": "synthetic-digest",
            "MEMINI_EMBEDDING_DOCUMENT_PREFIX": "search_document: ",
            "MEMINI_EMBEDDING_URL": "http://127.0.0.1:11434/v1",
            "MEMINI_EMBEDDING_API_KEY": FAKE_KEY,
            "MEMINI_EMBEDDING_BATCH_SIZE": "8",
        }
    )

    embedder = create_embedder(settings)

    assert isinstance(embedder, OpenAICompatibleEmbedder)
    assert (
        embedder.spec
        == embedding_spec(settings)
        == EmbeddingSpec(
            provider="openai_compatible",
            model="synthetic-embed:v1",
            dimension=768,
            digest="synthetic-digest",
            document_prefix="search_document: ",
        )
    )
    assert embedder.batch_size == 8
    assert embedder.target == "http://127.0.0.1:11434"
    assert redact(f"key {FAKE_KEY}") == "key [REDACTED]"
    await embedder.aclose()


def test_a_local_provider_loads_nothing_until_used(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_EMBEDDING_PROVIDER": "local",
            "MEMINI_EMBEDDING_MODEL": "synthetic-org/synthetic-model",
            "MEMINI_EMBEDDING_DIMENSION": "384",
            "MEMINI_EMBEDDING_DIGEST": DIGEST,
            "MEMINI_EMBEDDING_MODEL_DIR": str(tmp_path),
            "MEMINI_EMBEDDING_MAX_TOKENS": "256",
        }
    )

    embedder = create_embedder(settings)

    assert isinstance(embedder, LocalEmbedder)
    assert embedder.spec == EmbeddingSpec(
        provider="local",
        model="synthetic-org/synthetic-model",
        dimension=384,
        digest=DIGEST,
        pooling="mean",
        max_tokens=256,
    )
    assert embedder.files_dir == model_files_dir(tmp_path, "synthetic-org/synthetic-model", DIGEST)
    assert list(tmp_path.iterdir()) == []


def test_settings_built_by_hand_without_a_required_value_are_refused() -> None:
    settings = Settings(
        database_url=SecretStr(FAKE_URL),
        embedding_provider="openai_compatible",
        embedding_model="m",
        embedding_dimension=8,
    )

    with pytest.raises(EmbeddingError, match="set MEMINI_EMBEDDING_URL"):
        create_embedder(settings)
