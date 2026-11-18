"""Reranking: a cross-encoder reorders the best hits of a ranked search (optional, off by default).

- :class:`LocalReranker` runs an ONNX cross-encoder in the process, by default Qwen3-Reranker
  0.6B; it needs the optional dependencies ``memini[local-embeddings]`` and verifies the model
  files against the pinned digest first. It implements :class:`memini.search.Reranker`.
- :func:`create_reranker` builds the configured one from the settings (``None`` while
  ``search_reranker`` is not set); :func:`local_reranker` builds it in any case, for the eval.

:func:`memini.search.search` hands the reranker the best ``search_rerank_depth`` hits of a
``hybrid``, ``dense`` or ``lexical`` search, and keeps the fused order if it raises. On the code
suite it lifts hybrid R@1 from 0.61 to 0.78 and MRR from 0.77 to 0.88 at depth 5, for about 0.5 s
per search on an Apple M4 Max; it is off by default because it needs the optional runtime and a
600 MB download (docs/requirements.md, section 9, Reranker).
"""

from memini.rerank.factory import create_reranker, default_model_dir, local_reranker
from memini.rerank.local import INSTRUCTION, LocalReranker, RerankError, relevance

__all__ = [
    "INSTRUCTION",
    "LocalReranker",
    "RerankError",
    "create_reranker",
    "default_model_dir",
    "local_reranker",
    "relevance",
]
