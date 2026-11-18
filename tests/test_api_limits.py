"""Request limits without a database: rate limits, body size, the answer to a database timeout.

Tokens are generated here and resolved by a dictionary; every value is synthetic. The database side
(statement and idle-transaction timeouts on pooled connections) is in tests/test_api_limits_db.py.
"""

import json
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from mcp_types import TextContent
from psycopg import errors
from pydantic import SecretStr

from memini.api import create_app
from memini.api.limits import KNOWN_TOKEN_TTL, Admission, RateLimiter, Refusal
from memini.api.mcp import WRITE_TOOLS, calls_a_tool
from memini.auth import AuthenticationError, Principal, Scope, generate_token, hash_token
from memini.config import Settings, load_settings
from memini.db import SessionLimits, current_session_limits
from memini.mcp import MCP_PATH
from memini.mcp.decisions import DECISION_WRITE_TOOLS
from memini.mcp.tools import NoArguments, Tool, ToolCall

OWNER = uuid.UUID("00000000-0000-7000-8000-0000000000f0")
ADDRESS = "192.0.2.10"  # TEST-NET-1
OTHER_ADDRESS = "192.0.2.20"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
FAKE_URL = "postgresql://fake:fake@db.invalid:5432/memini"
ROOT = Path(__file__).parent.parent


def tools_call(name: str) -> dict[str, Any]:
    """A call of the tool ``name`` with no arguments: refused by the tool, after it is counted."""
    params: dict[str, Any] = {"name": name, "arguments": {}}
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeAuthenticator:
    """Resolves the tokens it issued; refuses every other one as unknown."""

    def __init__(self) -> None:
        self.tokens: dict[str, Principal] = {}
        self.calls = 0

    def issue(self, *scopes: Scope) -> str:
        token = generate_token()
        self.tokens[token] = Principal(
            principal_id=OWNER, token_id=uuid.uuid4(), scopes=frozenset(scopes or ("read",))
        )
        return token

    async def __call__(self, token: str) -> Principal:
        self.calls += 1
        try:
            return self.tokens[token]
        except KeyError:
            raise AuthenticationError("unknown") from None


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()


def limited(**values: Any) -> Settings:
    return Settings(database_url=SecretStr(FAKE_URL), **values)


type MakeClient = Callable[..., TestClient]


@pytest.fixture
def make_client(authenticator: FakeAuthenticator) -> Iterator[MakeClient]:
    """``make_client(**settings)``: a client of an application with those settings, running."""
    clients: list[TestClient] = []

    def make(**values: Any) -> TestClient:
        # No pool: a request that gets past authentication answers 503 for the database.
        client = TestClient(create_app(limited(**values), authenticator=authenticator))
        client.__enter__()  # the lifespan runs the MCP endpoint
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_of(response: Response, status_code: int, code: str) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert list(body) == ["error"]
    error: dict[str, Any] = body["error"]
    assert error["code"] == code
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


# --- The rate limiter ----------------------------------------------------------------------------


def known_limiter(clock: FakeClock, **budgets: int) -> tuple[RateLimiter, bytes, uuid.UUID]:
    limiter = RateLimiter(
        reads=budgets.get("reads", 600),
        writes=budgets.get("writes", 120),
        unauthenticated=budgets.get("unauthenticated", 60),
        max_keys=budgets.get("max_keys", 100),
        clock=clock,
    )
    digest, token_id = hash_token(generate_token()), uuid.uuid4()
    limiter.authenticated(digest, token_id)
    return limiter, digest, token_id


def test_a_budget_admits_a_minutes_worth_and_then_one_per_share_of_the_minute() -> None:
    clock = FakeClock()
    limiter, digest, token_id = known_limiter(clock, reads=3)
    assert [limiter.admit("read", digest, ADDRESS) for _ in range(3)] == [None, None, None]
    refusal = limiter.admit("read", digest, ADDRESS)
    assert refusal is not None
    assert (refusal.budget, refusal.retry_after, refusal.first) == ("read", 20, True)
    assert refusal.subject == f"token {token_id}"
    clock.now += 5
    again = limiter.admit("read", digest, ADDRESS)
    assert again is not None
    assert (again.retry_after, again.first) == (15, False)
    clock.now += 15
    assert limiter.admit("read", digest, ADDRESS) is None
    assert limiter.admit("read", digest, ADDRESS) is not None


def test_retry_after_is_whole_seconds_and_at_least_one() -> None:
    clock = FakeClock()
    limiter, digest, _ = known_limiter(clock, reads=7)  # one per 8.57 s
    for _ in range(7):
        assert limiter.admit("read", digest, ADDRESS) is None
    refusal = limiter.admit("read", digest, ADDRESS)
    assert refusal is not None
    assert refusal.retry_after == 9
    clock.now += 8.5
    refusal = limiter.admit("read", digest, ADDRESS)
    assert refusal is not None
    assert refusal.retry_after == 1


def test_reads_and_writes_and_tokens_have_separate_budgets() -> None:
    clock = FakeClock()
    limiter, digest, _ = known_limiter(clock, reads=1, writes=1)
    other, other_id = hash_token(generate_token()), uuid.uuid4()
    limiter.authenticated(other, other_id)
    assert limiter.admit("read", digest, ADDRESS) is None
    assert limiter.admit("read", digest, ADDRESS) is not None
    assert limiter.admit("write", digest, ADDRESS) is None
    assert limiter.admit("write", digest, ADDRESS) is not None
    assert limiter.admit("read", other, ADDRESS) is None
    assert limiter.admit("write", other, ADDRESS) is None


def test_a_token_not_known_to_be_valid_is_counted_against_its_address() -> None:
    clock = FakeClock()
    limiter, known, _ = known_limiter(clock, unauthenticated=2)
    guesses = [hash_token(generate_token()) for _ in range(3)]
    assert limiter.admit("read", guesses[0], ADDRESS) is None
    assert limiter.admit("write", None, ADDRESS) is None
    refusal = limiter.admit("read", guesses[1], ADDRESS)
    assert refusal is not None
    assert (refusal.budget, refusal.retry_after) == ("unauthenticated", 30)
    assert refusal.subject == f"address {ADDRESS}"
    # Another address has its own budget; a known token is not held up by its address.
    assert limiter.admit("read", guesses[2], OTHER_ADDRESS) is None
    assert limiter.admit("read", known, ADDRESS) is None


def test_a_token_is_forgotten_after_a_401_and_when_idle() -> None:
    clock = FakeClock()
    limiter, digest, token_id = known_limiter(clock, unauthenticated=1)
    limiter.refused(digest)
    assert limiter.admit("read", digest, ADDRESS) is None  # the address's one request
    assert limiter.admit("read", digest, ADDRESS) is not None
    limiter.authenticated(digest, token_id)
    assert limiter.admit("read", digest, ADDRESS) is None
    clock.now += KNOWN_TOKEN_TTL + 1
    assert limiter.admit("read", digest, ADDRESS) is None  # the address's budget refilled
    assert limiter.known_tokens() == 0
    refusal = limiter.admit("read", digest, ADDRESS)
    assert refusal is not None
    assert refusal.budget == "unauthenticated"


def test_idle_keys_are_evicted_once_their_bucket_has_refilled() -> None:
    clock = FakeClock()
    limiter, _, _ = known_limiter(clock, unauthenticated=60)
    for host in range(50):
        assert limiter.admit("read", None, f"198.51.100.{host}") is None
    assert limiter.keys("unauthenticated") == 50
    clock.now += 0.5  # half a request back each: not full yet
    assert limiter.admit("read", None, ADDRESS) is None
    assert limiter.keys("unauthenticated") == 51
    clock.now += 1  # full again: forgetting them changes nothing
    assert limiter.admit("read", None, ADDRESS) is None
    assert limiter.keys("unauthenticated") == 1


def test_memory_is_bounded_by_max_keys() -> None:
    clock = FakeClock()
    limiter, _, _ = known_limiter(clock, unauthenticated=1, max_keys=3)
    for host in range(10):
        limiter.admit("read", None, f"198.51.100.{host}")
        limiter.authenticated(hash_token(generate_token()), uuid.uuid4())
    assert limiter.keys("unauthenticated") == 3
    assert limiter.known_tokens() == 3


def test_settings_build_the_limiter_or_turn_it_off() -> None:
    assert RateLimiter.from_settings(limited()) is not None
    assert RateLimiter.from_settings(limited(http_rate_limit=False)) is None
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(reads=0, writes=1, unauthenticated=1)


def test_a_request_counted_as_a_write_instead_gives_its_read_back() -> None:
    clock = FakeClock()
    limiter, digest, token_id = known_limiter(clock, reads=1, writes=1)
    read = limiter.count("read", digest, ADDRESS)
    assert isinstance(read, Admission)
    assert read == Admission("read", token_id)
    write = limiter.recount(read, "write")
    assert isinstance(write, Admission)
    assert write == Admission("write", token_id)
    assert limiter.recount(write, "write") == write  # counted once
    again = limiter.count("read", digest, ADDRESS)  # the read is back
    assert isinstance(again, Admission)
    refusal = limiter.recount(again, "write")
    assert isinstance(refusal, Refusal)
    assert (refusal.budget, refusal.retry_after, refusal.first) == ("write", 60, True)
    assert refusal.subject == f"token {token_id}"
    # Refused, the request took nothing: the read is back again.
    assert limiter.admit("read", digest, ADDRESS) is None
    assert limiter.admit("read", digest, ADDRESS) is not None


def test_a_request_counted_against_its_address_stays_there() -> None:
    clock = FakeClock()
    limiter, _, _ = known_limiter(clock, writes=1)
    guess = limiter.count("read", hash_token(generate_token()), ADDRESS)
    assert isinstance(guess, Admission)
    assert guess == Admission("unauthenticated", ADDRESS)
    assert limiter.recount(guess, "write") == guess
    assert limiter.keys("write") == 0


# --- Rate limits in the application --------------------------------------------------------------


def test_a_token_over_its_read_budget_gets_429_with_retry_after(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit_reads=2)
    token = authenticator.issue("read")
    # The first request is counted against the address; then the token is known.
    for _ in range(3):
        error_of(client.get("/v1/workspaces", headers=bearer(token)), 503, "unavailable")
    response = client.get("/v1/workspaces", headers=bearer(token))
    error = error_of(response, 429, "rate_limited")
    assert response.headers["Retry-After"] == "30"
    assert error["message"] == "Too many reads for this token; retry after 30 s."
    # Another token is not held up.
    other = authenticator.issue("read")
    error_of(client.get("/v1/workspaces", headers=bearer(other)), 503, "unavailable")


def test_writes_have_a_budget_of_their_own(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit_writes=1)
    token = authenticator.issue("read", "admin")
    error_of(client.get("/v1/workspaces", headers=bearer(token)), 503, "unavailable")
    alpha = {"name": "alpha"}
    error_of(client.post("/v1/workspaces", json=alpha, headers=bearer(token)), 503, "unavailable")
    response = client.post("/v1/workspaces", json=alpha, headers=bearer(token))
    assert error_of(response, 429, "rate_limited")["message"].startswith("Too many writes")
    assert response.headers["Retry-After"] == "60"
    error_of(client.get("/v1/workspaces", headers=bearer(token)), 503, "unavailable")


def test_the_mcp_endpoint_draws_on_the_same_read_budget(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit_reads=2)
    token = authenticator.issue("read")
    headers = {**bearer(token), **MCP_HEADERS}
    assert client.post(MCP_PATH, json=TOOLS_LIST, headers=headers).status_code == 200
    assert client.post(MCP_PATH, json=TOOLS_LIST, headers=headers).status_code == 200
    error_of(client.get("/v1/workspaces", headers=bearer(token)), 503, "unavailable")
    response = client.post(MCP_PATH, json=TOOLS_LIST, headers=headers)
    error_of(response, 429, "rate_limited")
    assert response.headers["Retry-After"] == "30"


def test_an_mcp_call_of_a_write_tool_spends_the_write_budget(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit_reads=1, http_rate_limit_writes=2)
    token = authenticator.issue("read", "ingest")
    headers = {**bearer(token), **MCP_HEADERS}
    # The first request is counted against the address; then the token is known.
    assert client.post(MCP_PATH, json=TOOLS_LIST, headers=headers).status_code == 200
    for name in ("record_decision", "supersede_decision"):
        # Counted as a read on arrival, then as a write: the one read is given back each time.
        response = client.post(MCP_PATH, json=tools_call(name), headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["isError"] is True  # called: its arguments are refused
    response = client.post(MCP_PATH, json=tools_call("retract_decision"), headers=headers)
    error = error_of(response, 429, "rate_limited")
    assert error["message"] == "Too many writes for this token; retry after 30 s."
    assert response.headers["Retry-After"] == "30"
    # The writes over HTTP draw on the same budget; the read is still there, once.
    alpha = {"name": "alpha"}
    error_of(client.post("/v1/workspaces", json=alpha, headers=bearer(token)), 429, "rate_limited")
    assert client.post(MCP_PATH, json=TOOLS_LIST, headers=headers).status_code == 200
    response = client.post(MCP_PATH, json=TOOLS_LIST, headers=headers)
    assert error_of(response, 429, "rate_limited")["message"].startswith("Too many reads")


def test_an_mcp_call_of_a_read_tool_spends_the_read_budget(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit_reads=2, http_rate_limit_writes=1)
    token = authenticator.issue("read", "ingest")
    headers = {**bearer(token), **MCP_HEADERS}
    assert client.post(MCP_PATH, json=TOOLS_LIST, headers=headers).status_code == 200
    for name in ("status", "search"):
        response = client.post(MCP_PATH, json=tools_call(name), headers=headers)
        assert response.status_code == 200, response.text
    response = client.post(MCP_PATH, json=tools_call("read_source"), headers=headers)
    assert error_of(response, 429, "rate_limited")["message"].startswith("Too many reads")
    # Every MCP request is counted as a read before its body is read: a write call too.
    response = client.post(MCP_PATH, json=tools_call("record_decision"), headers=headers)
    assert error_of(response, 429, "rate_limited")["message"].startswith("Too many reads")
    # The write budget is untouched: its one write gets past it, to the scope check.
    alpha = {"name": "alpha"}
    response = client.post("/v1/workspaces", json=alpha, headers=bearer(token))
    error_of(response, 403, "insufficient_scope")
    error_of(client.post("/v1/workspaces", json=alpha, headers=bearer(token)), 429, "rate_limited")


def test_an_mcp_write_call_with_a_token_not_yet_known_is_counted_against_the_address(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit_writes=1)
    token = authenticator.issue("read", "ingest")
    headers = {**bearer(token), **MCP_HEADERS}
    response = client.post(MCP_PATH, json=tools_call("close_decision"), headers=headers)
    assert response.status_code == 200, response.text
    alpha = {"name": "alpha"}
    response = client.post("/v1/workspaces", json=alpha, headers=bearer(token))
    error_of(response, 403, "insufficient_scope")
    error_of(client.post("/v1/workspaces", json=alpha, headers=bearer(token)), 429, "rate_limited")


def test_an_mcp_write_call_passes_with_rate_limits_off(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit=False)
    headers = {**bearer(authenticator.issue("read", "ingest")), **MCP_HEADERS}
    for _ in range(3):
        response = client.post(MCP_PATH, json=tools_call("record_decision"), headers=headers)
        assert response.json()["result"]["isError"] is True


def test_the_write_tools_are_add_document_and_the_decision_tools() -> None:
    decision_tools = {tool.name for tool in DECISION_WRITE_TOOLS}
    assert {"add_document", *decision_tools} == WRITE_TOOLS
    assert len(WRITE_TOOLS) == 5


def call(name: object, method: str = "tools/call") -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": method, "params": {"name": name}}
    ).encode()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (call("record_decision"), True),
        (call("search"), False),
        (json.dumps([TOOLS_LIST, json.loads(call("close_decision"))]).encode(), True),
        (json.dumps([TOOLS_LIST, json.loads(call("status"))]).encode(), False),
        (call("record_decision", method="tools/list"), False),
        (call(["record_decision"]), False),
        (b'{"method": "tools/call", "params": ["record_decision"]}', False),
        (b'{"method": "tools\\/call", "params": {"name": "record\\u005fdecision"}}', True),
        (b'{"method": "tools/call", "params": {"name": "search", "name": "close_decision"}}', True),
        (b"{", False),
        (b"\xff\xfe\x00", False),
        (b"[" * 100_000 + b"]" * 100_000, False),
        (b"", False),
    ],
    ids=[
        "write",
        "read",
        "batch-write",
        "batch-read",
        "other-method",
        "name-not-text",
        "params-not-object",
        "escaped",
        "duplicate-name",
        "not-json",
        "not-utf8",
        "deep",
        "empty",
    ],
)
def test_a_call_of_a_write_tool_is_told_from_the_body(body: bytes, expected: bool) -> None:
    assert calls_a_tool(body, WRITE_TOOLS) is expected


def test_requests_without_a_valid_token_are_counted_by_address_before_the_lookup(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit_unauthenticated=3)
    error_of(client.get("/v1/workspaces"), 401, "unauthorized")
    error_of(client.get("/v1/workspaces", headers=bearer(generate_token())), 401, "invalid_token")
    error_of(client.post(MCP_PATH, json=TOOLS_LIST, headers=MCP_HEADERS), 401, "unauthorized")
    assert authenticator.calls == 1
    for request in (
        client.get("/v1/workspaces"),
        client.get("/v1/workspaces", headers=bearer(generate_token())),
        client.post(MCP_PATH, json=TOOLS_LIST, headers=bearer(generate_token())),
    ):
        error = error_of(request, 429, "rate_limited")
        assert error["message"] == (
            "Too many requests without a valid token from this address; retry after 20 s."
        )
        assert request.headers["Retry-After"] == "20"
    assert authenticator.calls == 1  # refused before the token was looked up


def test_a_known_token_is_not_held_up_by_its_address_until_it_answers_401(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_rate_limit_unauthenticated=2)
    token = authenticator.issue("read")
    error_of(client.get("/v1/workspaces", headers=bearer(token)), 503, "unavailable")
    error_of(client.get("/v1/workspaces"), 401, "unauthorized")
    error_of(client.get("/v1/workspaces"), 429, "rate_limited")
    error_of(client.get("/v1/workspaces", headers=bearer(token)), 503, "unavailable")
    del authenticator.tokens[token]  # revoked
    error_of(client.get("/v1/workspaces", headers=bearer(token)), 401, "invalid_token")
    error_of(client.get("/v1/workspaces", headers=bearer(token)), 429, "rate_limited")


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/version", "/openapi.json"])
def test_public_routes_are_not_counted(make_client: MakeClient, path: str) -> None:
    client = make_client(http_rate_limit_unauthenticated=1, http_rate_limit_reads=1)
    for _ in range(5):
        assert client.get(path).status_code == 200


def test_rate_limits_can_be_turned_off(make_client: MakeClient) -> None:
    client = make_client(http_rate_limit=False, http_rate_limit_unauthenticated=1)
    for _ in range(5):
        error_of(client.get("/v1/workspaces"), 401, "unauthorized")


# --- Body size -----------------------------------------------------------------------------------


def chunks(size: int) -> Iterator[bytes]:
    """A JSON body of about ``size`` bytes, sent without a Content-Length."""
    yield b'{"name": "'
    for _ in range(size // 256):
        yield b"x" * 256
    yield b'"}'


def test_a_body_over_the_limit_is_refused_by_its_content_length(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_max_body_size=1024)
    token = authenticator.issue("read", "admin")
    response = client.post("/v1/workspaces", json={"name": "x" * 2000}, headers=bearer(token))
    error = error_of(response, 413, "content_too_large")
    assert error["message"] == "The body is larger than 1024 bytes."
    assert authenticator.calls == 0  # refused before anything ran
    # One under the limit gets through to the route.
    small = client.post("/v1/workspaces", json={"name": "alpha"}, headers=bearer(token))
    error_of(small, 503, "unavailable")


def test_a_streamed_body_over_the_limit_is_refused_as_it_arrives(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_max_body_size=1024)
    token = authenticator.issue("read", "admin")
    headers = {**bearer(token), "Content-Type": "application/json"}
    response = client.post("/v1/workspaces", content=chunks(4096), headers=headers)
    assert "content-length" not in response.request.headers
    error_of(response, 413, "content_too_large")


@pytest.mark.parametrize("streamed", [False, True], ids=["content-length", "streamed"])
def test_the_mcp_endpoint_refuses_a_body_over_the_limit(
    make_client: MakeClient, authenticator: FakeAuthenticator, streamed: bool
) -> None:
    client = make_client(http_max_body_size=1024)
    token = authenticator.issue("read")
    headers = {**bearer(token), **MCP_HEADERS, "Content-Type": "application/json"}
    body = chunks(4096) if streamed else b'{"jsonrpc": "2.0", "pad": "' + b"x" * 2000 + b'"}'
    response = client.post(MCP_PATH, content=body, headers=headers)
    error_of(response, 413, "content_too_large")
    assert client.post(MCP_PATH, json=TOOLS_LIST, headers=headers).status_code == 200


def test_the_ingest_route_keeps_its_own_body_limit(
    make_client: MakeClient, authenticator: FakeAuthenticator
) -> None:
    client = make_client(http_max_body_size=1024)
    token = authenticator.issue("ingest")
    batch = {
        "records": [
            {
                "kind": "document",
                "external_id": "doc-1",
                "occurred_at": "2026-03-01T09:30:00Z",
                "payload": {"title": "Notes", "text": "x" * 4000},
            }
        ]
    }
    response = client.post(
        "/v1/workspaces/alpha/sources/notes/records", json=batch, headers=bearer(token)
    )
    error_of(response, 503, "unavailable")  # past the body, at the database


# --- Database time limits ------------------------------------------------------------------------


def test_every_request_runs_under_the_session_limits_of_the_settings(
    make_client: MakeClient,
) -> None:
    client = make_client(http_statement_timeout=12.5, http_idle_in_transaction_timeout=0)
    app = cast(FastAPI, client.app)

    @app.get("/test/limits")
    async def limits() -> dict[str, float | None]:
        current = current_session_limits()
        assert current is not None
        return {
            "statement": current.statement_timeout,
            "idle": current.idle_in_transaction_timeout,
        }

    assert client.get("/test/limits").json() == {"statement": 12.5, "idle": 0}
    assert current_session_limits() is None


@pytest.mark.parametrize(
    "error",
    [
        errors.QueryCanceled("canceling statement due to statement timeout"),
        errors.IdleInTransactionSessionTimeout("terminating connection"),
    ],
    ids=["statement", "idle-transaction"],
)
def test_a_database_time_limit_answers_503(make_client: MakeClient, error: Exception) -> None:
    client = make_client()
    app = cast(FastAPI, client.app)

    @app.get("/test/slow")
    async def slow() -> None:
        raise error

    response = client.get("/test/slow")
    body = error_of(response, 503, "query_timeout")
    assert "statement" in body["message"]
    assert str(error) not in response.text


@pytest.mark.anyio
async def test_an_mcp_tool_stopped_by_a_time_limit_says_so() -> None:
    async def run(call: ToolCall, arguments: NoArguments) -> NoArguments:
        raise errors.QueryCanceled("canceling statement due to statement timeout")

    tool = Tool(
        name="slow",
        title="Slow",
        description="Runs past the limit.",
        arguments=NoArguments,
        result=NoArguments,
        run=run,
        summarize=lambda result: "",
    )
    principal = Principal(principal_id=OWNER, token_id=uuid.uuid4(), scopes=frozenset({"read"}))
    result = await tool.call(ToolCall(principal=principal, pool=None), {})
    assert result.is_error is True
    (content,) = result.content
    assert isinstance(content, TextContent)
    assert "time limit" in content.text
    assert "canceling" not in content.text


def test_session_limits_are_not_negative() -> None:
    with pytest.raises(ValueError, match="0 or more"):
        SessionLimits(statement_timeout=-1, idle_in_transaction_timeout=0)


# --- Settings in the compose stack ---------------------------------------------------------------


LIMIT_VARIABLES = {
    f"MEMINI_{name.upper()}"
    for name in Settings.model_fields
    if name.startswith("http_") and name not in ("http_host", "http_port")
}
"""The request limits, as environment variables. Compose sets the host and port."""


def test_compose_leaves_every_request_limit_to_the_settings_file() -> None:
    # A limit compose set in the service's `environment` would override .env.memini, even empty.
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert not [name for name in LIMIT_VARIABLES if name in text]


def test_env_memini_example_lists_the_request_limits_with_the_services_defaults() -> None:
    text = (ROOT / ".env.memini.example").read_text(encoding="utf-8")
    listed = dict(re.findall(r"(?m)^# (MEMINI_HTTP_[A-Z_]+)=(.*)$", text))

    assert set(listed) == LIMIT_VARIABLES
    url = {"MEMINI_DATABASE_URL": FAKE_URL}
    assert load_settings(url | listed) == load_settings(url)
