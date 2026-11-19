"""Embeddings from an ONNX model run in the process: the ``local`` provider.

It needs the optional dependencies ``memini[local-embeddings]``: ``onnxruntime`` runs the model,
``tokenizers`` splits text into tokens, ``numpy`` carries the arrays between them. They add about
170 MB to an install, so the base install goes without them (the image has them, for the
reranker), and this module imports them only when the first text is embedded. Without them, that
first call fails with a message naming the extra.

On first use the embedder makes sure the model files are present and match the pinned digest
(:func:`memini.embeddings.model_files.ensure_model_files`; fetched at the pinned revision if
missing), then loads them: files that do not match are never loaded. Inference runs in a worker
thread, so the event loop stays free; onnxruntime uses the cores it may for each run.

The model runs once per text, never on a padded batch. With a quantized model the scale of the
activations is computed over the whole batch, so a padded batch makes a text's vector depend on
its neighbours (by up to 0.06 per value, measured on an int8 MiniLM; 6e-7 in float32). One run per
text keeps the vector of a text a function of the text alone, and on a CPU it was no slower: no
work is spent on padding. ``batch_size`` is then the number of texts per hop to the worker thread.

onnxruntime is imported with its telemetry off, and a session gets no more threads than a CPU
limit of the container allows (:mod:`memini.embeddings.runtime`).

A text is cut to ``max_tokens`` tokens. The model's first output is either one vector per token,
pooled by ``pooling`` (``mean`` of the tokens, or ``cls``, the first token), or already one vector
per text, used as it is. The model may take ``input_ids``, ``attention_mask`` and
``token_type_ids``; a model that needs any other input is refused.
"""

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Final

import anyio
import anyio.to_thread
import httpx2

from memini.config import EmbeddingPooling
from memini.embeddings.base import BatchingEmbedder, EmbeddingError, EmbeddingSpec
from memini.embeddings.model_files import HUB_URL, TOKENIZER_FILE, ensure_model_files
from memini.embeddings.runtime import quiet_runtime, session_options

__all__ = ["LOCAL_EXTRA", "RUNTIME_MODULES", "LocalEmbedder"]

LOCAL_EXTRA: Final = "local-embeddings"
"""The extra the local provider needs: ``pip install 'memini[local-embeddings]'``."""

RUNTIME_MODULES: Final = ("onnxruntime", "tokenizers", "numpy")
"""What the extra brings, in the order they are imported: the first one missing is named."""

_INPUTS: Final = frozenset({"input_ids", "attention_mask", "token_type_ids"})
_ERROR_LIMIT: Final = 300


class LocalEmbedder(BatchingEmbedder):
    """An :class:`~memini.embeddings.base.Embedder` running an ONNX model in the process.

    ``files_dir`` holds (or will hold) ``model_file`` and ``tokenizer.json``; see
    :func:`memini.embeddings.model_files.model_files_dir` for where the settings put it.
    ``revision`` is the commit to fetch missing files at; without it, missing files are an error.
    ``hub_url`` and ``transport`` are for tests.
    """

    def __init__(
        self,
        spec: EmbeddingSpec,
        *,
        files_dir: Path,
        model_file: str = "onnx/model.onnx",
        revision: str | None = None,
        batch_size: int = 32,
        query_prefix: str = "",
        timeout: float = 60.0,
        hub_url: str = HUB_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if spec.provider != "local" or spec.pooling is None or spec.max_tokens is None:
            raise ValueError("LocalEmbedder needs a spec of provider local")
        if not spec.digest:
            raise ValueError("LocalEmbedder needs the digest of the model files")
        super().__init__(spec, batch_size=batch_size, query_prefix=query_prefix)
        self._pooling: EmbeddingPooling = spec.pooling
        self._max_tokens: int = spec.max_tokens
        self._files_dir = files_dir
        self._model_file = model_file
        self._revision = revision
        self._timeout = timeout
        self._hub_url = hub_url
        self._transport = transport
        self._lock = anyio.Lock()
        self._runtime: _Runtime | None = None

    @property
    def files_dir(self) -> Path:
        """Where the model files are, or will be fetched to."""
        return self._files_dir

    async def load(self) -> None:
        """Import the runtime, verify (or fetch) the model files and load them. Idempotent.

        Called by the first :meth:`embed`; call it at start to fail early.

        Raises:
            EmbeddingError: the optional dependencies are missing, the files are missing or do not
                match the digest, or the model cannot be loaded.
        """
        async with self._lock:
            if self._runtime is not None:
                return
            modules = _import_runtime()  # before a download that would be of no use without it
            await ensure_model_files(
                directory=self._files_dir,
                model=self.spec.model,
                names=(self._model_file, TOKENIZER_FILE),
                digest=self.spec.digest,
                revision=self._revision,
                hub_url=self._hub_url,
                timeout=self._timeout,
                transport=self._transport,
            )
            load = partial(
                _Runtime.load,
                modules,
                self._files_dir / self._model_file,
                self._files_dir / TOKENIZER_FILE,
                pooling=self._pooling,
                max_tokens=self._max_tokens,
            )
            try:
                self._runtime = await anyio.to_thread.run_sync(load)
            except EmbeddingError:
                raise
            except Exception as error:
                where = f"model {self.spec.model} from {self._files_dir}"
                raise EmbeddingError(f"cannot load {where}: {_describe(error)}") from None

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        await self.load()
        runtime = self._runtime
        if runtime is None:  # load() either sets it or raises
            raise EmbeddingError(f"model {self.spec.model} is not loaded")
        try:
            return await anyio.to_thread.run_sync(runtime.embed, texts)
        except Exception as error:
            raise EmbeddingError(f"model {self.spec.model} failed: {_describe(error)}") from None

    async def aclose(self) -> None:
        self._runtime = None


def _describe(error: Exception) -> str:
    message = " ".join(str(error).split())[:_ERROR_LIMIT]
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


@dataclass(frozen=True, slots=True)
class _Modules:
    numpy: Any
    onnxruntime: Any
    tokenizers: Any


def _import_runtime() -> _Modules:
    """The optional modules, imported now, onnxruntime without telemetry; an
    :class:`EmbeddingError` naming the extra if absent."""
    quiet_runtime()
    loaded: dict[str, Any] = {}
    for name in RUNTIME_MODULES:
        try:
            loaded[name] = importlib.import_module(name)
        except ImportError:
            raise EmbeddingError(
                "the local embedding provider needs the optional dependencies"
                f" memini[{LOCAL_EXTRA}] ({name} is missing): install them with"
                f" `pip install 'memini[{LOCAL_EXTRA}]'`, or `uv sync --extra {LOCAL_EXTRA}` in a"
                " checkout"
            ) from None
    return _Modules(**loaded)


class _Runtime:
    """A loaded model and tokenizer. Its methods block; call them in a worker thread."""

    def __init__(
        self,
        numpy: Any,
        session: Any,
        tokenizer: Any,
        inputs: frozenset[str],
        pooling: EmbeddingPooling,
    ) -> None:
        self._np = numpy
        self._session = session
        self._tokenizer = tokenizer
        self._inputs = inputs
        self._pooling = pooling

    @classmethod
    def load(
        cls,
        modules: _Modules,
        model_path: Path,
        tokenizer_path: Path,
        *,
        pooling: EmbeddingPooling,
        max_tokens: int,
    ) -> "_Runtime":
        ort = modules.onnxruntime
        options = session_options(ort)
        session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        inputs = frozenset(str(item.name) for item in session.get_inputs())
        if "input_ids" not in inputs or not inputs <= _INPUTS:
            raise EmbeddingError(
                f"{model_path.name} takes the inputs {', '.join(sorted(inputs))}; the local"
                f" provider gives {', '.join(sorted(_INPUTS))}, and input_ids at least"
            )
        tokenizer = modules.tokenizers.Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_truncation(max_length=max_tokens)
        tokenizer.no_padding()  # one text per run: nothing to pad
        return cls(modules.numpy, session, tokenizer, inputs, pooling)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One model run per text, so a text's vector never depends on the texts beside it."""
        return [self._embed_one(encoding) for encoding in self._tokenizer.encode_batch(texts)]

    def _embed_one(self, encoding: Any) -> list[float]:
        np = self._np
        feeds: dict[str, Any] = {"input_ids": np.array([encoding.ids], dtype=np.int64)}
        if "attention_mask" in self._inputs:
            feeds["attention_mask"] = np.array([encoding.attention_mask], dtype=np.int64)
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.array([encoding.type_ids], dtype=np.int64)
        output = self._session.run(None, feeds)[0][0]
        if output.ndim == 1:  # the model pools by itself
            pooled = output
        elif self._pooling == "cls":
            pooled = output[0]
        else:
            pooled = output.mean(axis=0)
        vector: list[float] = pooled.astype(np.float64).tolist()
        return vector
