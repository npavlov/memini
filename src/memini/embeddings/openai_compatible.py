"""Embeddings from an OpenAI-compatible HTTP API: Ollama, llama.cpp, vLLM, or a cloud service.

One ``POST {url}/embeddings`` per batch with ``{"model": ..., "input": [texts]}``; the answer's
``data[i].embedding`` are the vectors, put in order by ``data[i].index``. The endpoint is the user's
choice: chunk text goes wherever ``embedding_url`` points, so a cloud endpoint is an explicit
opt-in (principle 8). The service logs which host gets the text when the embedder is created.

Failures:

- A connection error, a timeout, HTTP 408, 429, 500, 502, 503 or 504 is retried, up to
  ``max_attempts`` attempts in all, after an exponential backoff with jitter, or after the
  ``Retry-After`` the server asked for (capped at ``backoff_max``).
- Any other non-2xx answer fails at once: a wrong key, URL or model does not get better by waiting.
- ``timeout`` bounds each attempt as a whole, connect to last byte.

The API key travels in the ``Authorization`` header only. It is registered with
:func:`memini.logs.register_secret`, removed from any error text the server sends back, and never
part of a message: messages name the endpoint as ``scheme://host:port`` and nothing more.
"""

import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final
from urllib.parse import urlsplit

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from memini import __version__
from memini.embeddings.base import BatchingEmbedder, EmbeddingError, EmbeddingSpec
from memini.logs import REDACTED, redact, register_secret

__all__ = ["RETRYABLE_STATUS", "OpenAICompatibleEmbedder"]

RETRYABLE_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504})
"""HTTP statuses after which a request is tried again."""

_DETAIL_LIMIT: Final = 200

log = logging.getLogger(__name__)


class _Item(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int | None = None
    embedding: list[float]


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: list[_Item]


class _ErrorObject(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: str


class _ErrorAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error: str | _ErrorObject


class OpenAICompatibleEmbedder(BatchingEmbedder):
    """An :class:`~memini.embeddings.base.Embedder` that calls ``{url}/embeddings``.

    ``url`` is the base of the API, e.g. ``http://127.0.0.1:11434/v1`` for Ollama. ``transport``,
    ``sleep`` and ``rng`` are for tests: an in-process server, no real waits, fixed jitter.
    """

    def __init__(
        self,
        spec: EmbeddingSpec,
        *,
        url: str,
        api_key: SecretStr | None = None,
        batch_size: int = 32,
        timeout: float = 60.0,
        query_prefix: str = "",
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if spec.provider != "openai_compatible":
            raise ValueError("OpenAICompatibleEmbedder needs a spec of provider openai_compatible")
        if max_attempts < 1 or timeout <= 0 or not 0 <= backoff_base <= backoff_max:
            raise ValueError("needs max_attempts >= 1, timeout > 0, 0 <= backoff_base <= max")
        super().__init__(spec, batch_size=batch_size, query_prefix=query_prefix)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the embedding URL must be an http:// or https:// URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("the embedding URL must not hold credentials; pass api_key")
        port = f":{parts.port}" if parts.port is not None else ""
        self._target = f"{parts.scheme}://{parts.hostname}{port}"
        self._endpoint = url.rstrip("/") + "/embeddings"
        self._key = api_key  # kept wrapped: it renders as ********** wherever it is printed
        headers = {"Accept": "application/json", "User-Agent": f"memini/{__version__}"}
        if api_key is not None:
            register_secret(api_key.get_secret_value())
            headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        # No redirects: a redirect would carry the key to wherever it points.
        self._client = httpx2.AsyncClient(
            headers=headers,
            timeout=httpx2.Timeout(timeout),
            follow_redirects=False,
            transport=transport,
        )
        log.info("embeddings: chunk text goes to %s, model %s", self._target, spec.model)

    @property
    def target(self) -> str:
        """``scheme://host:port`` of the endpoint, for messages: no path, no credentials."""
        return self._target

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        response = await self._post({"model": self.spec.model, "input": texts})
        try:
            answer = _Answer.model_validate_json(response.content)
        except ValidationError:
            raise EmbeddingError(
                f"the embedding endpoint at {self._target} answered with something that is not an"
                " OpenAI embeddings response"
            ) from None
        items = answer.data
        if len(items) != len(texts):
            raise EmbeddingError(
                f"the embedding endpoint at {self._target} returned {len(items)} vectors for"
                f" {len(texts)} texts"
            )
        indexes = [item.index for item in items]
        if all(index is not None for index in indexes):
            if sorted(index for index in indexes if index is not None) != list(range(len(items))):
                raise EmbeddingError(
                    f"the embedding endpoint at {self._target} returned vectors with indexes that"
                    f" are not 0 to {len(items) - 1}"
                )
            items = sorted(items, key=lambda item: item.index or 0)
        return [item.embedding for item in items]

    async def _post(self, payload: dict[str, Any]) -> httpx2.Response:
        attempt = 0
        while True:
            attempt += 1
            retry_after: float | None = None
            try:
                with anyio.fail_after(self._timeout):
                    response = await self._client.post(self._endpoint, json=payload)
            except TimeoutError:
                problem = f"no answer within {self._timeout:g} s"
            except httpx2.TransportError as error:
                problem = _transport_problem(error)
            else:
                if response.is_success:
                    return response
                if response.status_code not in RETRYABLE_STATUS:
                    raise self._refused(response)
                problem = f"HTTP {response.status_code}"
                retry_after = _retry_after(response)
            if attempt >= self._max_attempts:
                raise EmbeddingError(
                    f"the embedding endpoint at {self._target} failed {attempt} times;"
                    f" last: {problem}"
                )
            delay = self._delay(attempt, retry_after)
            log.warning(
                "embedding request to %s failed (%s); attempt %d of %d, next in %.1f s",
                self._target,
                problem,
                attempt,
                self._max_attempts,
                delay,
            )
            await self._sleep(delay)

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._backoff_max)
        delay = min(self._backoff_base * 2 ** (attempt - 1), self._backoff_max)
        return delay / 2 + self._rng.uniform(0, delay / 2)

    def _refused(self, response: httpx2.Response) -> EmbeddingError:
        code = response.status_code
        where = f"the embedding endpoint at {self._target} refused the request (HTTP {code})"
        if code in (401, 403):
            return EmbeddingError(f"{where}; check MEMINI_EMBEDDING_API_KEY")
        if code == 404:
            return EmbeddingError(
                f"{where}; check MEMINI_EMBEDDING_URL, the base of the API such as"
                f" http://127.0.0.1:11434/v1, and that it serves model {self.spec.model}"
            )
        detail = self._detail(response)
        return EmbeddingError(f"{where}: {detail}" if detail else where)

    def _detail(self, response: httpx2.Response) -> str:
        """The server's error message, cut short, without the key or other secrets.

        OpenAI sends ``{"error": {"message": ...}}``, Ollama ``{"error": "..."}``.
        """
        try:
            error = _ErrorAnswer.model_validate_json(response.content).error
        except ValidationError:
            return ""
        message = error if isinstance(error, str) else error.message
        text = " ".join(message.split())
        if self._key is not None:
            text = text.replace(self._key.get_secret_value(), REDACTED)
        text = redact(text)
        return text if len(text) <= _DETAIL_LIMIT else text[: _DETAIL_LIMIT - 3] + "..."


def _transport_problem(error: httpx2.TransportError) -> str:
    """The kind of transport error and its message, e.g. ``ConnectError: Connection refused``."""
    message = " ".join(str(error).split())
    name = type(error).__name__
    return f"{name}: {redact(message)[:_DETAIL_LIMIT]}" if message else name


def _retry_after(response: httpx2.Response) -> float | None:
    """The ``Retry-After`` header in seconds, if it is a number of seconds."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:  # an HTTP date: use the backoff instead
        return None
    return seconds if 0 <= seconds < float("inf") else None
