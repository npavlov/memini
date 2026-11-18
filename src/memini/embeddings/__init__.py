"""Embeddings: one interface, two providers, and embedding only what is new.

- :class:`Embedder` turns texts into L2-normalized float32 vectors of a fixed dimension, in
  batches. :class:`EmbeddingSpec` is what decides those vectors; it is the embedding part of the
  index fingerprint (:mod:`memini.index`).
- :class:`OpenAICompatibleEmbedder` calls an OpenAI-compatible ``/embeddings`` endpoint (Ollama,
  llama.cpp, vLLM, a cloud service), with retries, backoff and timeouts.
- :class:`LocalEmbedder` runs an ONNX model in the process. It needs the optional dependencies
  ``memini[local-embeddings]`` and verifies the model files against the pinned digest first.
- :func:`create_embedder` builds the configured one from the settings, :func:`embedding_spec` its
  spec alone. With no provider configured both raise :class:`EmbeddingsNotConfiguredError`.
- :func:`embed_missing` embeds only the texts that have no stored vector yet (principle 6).
- :class:`FakeEmbedder` is for tests: deterministic vectors and a record of every batch.

Which model ships as the default, and whether in the process or through Ollama, is open question 2
of the requirements, settled by measurement on the eval set.
"""

from memini.embeddings.base import (
    NORMALIZATION,
    BatchingEmbedder,
    Embedder,
    EmbeddingError,
    EmbeddingsNotConfiguredError,
    EmbeddingSpec,
    TextKind,
    Vector,
)
from memini.embeddings.factory import create_embedder, embedding_spec
from memini.embeddings.fake import FakeEmbedder
from memini.embeddings.local import LOCAL_EXTRA, LocalEmbedder
from memini.embeddings.openai_compatible import OpenAICompatibleEmbedder
from memini.embeddings.reuse import EmbeddedTexts, VectorLookup, embed_missing, text_hash

__all__ = [
    "LOCAL_EXTRA",
    "NORMALIZATION",
    "BatchingEmbedder",
    "EmbeddedTexts",
    "Embedder",
    "EmbeddingError",
    "EmbeddingSpec",
    "EmbeddingsNotConfiguredError",
    "FakeEmbedder",
    "LocalEmbedder",
    "OpenAICompatibleEmbedder",
    "TextKind",
    "Vector",
    "VectorLookup",
    "create_embedder",
    "embed_missing",
    "embedding_spec",
    "text_hash",
]
