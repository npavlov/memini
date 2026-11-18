"""From settings to an embedder, and to the spec that pins its vectors."""

from memini.config import Settings
from memini.embeddings.base import (
    Embedder,
    EmbeddingError,
    EmbeddingsNotConfiguredError,
    EmbeddingSpec,
)
from memini.embeddings.local import LocalEmbedder
from memini.embeddings.model_files import model_files_dir
from memini.embeddings.openai_compatible import OpenAICompatibleEmbedder

__all__ = ["create_embedder", "embedding_spec"]


def embedding_spec(settings: Settings) -> EmbeddingSpec:
    """The spec of the configured embedder, without creating it: what search compares the index
    fingerprint with. Loads no model and opens no connection.

    Raises:
        EmbeddingsNotConfiguredError: ``embedding_provider`` is not set.
        EmbeddingError: a setting the provider needs is missing; :func:`memini.config.load_settings`
            refuses such settings, so only settings built by hand get here.
    """
    provider = settings.embedding_provider
    if provider is None:
        raise EmbeddingsNotConfiguredError()
    model = _required(settings.embedding_model, "embedding_model")
    dimension = _required(settings.embedding_dimension, "embedding_dimension")
    digest = settings.embedding_digest or ""
    if provider == "local":
        return EmbeddingSpec(
            provider="local",
            model=model,
            dimension=dimension,
            digest=_required(settings.embedding_digest, "embedding_digest"),
            pooling=settings.embedding_pooling,
            max_tokens=settings.embedding_max_tokens,
            document_prefix=settings.embedding_document_prefix,
        )
    return EmbeddingSpec(
        provider="openai_compatible",
        model=model,
        dimension=dimension,
        digest=digest,
        document_prefix=settings.embedding_document_prefix,
    )


def create_embedder(settings: Settings) -> Embedder:
    """The embedder the settings describe. Cheap: a local model loads on first use, an HTTP
    connection opens on the first request. The caller closes it with ``aclose()``.

    An API key is registered with :func:`memini.logs.register_secret`, so logs mask it.

    Raises:
        EmbeddingsNotConfiguredError: ``embedding_provider`` is not set.
        EmbeddingError: a setting the provider needs is missing (settings built by hand only).
    """
    spec = embedding_spec(settings)
    if spec.provider == "local":
        root = _required(settings.embedding_model_dir, "embedding_model_dir")
        return LocalEmbedder(
            spec,
            files_dir=model_files_dir(root, spec.model, spec.digest),
            model_file=settings.embedding_model_file,
            revision=settings.embedding_revision,
            batch_size=settings.embedding_batch_size,
            query_prefix=settings.embedding_query_prefix,
            timeout=settings.embedding_timeout,
        )
    return OpenAICompatibleEmbedder(
        spec,
        url=_required(settings.embedding_url, "embedding_url"),
        api_key=settings.embedding_api_key,
        batch_size=settings.embedding_batch_size,
        timeout=settings.embedding_timeout,
        query_prefix=settings.embedding_query_prefix,
    )


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise EmbeddingError(
            f"embedding_provider is set, but {name} is not: set MEMINI_{name.upper()}"
        )
    return value
