"""One OpenAI-compatible chat endpoint: the only code that sends content to an LLM.

Private to :mod:`memini.llm`: only :mod:`memini.llm.gate` creates and calls an endpoint. Three
things keep it so:

- An endpoint is created with :data:`_GATE_KEY`, or not at all. The key is a private name: pyright
  (strict) reports an import of it anywhere, and the gate's one import carries an explicit ignore.
- A test parses every module of the service and fails on an import of this module, or on the
  path of a chat endpoint, outside this package.
- The one method that sends, :meth:`ChatEndpoint.complete`, takes the :class:`~memini.llm.Grant`
  the gate decided on and checks it again before any byte leaves: a cloud endpoint takes only a
  grant whose effective policy is ``cloud``.

One ``POST {url}/chat/completions`` per call::

    {"model": ..., "messages": [...], "temperature": 0.0, "max_tokens": 1024, "stream": false,
     "response_format": {"type": "json_schema", "json_schema": {"name": ..., "schema": {...}}}}

How the endpoint is asked for JSON is set per provider (:data:`memini.config.LlmStructuredOutput`):
``json_schema`` sends the schema in ``response_format`` (OpenAI, Ollama, llama.cpp and vLLM
constrain the reply to it); ``json_object`` asks for any JSON object and puts the schema into the
system message; ``none`` sends no ``response_format`` and puts the schema into the system message.
The reply is parsed and validated against the schema in every case: a constrained endpoint is not
trusted to have kept to it. A reply wrapped in a code fence, or after a ``<think>`` block, is
still read.

Failures, as for embeddings (:mod:`memini.embeddings.openai_compatible`):

- A connection error, a timeout, HTTP 408, 429, 500, 502, 503 or 504 is retried, up to
  ``max_attempts`` attempts in all, after an exponential backoff with jitter, or after the
  ``Retry-After`` the server asked for (capped at ``backoff_max``).
- Any other non-2xx answer fails at once.
- ``timeout`` bounds each attempt as a whole.
- A reply that is not JSON, does not match the schema, was cut at ``max_tokens`` or is a refusal
  raises :class:`~memini.llm.LlmOutputError`; it is not retried.

The tokens of every answered call go to the run being recorded (:func:`memini.runs.current_run`),
under ``{provider class}/{model}``, also when the reply then fails validation.

The API key travels in the ``Authorization`` header only. It is registered with
:func:`memini.logs.register_secret`, removed from any error text the server sends back, and never
part of a message: messages name the endpoint as ``scheme://host:port``. No message holds the
content of a request or a reply.
"""

import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final
from urllib.parse import urlsplit

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from memini import __version__
from memini.config import LlmStructuredOutput
from memini.embeddings.openai_compatible import RETRYABLE_STATUS
from memini.llm.model import (
    ChatRequest,
    Completion,
    Grant,
    LlmError,
    LlmOutputError,
    LlmRefused,
    Message,
    ProviderClass,
    Usage,
)
from memini.logs import REDACTED, redact, register_secret
from memini.metrics import LLM_DURATION, LLM_REQUESTS, LLM_TOKENS
from memini.runs import current_run

__all__ = ["ChatEndpoint", "endpoint_target", "parse_output"]

_DETAIL_LIMIT: Final = 200
_MAX_FIELD_ERRORS: Final = 5
_RANK: Final[dict[str, int]] = {"local": 1, "cloud": 2}
"""How much a policy allows, and how much a provider class needs."""

log = logging.getLogger(__name__)


# --- The answer, as far as it is read -----------------------------------------------------------


class _ReplyMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str | None = None
    refusal: str | None = None


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: _ReplyMessage
    finish_reason: str | None = None


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None


class _ErrorObject(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: str


class _ErrorAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error: str | _ErrorObject


# --- The endpoint --------------------------------------------------------------------------------


class _GateKey:
    """What an endpoint is created with. Only :mod:`memini.llm.gate` imports the one instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<the key of the LLM gate>"


_GATE_KEY: Final = _GateKey()


class ChatEndpoint:
    """An OpenAI-compatible ``/chat/completions`` endpoint of one provider class.

    ``key`` is :data:`_GATE_KEY`, which only the gate holds. ``url`` is the base of the API, e.g.
    ``http://127.0.0.1:11434/v1`` for Ollama. ``transport``, ``sleep`` and ``rng`` are for tests:
    an in-process server, no real waits, fixed jitter.
    """

    def __init__(
        self,
        key: _GateKey,
        provider_class: ProviderClass,
        *,
        url: str,
        model: str,
        api_key: SecretStr | None = None,
        structured_output: LlmStructuredOutput = "json_schema",
        timeout: float = 300.0,
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if key is not _GATE_KEY:
            raise TypeError("an LLM endpoint is created by the gate only (memini.llm.gate)")
        if max_attempts < 1 or timeout <= 0 or not 0 <= backoff_base <= backoff_max:
            raise ValueError("needs max_attempts >= 1, timeout > 0, 0 <= backoff_base <= max")
        if not model:
            raise ValueError("an LLM endpoint needs a model name")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("an LLM URL must be an http:// or https:// URL")
        if provider_class == "cloud" and parts.scheme != "https":
            raise ValueError("the URL of a cloud LLM must be an https:// URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("an LLM URL must not hold credentials; pass api_key")
        self._class: ProviderClass = provider_class
        self._model = model
        self._target = endpoint_target(url)
        self._endpoint = url.rstrip("/") + "/chat/completions"
        self._structured_output: LlmStructuredOutput = structured_output
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
        # No redirects: a redirect would carry the key, and the content, to wherever it points.
        self._client = httpx2.AsyncClient(
            headers=headers,
            timeout=httpx2.Timeout(timeout),
            follow_redirects=False,
            transport=transport,
        )
        log.info("llm: the %s provider is model %s at %s", provider_class, model, self._target)

    @property
    def provider_class(self) -> ProviderClass:
        return self._class

    @property
    def model(self) -> str:
        return self._model

    @property
    def target(self) -> str:
        """``scheme://host:port`` of the endpoint, for messages: no path, no credentials."""
        return self._target

    @property
    def _name(self) -> str:
        return f"the {self._class} LLM endpoint at {self._target}"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete[T: BaseModel](self, grant: Grant, request: ChatRequest[T]) -> Completion[T]:
        """Send ``request`` under ``grant`` and return the validated reply.

        Raises:
            LlmRefused: the grant is not for this endpoint, or does not allow its class. Nothing
                was sent.
            LlmError: the endpoint failed or refused the request.
            LlmOutputError: the reply does not match the schema.
        """
        self._check(grant)
        started = time.perf_counter()
        outcome = "error"
        try:
            completion = await self._complete(grant, request)
            outcome = "ok"
            return completion
        except LlmOutputError:
            outcome = "output_error"
            raise
        finally:
            LLM_REQUESTS.inc(provider=self._class, outcome=outcome)
            LLM_DURATION.observe(time.perf_counter() - started, provider=self._class)

    async def _complete[T: BaseModel](self, grant: Grant, request: ChatRequest[T]) -> Completion[T]:
        response = await self._post(self._payload(request))
        try:
            answer = _Answer.model_validate_json(response.content)
        except ValidationError:
            raise LlmError(
                f"{self._name} answered with something that is not an OpenAI chat completion"
            ) from None
        usage = Usage(
            input_tokens=answer.usage.prompt_tokens if answer.usage else 0,
            output_tokens=answer.usage.completion_tokens if answer.usage else 0,
        )
        LLM_TOKENS.inc(usage.input_tokens, provider=self._class, direction="input")
        LLM_TOKENS.inc(usage.output_tokens, provider=self._class, direction="output")
        run = current_run()
        if run is not None:
            run.llm_tokens(
                model=f"{self._class}/{self._model}",
                input=usage.input_tokens,
                output=usage.output_tokens,
            )
        choice = answer.choices[0]
        what = f"model {self._model} at {self._target}"
        if choice.message.content is None or not choice.message.content.strip():
            if choice.message.refusal is not None:
                raise LlmOutputError(f"{what} refused to answer")
            raise LlmOutputError(f"{what} answered with no content")
        if choice.finish_reason == "length":
            raise LlmOutputError(
                f"the reply of {what} was cut at max_tokens ({request.max_tokens}); raise it"
            )
        output = parse_output(choice.message.content, request.output, what=what)
        return Completion(output=output, usage=usage, grant=grant)

    def _check(self, grant: Grant) -> None:
        """The second check of the gate's decision, at the last point before the network."""
        if (grant.provider_class, grant.model, grant.target) != (
            self._class,
            self._model,
            self._target,
        ):
            raise LlmRefused("grant_mismatch", f"the grant is not for {self._name}")
        # A local endpoint may read what a local or cloud policy allows, a cloud endpoint only
        # what a cloud policy allows. Anything else, a value the gate never issues included, is
        # refused.
        if _RANK.get(grant.policy, 0) < _RANK[self._class]:
            raise LlmRefused(
                "grant_mismatch",
                f"{self._name} may not read content whose effective llm_policy is {grant.policy}",
            )

    def _payload(self, request: ChatRequest[Any]) -> dict[str, Any]:
        schema = request.output.model_json_schema()
        messages = list(request.messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if self._structured_output == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.output_name, "schema": schema},
            }
        else:
            messages = _with_schema(messages, schema)
            if self._structured_output == "json_object":
                payload["response_format"] = {"type": "json_object"}
        payload["messages"] = [{"role": m.role, "content": m.content} for m in messages]
        return payload

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
                raise LlmError(f"{self._name} failed {attempt} times; last: {problem}")
            delay = self._delay(attempt, retry_after)
            log.warning(
                "LLM request to %s failed (%s); attempt %d of %d, next in %.1f s",
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

    def _refused(self, response: httpx2.Response) -> LlmError:
        code = response.status_code
        where = f"{self._name} refused the request (HTTP {code})"
        setting = f"MEMINI_LLM_{self._class.upper()}"
        if code in (401, 403):
            return LlmError(f"{where}; check {setting}_API_KEY")
        if code == 404:
            return LlmError(
                f"{where}; check {setting}_URL, the base of the API such as"
                f" http://127.0.0.1:11434/v1, and that it serves model {self._model}"
            )
        detail = self._detail(response)
        hint = ""
        if code == 400 and self._structured_output != "none":
            hint = (
                f"; if the endpoint does not support response_format {self._structured_output},"
                f" set {setting}_STRUCTURED_OUTPUT to "
                + ("json_object or none" if self._structured_output == "json_schema" else "none")
            )
        return LlmError(f"{where}: {detail}{hint}" if detail else f"{where}{hint}")

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


# --- Structured output ---------------------------------------------------------------------------

_SCHEMA_INSTRUCTION: Final = (
    "Reply with one JSON object and nothing else. It must match this JSON schema:\n"
)
_FENCE: Final = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK: Final = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _with_schema(messages: list[Message], schema: dict[str, Any]) -> list[Message]:
    """``messages`` with the schema in the system message: appended to the first message if that
    is a system message, else as a new first message (some chat templates take one system message
    only, and only first)."""
    instruction = _SCHEMA_INSTRUCTION + json.dumps(schema, ensure_ascii=False, sort_keys=True)
    if messages and messages[0].role == "system":
        first = messages[0]
        return [Message("system", f"{first.content}\n\n{instruction}"), *messages[1:]]
    return [Message("system", instruction), *messages]


def parse_output[T: BaseModel](content: str, output: type[T], *, what: str) -> T:
    """The reply ``content`` as an instance of ``output``.

    Takes the whole reply as JSON, else the first fenced block, else the text from its first
    ``{`` to its last ``}``; a ``<think>`` block is dropped first. ``what`` names the model in
    messages.

    Raises:
        LlmOutputError: no JSON was found, or it does not match the schema. The text names the
            fields and the kinds of failure, never values.
    """
    text = _THINK.sub("", content).strip()
    candidates = [text]
    fence = _FENCE.search(text)
    if fence is not None:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    data: Any = None
    found = False
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        found = True
        break
    if not found:
        raise LlmOutputError(f"the reply of {what} is not JSON")
    try:
        return output.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or '(the reply)'}: {error['type']}"
            for error in errors[:_MAX_FIELD_ERRORS]
        ]
        more = len(errors) - len(problems)
        listing = "; ".join(problems) + (f"; and {more} more" if more > 0 else "")
        raise LlmOutputError(
            f"the reply of {what} does not match the schema {output.__name__}: {listing}"
        ) from None


# --- Helpers -------------------------------------------------------------------------------------


def endpoint_target(url: str) -> str:
    """``scheme://host:port`` of ``url``, for messages: no path, no credentials."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


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
