"""The OpenAI-compatible provider against an in-process fake server (httpx2.MockTransport).

No network: every request goes to a handler in this file. The API key is an obvious fake.
"""

import io
import json
import traceback
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field

import anyio
import httpx2
import pytest
from pydantic import SecretStr

from memini.embeddings import EmbeddingError, EmbeddingSpec, OpenAICompatibleEmbedder
from memini.embeddings.fake import fake_vector
from memini.logs import clear_secrets, redact

pytestmark = pytest.mark.anyio

FAKE_KEY = "sk-fake-embedding-key-not-real-0009"
URL = "http://embeddings.test:11434/v1"
SPEC = EmbeddingSpec(provider="openai_compatible", model="fake-model", dimension=4)

type Respond = Callable[[httpx2.Request, list[str]], Awaitable[httpx2.Response] | httpx2.Response]


@dataclass
class FakeServer:
    """An OpenAI-compatible /embeddings endpoint. ``script`` answers the first requests in turn;
    after it runs out, every request gets a correct answer."""

    dimension: int = 4
    script: list[Respond] = field(default_factory=list[Respond])
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def inputs(self, request: httpx2.Request) -> list[str]:
        body = json.loads(request.content)
        return list(body["input"])

    def answer(self, texts: list[str], *, dimension: int | None = None) -> httpx2.Response:
        size = self.dimension if dimension is None else dimension
        data = [
            {"object": "embedding", "index": i, "embedding": fake_vector(text, size)}
            for i, text in enumerate(texts)
        ]
        return httpx2.Response(200, json={"object": "list", "data": data, "model": "fake-model"})

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        texts = self.inputs(request)
        if self.script:
            result = self.script.pop(0)(request, texts)
            return await result if isinstance(result, Awaitable) else result
        return self.answer(texts)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture(autouse=True)
def _forget_secrets() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """The embedder registers its key as a secret; forget it after each test."""
    yield
    clear_secrets()


class Sleeps:
    """Records the waits between attempts instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make(
    server: FakeServer,
    *,
    sleeps: Sleeps | None = None,
    api_key: str | None = FAKE_KEY,
    **options: object,
) -> OpenAICompatibleEmbedder:
    return OpenAICompatibleEmbedder(
        SPEC,
        url=URL,
        api_key=None if api_key is None else SecretStr(api_key),
        transport=server.transport(),
        sleep=sleeps or Sleeps(),
        **options,  # pyright: ignore[reportArgumentType]
    )


def status(code: int, body: object = None, headers: dict[str, str] | None = None) -> Respond:
    def respond(request: httpx2.Request, texts: list[str]) -> httpx2.Response:
        return httpx2.Response(code, json=body, headers=headers)

    return respond


def raises(error: type[httpx2.TransportError]) -> Respond:
    def respond(request: httpx2.Request, texts: list[str]) -> httpx2.Response:
        raise error("synthetic transport failure", request=request)

    return respond


# --- Requests and answers ------------------------------------------------------------------------


async def test_posts_the_model_and_the_texts_with_the_key(server: FakeServer) -> None:
    async with make(server) as embedder:
        vectors = await embedder.embed(["alpha", "beta"])

    (request,) = server.requests
    assert request.method == "POST"
    assert str(request.url) == f"{URL}/embeddings"
    assert json.loads(request.content) == {"model": "fake-model", "input": ["alpha", "beta"]}
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert len(vectors) == 2
    assert all(len(vector) == 4 for vector in vectors)


async def test_without_a_key_no_authorization_header_is_sent(server: FakeServer) -> None:
    async with make(server, api_key=None) as embedder:
        await embedder.embed(["alpha"])

    assert "authorization" not in server.requests[0].headers


async def test_a_trailing_slash_in_the_url_is_ignored(server: FakeServer) -> None:
    embedder = OpenAICompatibleEmbedder(SPEC, url=URL + "/", transport=server.transport())
    async with embedder:
        await embedder.embed(["alpha"])

    assert str(server.requests[0].url) == f"{URL}/embeddings"


async def test_ten_texts_in_batches_of_four_take_three_requests(server: FakeServer) -> None:
    texts = [f"synthetic text {i}" for i in range(10)]

    async with make(server, batch_size=4) as embedder:
        vectors = await embedder.embed(texts)

    assert [len(server.inputs(request)) for request in server.requests] == [4, 4, 2]
    assert len(vectors) == 10


async def test_vectors_are_put_in_order_by_their_index(server: FakeServer) -> None:
    def shuffled(request: httpx2.Request, texts: list[str]) -> httpx2.Response:
        data = [{"index": i, "embedding": fake_vector(text, 4)} for i, text in enumerate(texts)][
            ::-1
        ]
        return httpx2.Response(200, json={"data": data})

    server.script = [shuffled]
    async with make(server) as embedder:
        reordered = await embedder.embed(["one", "two", "three"])
        ordered = await embedder.embed(["one", "two", "three"])

    assert reordered == ordered


async def test_vectors_are_normalized_like_every_provider(server: FakeServer) -> None:
    async with make(server) as embedder:
        (vector,) = await embedder.embed(["alpha"])

    assert sum(x * x for x in vector) == pytest.approx(1.0)


# --- Answers that cannot be used -----------------------------------------------------------------


async def test_a_dimension_other_than_the_pinned_one_is_a_clear_error(server: FakeServer) -> None:
    server.script = [lambda request, texts: server.answer(texts, dimension=7)]

    async with make(server) as embedder:
        with pytest.raises(EmbeddingError) as info:
            await embedder.embed(["alpha"])

    message = str(info.value)
    assert "dimension 7, but embedding_dimension is 4" in message
    assert "MEMINI_EMBEDDING_DIMENSION" in message
    assert "memini reindex" in message


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"data": [{"index": 0, "embedding": "not a list"}]}, "not an OpenAI embeddings response"),
        ({"result": []}, "not an OpenAI embeddings response"),
        ({"data": []}, "returned 0 vectors for 1 texts"),
        ({"data": [{"index": 5, "embedding": [1, 0, 0, 0]}]}, "indexes that are not 0 to 0"),
    ],
)
async def test_a_malformed_answer_is_an_error(
    server: FakeServer, body: object, message: str
) -> None:
    server.script = [status(200, body)]

    async with make(server) as embedder:
        with pytest.raises(EmbeddingError, match=message):
            await embedder.embed(["alpha"])


async def test_an_answer_without_indexes_keeps_its_order(server: FakeServer) -> None:
    server.script = [
        status(200, {"data": [{"embedding": [1, 0, 0, 0]}, {"embedding": [0, 1, 0, 0]}]})
    ]

    async with make(server) as embedder:
        vectors = await embedder.embed(["a", "b"])

    assert vectors == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]


# --- Retries, backoff, timeouts ------------------------------------------------------------------


async def test_server_errors_are_retried_with_growing_waits(server: FakeServer) -> None:
    sleeps = Sleeps()
    server.script = [status(503), status(502), status(500)]

    async with make(server, sleeps=sleeps, backoff_base=1.0) as embedder:
        vectors = await embedder.embed(["alpha"])

    assert len(vectors) == 1
    assert len(server.requests) == 4
    # Equal jitter: each wait is between half and all of base * 2**(attempt - 1).
    for delay, full in zip(sleeps.delays, [1.0, 2.0, 4.0], strict=True):
        assert full / 2 <= delay <= full


async def test_retry_after_is_honoured_and_capped(server: FakeServer) -> None:
    sleeps = Sleeps()
    server.script = [
        status(429, headers={"Retry-After": "7"}),
        status(429, headers={"Retry-After": "900"}),
    ]

    async with make(server, sleeps=sleeps, backoff_max=30.0) as embedder:
        await embedder.embed(["alpha"])

    assert sleeps.delays == [7.0, 30.0]


async def test_connection_errors_are_retried(server: FakeServer) -> None:
    server.script = [raises(httpx2.ConnectError), raises(httpx2.ReadTimeout)]

    async with make(server) as embedder:
        await embedder.embed(["alpha"])

    assert len(server.requests) == 3


async def test_after_the_last_attempt_the_error_names_endpoint_and_cause(
    server: FakeServer,
) -> None:
    server.script = [status(503)] * 3

    async with make(server, max_attempts=3) as embedder:
        with pytest.raises(EmbeddingError) as info:
            await embedder.embed(["alpha"])

    assert len(server.requests) == 3
    assert str(info.value) == (
        "the embedding endpoint at http://embeddings.test:11434 failed 3 times; last: HTTP 503"
    )


async def test_an_attempt_that_takes_too_long_times_out_and_is_retried(server: FakeServer) -> None:
    async def slow(request: httpx2.Request, texts: list[str]) -> httpx2.Response:
        await anyio.sleep(5)
        return server.answer(texts)

    server.script = [slow, slow]

    async with make(server, timeout=0.05, max_attempts=2) as embedder:
        with pytest.raises(EmbeddingError, match=r"failed 2 times; last: no answer within 0.05 s"):
            await embedder.embed(["alpha"])


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (401, "check MEMINI_EMBEDDING_API_KEY"),
        (403, "check MEMINI_EMBEDDING_API_KEY"),
        (404, "check MEMINI_EMBEDDING_URL"),
        (400, "HTTP 400"),
        (422, "HTTP 422"),
    ],
)
async def test_a_refusal_is_not_retried(server: FakeServer, code: int, hint: str) -> None:
    sleeps = Sleeps()
    server.script = [status(code, {"error": {"message": "synthetic refusal"}})]

    async with make(server, sleeps=sleeps) as embedder:
        with pytest.raises(EmbeddingError, match=hint):
            await embedder.embed(["alpha"])

    assert len(server.requests) == 1
    assert sleeps.delays == []


async def test_a_refusal_carries_the_servers_message_cut_short(server: FakeServer) -> None:
    server.script = [status(400, {"error": "input is too long " + "x" * 500})]

    async with make(server) as embedder:
        with pytest.raises(EmbeddingError) as info:
            await embedder.embed(["alpha"])

    message = str(info.value)
    assert message.startswith(
        "the embedding endpoint at http://embeddings.test:11434 refused the request (HTTP 400):"
        " input is too long xxx"
    )
    assert message.endswith("...")
    assert len(message) < 320


# --- The key stays secret ------------------------------------------------------------------------


async def test_the_key_never_reaches_an_error_or_a_log_line(
    server: FakeServer, log_output: io.StringIO
) -> None:
    """A server that echoes the key in its error must not make us repeat it anywhere."""
    echo = {"error": {"message": f"invalid api key {FAKE_KEY}"}}
    server.script = [status(503), raises(httpx2.ConnectError), status(400, echo)]

    async with make(server) as embedder:
        with pytest.raises(EmbeddingError) as info:
            await embedder.embed(["alpha"])
        assert FAKE_KEY not in repr(embedder.__dict__)

    error = info.value
    assert "invalid api key [REDACTED]" in str(error)
    for text in (str(error), repr(error), "".join(traceback.format_exception(error))):
        assert FAKE_KEY not in text
    assert error.__cause__ is None
    logs = log_output.getvalue()
    assert "embedding request to http://embeddings.test:11434 failed" in logs
    assert FAKE_KEY not in logs


async def test_the_key_is_registered_for_redaction(server: FakeServer) -> None:
    make(server)

    assert redact(f"oops {FAKE_KEY}") == "oops [REDACTED]"


# --- Construction --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://embeddings.test/v1", "http:// or https://"),
        ("http:///v1", "http:// or https://"),
        ("http://user:pass@embeddings.test/v1", "must not hold credentials"),
    ],
)
def test_a_bad_url_is_refused(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        OpenAICompatibleEmbedder(SPEC, url=url)


def test_messages_name_the_endpoint_without_path() -> None:
    embedder = OpenAICompatibleEmbedder(SPEC, url="https://api.example.test/some/path/v1")

    assert embedder.target == "https://api.example.test"


def test_a_local_spec_is_refused() -> None:
    spec = EmbeddingSpec(provider="local", model="org/m", dimension=4, pooling="mean", max_tokens=8)

    with pytest.raises(ValueError, match="openai_compatible"):
        OpenAICompatibleEmbedder(spec, url=URL)
