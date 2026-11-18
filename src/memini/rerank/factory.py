"""From settings to a reranker."""

import os
from collections.abc import Mapping
from pathlib import Path

from memini.config import Settings
from memini.embeddings.model_files import model_files_dir
from memini.rerank.local import LocalReranker

__all__ = ["create_reranker", "default_model_dir", "local_reranker"]


def default_model_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Where reranker files live when ``search_reranker_model_dir`` is not set: the user's cache.

    ``$XDG_CACHE_HOME/memini/models`` if that variable holds an absolute path, otherwise
    ``~/.cache/memini/models``. The files of each pinned digest get a directory of their own in
    it (:func:`memini.embeddings.model_files.model_files_dir`).
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    cache_home = env.get("XDG_CACHE_HOME", "")
    base = Path(cache_home) if os.path.isabs(cache_home) else Path.home() / ".cache"
    return base / "memini" / "models"


def local_reranker(settings: Settings) -> LocalReranker:
    """The local reranker the ``search_reranker_*`` settings describe, whether ``search_reranker``
    is set or not: ``memini eval --reranker local`` measures it either way. Cheap: the model
    loads on first use."""
    root = settings.search_reranker_model_dir or default_model_dir()
    return LocalReranker(
        model=settings.search_reranker_model,
        files_dir=model_files_dir(
            root, settings.search_reranker_model, settings.search_reranker_digest
        ),
        model_file=settings.search_reranker_model_file,
        digest=settings.search_reranker_digest,
        revision=settings.search_reranker_revision,
        max_tokens=settings.search_reranker_max_tokens,
        timeout=settings.search_reranker_timeout,
    )


def create_reranker(settings: Settings) -> LocalReranker | None:
    """The service's reranker, or ``None`` when ``search_reranker`` is not set. The caller loads
    it (``load()``) and closes it (``aclose()``)."""
    if settings.search_reranker is None:
        return None
    return local_reranker(settings)
