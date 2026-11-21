"""Reranking with a cross-encoder run in the process: the ``local`` provider.

The default model is Qwen3-Reranker 0.6B (docs/requirements.md, section 9, Reranker), a causal
language model asked one question per hit: does this document meet the query, yes or no? The prompt
is the model's own template::

    <|im_start|>system
    Judge whether the Document meets the requirements based on the Query and the Instruct
    provided. Note that the answer can only be "yes" or "no".<|im_end|>
    <|im_start|>user
    <Instruct>: {instruction}
    <Query>: {query}
    <Document>: {header}\\n{snippet}<|im_end|>
    <|im_start|>assistant
    <think>\\n\\n</think>\\n\\n

and a hit's score is the probability of "yes" against "no" as the next token: the sigmoid of the
difference of the two logits. The model file computes only those two logits, ``[no, yes]``, at
the last token, not the whole vocabulary at every position. The instruction is the task the
embedding model's queries carry (:data:`memini.config.DEFAULT_MODEL_SETTINGS`).

The query and the hit are data (principle 11): they are tokenized with special tokens off, so a
``<|im_end|>`` in a document is plain text and cannot close the template. The prompt is cut to
``max_tokens``: the end of the document goes first, the end of the template always stays.

One pair per model run, never a padded batch. The model gives every sequence positions from 0, so
in a left-padded batch a pair's positions, and its score, would depend on the longest pair beside
it. The upstream graph also scales int8 activations over the whole batch: a "yes" logit of
20.42 alone and 21.24 beside one other pair. The Linux graphs use either per-token activation
scales or blockwise integer projections, depending on the CPU. Each run hops to a worker
thread, so the event loop stays free and the timeout is checked between pairs. One search reranks
at a time: onnxruntime uses every core it may for one run already.

It needs the optional dependencies ``memini[local-embeddings]`` (onnxruntime, tokenizers, numpy),
imported on first use: onnxruntime with its telemetry off, with no more threads than a CPU limit
of the container allows, and at most :data:`THREADS` unless set (:mod:`memini.embeddings.runtime`).
The model files are verified against the pinned digest before they are loaded, and fetched at the
pinned revision when missing (:func:`memini.embeddings.model_files.ensure_model_files`): files that
do not match are never loaded.
"""

import gzip
import hashlib
import importlib
import math
import platform
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from importlib.resources import files
from pathlib import Path
from typing import Any, Final

import anyio
import anyio.to_thread
import httpx2

from memini.embeddings import EmbeddingError
from memini.embeddings.local import LOCAL_EXTRA, RUNTIME_MODULES
from memini.embeddings.model_files import HUB_URL, TOKENIZER_FILE, ensure_model_files
from memini.embeddings.runtime import (
    model_threads,
    native_output_logged,
    quiet_runtime,
    session_options,
)
from memini.search import Hit

__all__ = ["INSTRUCTION", "THREADS", "LocalReranker", "RerankError", "relevance"]

INSTRUCTION: Final = (
    "Given a search query, retrieve the relevant code or documentation that can help answer the"
    " query"
)
"""What the model is told the query is for: the task of the embedding model's queries."""

THREADS: Final = 4
"""The most threads one model run takes unless set (``search_reranker_threads``). onnxruntime
would take one per core, and every step of a run waits for the slowest thread: on a 16-core
machine shared with other work, 4 threads reranked 3.6 times as fast as 16 (docs/requirements.md,
section 9, Reranker)."""

_PREFIX: Final = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and"
    ' the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
    "<|im_start|>user\n"
)
_SUFFIX: Final = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_INPUTS: Final = frozenset({"input_ids", "attention_mask"})
_ERROR_LIMIT: Final = 300

# The graph references byte offsets in this specific verified model. Keep this pin independent
# of the configurable service default, so updating that default cannot select incompatible weights.
_LINUX_FILES_DIGEST: Final = (
    "sha256:eaa6edfadd2440e4f80851b3ff7701f26e8ec6aacaa2fedcee8ce86afd09afea"
)


@dataclass(frozen=True, slots=True)
class _Graph:
    label: str
    file: str
    sha256: str


_PER_TOKEN: Final = _Graph(
    "per-token INT8",
    "qwen3-per-token-int8.onnx.gz",
    "7f8cf431d2c1a36b251357dbff046a2b28480e2ce5bf45b76c81def591f49111",
)
_LEGACY_X86: Final = _Graph(
    "blockwise INT8",
    "qwen3-blockwise-int8.onnx.gz",
    "5eb524d5d293f14cdeae8486bc896a76b7748349ccbf10016fcd499b4dc52b13",
)


class RerankError(Exception):
    """The reranker could not rerank: its runtime or files are missing or do not match, the model
    failed, or it took too long. The message never quotes a query or a hit."""


def has_avx512_vnni(cpuinfo: Path = Path("/proc/cpuinfo")) -> bool:
    """Prefer the native INT8 kernel when every listed CPU supports AVX-512 VNNI."""
    try:
        lines = cpuinfo.read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return False
    fields = (line.partition(":") for line in lines)
    flags = [value.split() for key, _, value in fields if key.strip() == "flags"]
    return bool(flags) and all("avx512_vnni" in features for features in flags)


def _execution_graph(digest: str) -> _Graph | None:
    if digest != _LINUX_FILES_DIGEST or sys.platform != "linux":
        return None
    machine = platform.machine()
    if machine == "x86_64":
        return _PER_TOKEN if has_avx512_vnni() else _LEGACY_X86
    return _PER_TOKEN if machine == "aarch64" else None


def relevance(no: float, yes: float) -> float:
    """The probability of "yes" against "no" from their logits: ``1 / (1 + e^(no - yes))``."""
    difference = yes - no
    if difference >= 0:
        return 1.0 / (1.0 + math.exp(-difference))
    odds = math.exp(difference)
    return odds / (1.0 + odds)


class LocalReranker:
    """A :class:`~memini.search.Reranker` running an ONNX cross-encoder in the process.

    ``files_dir`` holds (or will hold) ``model_file`` and ``tokenizer.json``, pinned by ``digest``;
    ``revision`` is the commit to fetch missing files at. ``timeout`` bounds one call of
    :meth:`rerank`, waiting for another search's reranking included; ``download_timeout`` bounds
    each request of a download. ``threads`` is the number of threads of one model run; 0 lets
    :func:`memini.embeddings.runtime.model_threads` choose, at most :data:`THREADS`. ``hub_url``,
    ``transport`` and ``scorer`` are for tests; a ``scorer`` scores a query and a document
    (``header\nsnippet``) in place of the model, which is then never loaded.
    """

    def __init__(
        self,
        *,
        model: str,
        files_dir: Path,
        model_file: str,
        digest: str,
        revision: str | None,
        max_tokens: int = 1024,
        timeout: float = 5.0,
        threads: int = 0,
        instruction: str = INSTRUCTION,
        download_timeout: float = 600.0,
        hub_url: str = HUB_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        scorer: Callable[[str, str], float] | None = None,
    ) -> None:
        if max_tokens < 64 or not (math.isfinite(timeout) and timeout > 0) or threads < 0:
            raise ValueError("needs max_tokens >= 64, a timeout above 0 and threads >= 0")
        self._model = model
        self._files_dir = files_dir
        self._model_file = model_file
        self._digest = digest
        self._graph = _execution_graph(digest)
        self._revision = revision
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._threads = threads
        self._instruction = instruction
        self._download_timeout = download_timeout
        self._hub_url = hub_url
        self._transport = transport
        self._load_lock = anyio.Lock()
        self._run_lock = anyio.Lock()
        self._fixed = scorer
        self._score: Callable[[str, str], float] | None = scorer

    @property
    def files_dir(self) -> Path:
        """Where the model files are, or will be fetched to."""
        return self._files_dir

    @property
    def threads(self) -> int:
        """The threads of one model run: as set, or as chosen when the model was loaded (0 before
        that)."""
        return self._threads

    def describe(self) -> str:
        """The model, file, digest, token limit and optional Linux graph, in one line."""
        graph = f", {self._graph.label} {self._graph.sha256[:12]}" if self._graph else ""
        return (
            f"{self._model} {self._model_file} {self._digest[:19]}, at most"
            f" {self._max_tokens} tokens{graph}"
        )

    async def load(self) -> None:
        """Import the runtime, verify (or fetch) the model files and load them. Idempotent.

        Called by the first :meth:`rerank`; the service calls it at start, in the background.

        Raises:
            RerankError: the optional dependencies are missing, the files are missing or do not
                match the digest, or the model cannot be loaded.
        """
        async with self._load_lock:
            if self._score is not None:
                return
            modules = _import_runtime()  # before a download that would be of no use without it
            try:
                await ensure_model_files(
                    directory=self._files_dir,
                    model=self._model,
                    names=(self._model_file, TOKENIZER_FILE),
                    digest=self._digest,
                    revision=self._revision,
                    hub_url=self._hub_url,
                    timeout=self._download_timeout,
                    transport=self._transport,
                    settings=("search_reranker_digest", "search_reranker_revision"),
                )
            except EmbeddingError as error:
                raise RerankError(str(error)) from None
            threads = self._threads or model_threads(THREADS)
            load = partial(
                _Runtime.load,
                modules,
                self._files_dir / self._model_file,
                self._files_dir / TOKENIZER_FILE,
                instruction=self._instruction,
                max_tokens=self._max_tokens,
                threads=threads,
                graph=self._graph,
            )
            try:
                self._score = (await anyio.to_thread.run_sync(load)).score
                self._threads = threads
            except RerankError:
                raise
            except Exception as error:
                where = f"reranker {self._model} from {self._files_dir}"
                raise RerankError(f"cannot load {where}: {_describe(error)}") from None

    async def rerank(self, query: str, hits: Sequence[Hit]) -> list[Hit]:
        """``hits`` by the model's relevance, best first, each with its relevance as ``score``
        (0 to 1). Equal relevance keeps the order given.

        Raises:
            RerankError: :meth:`load` failed, the model failed, or reranking took longer than the
                timeout.
        """
        if not hits:
            return []
        await self.load()
        scorer = self._score
        if scorer is None:  # load() either sets it or raises
            raise RerankError(f"reranker {self._model} is not loaded")
        documents = [f"{hit.header}\n{hit.snippet}" for hit in hits]
        scores: list[float] = []
        try:
            with anyio.fail_after(self._timeout):
                async with self._run_lock:
                    for document in documents:
                        score = partial(scorer, query, document)
                        scores.append(await anyio.to_thread.run_sync(score))
        except TimeoutError:
            raise RerankError(
                f"reranking {len(hits)} hits took longer than {self._timeout:g} s"
                " (search_reranker_timeout)"
            ) from None
        except Exception as error:
            raise RerankError(f"reranker {self._model} failed: {_describe(error)}") from None
        order = sorted(range(len(hits)), key=lambda i: -scores[i])
        return [replace(hits[i], score=scores[i]) for i in order]

    async def aclose(self) -> None:
        self._score = self._fixed


def _describe(error: Exception) -> str:
    message = " ".join(str(error).split())[:_ERROR_LIMIT]
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


@dataclass(frozen=True, slots=True)
class _Modules:
    numpy: Any
    onnxruntime: Any
    tokenizers: Any


def _import_runtime() -> _Modules:
    """The optional modules, imported now, onnxruntime without telemetry
    (:mod:`memini.embeddings.runtime`); a :class:`RerankError` naming the extra if absent."""
    quiet_runtime()
    loaded: dict[str, Any] = {}
    for name in RUNTIME_MODULES:
        try:
            with native_output_logged():  # its first import writes past the JSON log
                loaded[name] = importlib.import_module(name)
        except ImportError:
            raise RerankError(
                f"the local reranker needs the optional dependencies memini[{LOCAL_EXTRA}]"
                f" ({name} is missing): install them with `pip install 'memini[{LOCAL_EXTRA}]'`,"
                f" or `uv sync --extra {LOCAL_EXTRA}` in a checkout"
            ) from None
    return _Modules(**loaded)


class _Runtime:
    """A loaded model and tokenizer. Its methods block; call them in a worker thread."""

    def __init__(
        self,
        numpy: Any,
        session: Any,
        text: Any,
        *,
        prefix: list[int],
        suffix: list[int],
        instruction: str,
        max_tokens: int,
    ) -> None:
        self._np = numpy
        self._session = session
        self._text = text
        self._prefix = prefix
        self._suffix = suffix
        self._instruction = instruction
        self._budget = max_tokens - len(prefix) - len(suffix)

    @classmethod
    def load(
        cls,
        modules: _Modules,
        model_path: Path,
        tokenizer_path: Path,
        *,
        instruction: str,
        max_tokens: int,
        threads: int,
        graph: _Graph | None,
    ) -> "_Runtime":
        ort = modules.onnxruntime
        options = session_options(ort, threads)
        source: bytes | str = str(model_path)
        if graph is not None:
            source = gzip.decompress(files("memini.rerank").joinpath(graph.file).read_bytes())
            if hashlib.sha256(source).hexdigest() != graph.sha256:
                raise RerankError(f"packaged reranker graph {graph.file} failed SHA-256")
            options.add_session_config_entry(
                "session.model_external_initializers_file_folder_path", str(model_path.parent)
            )
        session = ort.InferenceSession(
            source, sess_options=options, providers=["CPUExecutionProvider"]
        )
        inputs = frozenset(str(item.name) for item in session.get_inputs())
        outputs = session.get_outputs()
        width = outputs[0].shape[-1] if outputs and outputs[0].shape else None
        if inputs != _INPUTS or width != 2:
            raise RerankError(
                f"{model_path.name} takes {', '.join(sorted(inputs))} and gives"
                f" {width} values; the local reranker gives input_ids and attention_mask and reads"
                " two logits, [no, yes]"
            )
        tokenizer_class = modules.tokenizers.Tokenizer
        template = tokenizer_class.from_file(str(tokenizer_path))
        text = tokenizer_class.from_file(str(tokenizer_path))
        for tokenizer in (template, text):
            tokenizer.no_truncation()  # cut by hand: the end of the template must stay
            tokenizer.no_padding()  # one pair per run: nothing to pad
        text.encode_special_tokens = True  # a query or document is data, never template
        return cls(
            modules.numpy,
            session,
            text,
            prefix=template.encode(_PREFIX, add_special_tokens=False).ids,
            suffix=template.encode(_SUFFIX, add_special_tokens=False).ids,
            instruction=instruction,
            max_tokens=max_tokens,
        )

    def ids(self, query: str, document: str) -> list[int]:
        """The prompt of one pair, cut to the token limit at the end of the document."""
        body = f"<Instruct>: {self._instruction}\n<Query>: {query}\n<Document>: {document}"
        ids: list[int] = self._text.encode(body, add_special_tokens=False).ids
        return self._prefix + ids[: self._budget] + self._suffix

    def score(self, query: str, document: str) -> float:
        np = self._np
        ids = self.ids(query, document)
        feeds = {
            "input_ids": np.array([ids], dtype=np.int64),
            "attention_mask": np.ones((1, len(ids)), dtype=np.int64),
        }
        no, yes = (float(value) for value in self._session.run(None, feeds)[0][0])
        return relevance(no, yes)
