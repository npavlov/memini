"""The service executor's parts without a database: the egress policy, the guarded transport, the
budget, the drift check, the calls to make, the session's classification of failures, the OAuth
refresher and the mcp source settings. The MCP server is the fake Hearth (tests/fake_mcp.py)
inside the test process; no socket is opened and no name resolved. Every value is synthetic.
"""

import base64
import ipaddress
import json
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import anyio
import httpcore2
import httpx2
import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError
from pydantic import SecretStr

from fake_mcp import FakeMcp, recent_threads
from hearth_helpers import CHANNEL, HEARTH_RECIPE_PATH, WINDOW_1, FakeHearth, hearth_recipe
from memini.config import ConfigError, load_settings
from memini.credentials import CredentialInfo, OAuthClient, OAuthCredential, OAuthTokens
from memini.executors.budget import Budget, RetryPolicy
from memini.executors.client import (
    CallFailure,
    ConnectError,
    McpSession,
    _unwrap,  # pyright: ignore[reportPrivateUsage] - the unwrapping of the SDK's task groups
    open_session,
)
from memini.executors.core import ToolInfo, call_identity, calls_to_make, check_drift
from memini.executors.egress import (
    EgressPolicy,
    EgressRefused,
    EndpointRule,
    IPAddress,
    is_internal,
    parse_endpoint_url,
)
from memini.executors.oauth import RefreshFailed, token_refresher
from memini.executors.source import (
    HttpEndpoint,
    McpConfig,
    McpConfigError,
    RecipeRef,
    StdioEndpoint,
    check_endpoint,
    check_stream_keys,
    check_window,
    inline_recipe,
)
from memini.executors.transport import PinnedBackend, preflight, retry_after
from memini.recipes import MissingCall, Rate, WindowInput, register_parser, unregister_parser
from memini.responses import ServerInfo

pytestmark = pytest.mark.anyio

HOST: Final = "hearth.example"
URL: Final = f"https://{HOST}/mcp"
POLICY: Final = EgressPolicy.build(endpoints=[HOST])


# --- Endpoints ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://hearth.example/mcp",
        "https://user:secret@hearth.example/mcp",
        "https://hearth.example/mcp?key=abc",
        "https://hearth.example/mcp#part",
        "https://hearth.example:0/mcp",
        "ftp://hearth.example/mcp",
        " https://hearth.example/mcp",
        "https://hear th.example/mcp",
        "https:///mcp",
    ],
)
def test_an_endpoint_url_of_another_form_is_refused_without_repeating_it(url: str) -> None:
    with pytest.raises(EgressRefused) as refused:
        parse_endpoint_url(url)
    assert "secret" not in str(refused.value)
    assert "hearth" not in str(refused.value)


def test_an_endpoint_url_is_stored_in_lower_case_with_its_port() -> None:
    target = parse_endpoint_url("HTTPS://Hearth.Example:8443/MCP")
    assert (target.url, target.host, target.port, target.path) == (
        "https://hearth.example:8443/MCP",
        "hearth.example",
        8443,
        "/MCP",
    )
    assert parse_endpoint_url("http://hearth.example/mcp", allow_http=True).port == 80


@pytest.mark.parametrize(
    ("rule", "url", "allowed"),
    [
        ("hearth.example", "https://hearth.example/mcp", True),
        ("hearth.example", "https://hearth.example:8443/mcp", False),
        ("hearth.example:8443", "https://hearth.example:8443/mcp", True),
        ("hearth.example", "https://other.example/mcp", False),
        ("*.hearth.example", "https://a.hearth.example/mcp", True),
        ("*.hearth.example", "https://b.a.hearth.example/mcp", True),
        ("*.hearth.example", "https://hearth.example/mcp", False),
        ("https://hearth.example/v1", "https://hearth.example/v1/mcp", True),
        ("https://hearth.example/v1", "https://hearth.example/v1", True),
        ("https://hearth.example/v1", "https://hearth.example/v10/mcp", False),
        ("https://hearth.example/v1/", "https://hearth.example/other", False),
        ("https://hearth.example", "https://hearth.example/anything", True),
        ("https://hearth.example/v1", "http://hearth.example/v1/mcp", False),
    ],
)
def test_an_endpoint_rule_allows_its_host_port_and_path(rule: str, url: str, allowed: bool) -> None:
    target = parse_endpoint_url(url, allow_http=True)
    assert EndpointRule.parse(rule).allows(target) is allowed


@pytest.mark.parametrize(
    "rule", ["", "a b", "*.", "*.example", "*.10.0.0.1", "host:70000", "https://u:p@h/x"]
)
def test_an_endpoint_rule_that_is_not_one_is_refused(rule: str) -> None:
    with pytest.raises((ValueError, EgressRefused)):
        EndpointRule.parse(rule)


def test_the_settings_refuse_an_endpoint_rule_or_network_that_is_not_one() -> None:
    base = {"MEMINI_DATABASE_URL": "postgresql://fake:fake@db.invalid:5432/memini"}
    with pytest.raises(ConfigError) as refused:
        load_settings(
            base | {"MEMINI_MCP_ALLOWED_ENDPOINTS": "a b", "MEMINI_MCP_ALLOWED_NETWORKS": "10.0/x"}
        )
    text = str(refused.value)
    assert "MEMINI_MCP_ALLOWED_ENDPOINTS" in text
    assert "MEMINI_MCP_ALLOWED_NETWORKS" in text
    assert "a b" not in text
    settings = load_settings(
        base
        | {
            "MEMINI_MCP_ALLOWED_ENDPOINTS": "hearth.example, https://mill.example/v1",
            "MEMINI_MCP_ALLOWED_NETWORKS": "10.1.2.0/24,127.0.0.1",
            "MEMINI_MCP_ALLOWED_COMMANDS": "/usr/local/bin/hearth-mcp",
        }
    )
    policy = EgressPolicy.from_settings(settings)
    assert policy.check_url(URL).host == HOST
    assert policy.check_url("https://mill.example/v1/mcp").path == "/v1/mcp"
    assert settings.mcp_allowed_networks == ("10.1.2.0/24", "127.0.0.1/32")
    assert policy.check_command("/usr/local/bin/hearth-mcp") == "/usr/local/bin/hearth-mcp"


@pytest.mark.parametrize(
    ("address", "internal"),
    [
        ("127.0.0.1", True),
        ("10.20.30.40", True),
        ("172.16.0.1", True),
        ("192.168.1.1", True),
        ("169.254.169.254", True),  # the metadata service of clouds
        ("100.100.100.200", True),  # carrier-grade NAT, another cloud's metadata
        ("0.0.0.0", True),  # noqa: S104 - an address checked, not bound
        ("224.0.0.1", True),
        ("239.1.1.1", True),
        ("::1", True),
        ("fe80::1", True),
        ("fd00:ec2::254", True),
        ("::ffff:127.0.0.1", True),
        ("64:ff9b::a00:1", True),  # NAT64 of 10.0.0.1
        ("2002:a00:1::", True),  # 6to4 of 10.0.0.1
        ("192.0.2.10", True),  # documentation: not public either
        ("8.8.8.8", False),
        ("2001:4860:4860::8888", False),
        ("::ffff:8.8.8.8", False),
    ],
)
def test_internal_addresses_are_every_address_that_is_not_public(
    address: str, internal: bool
) -> None:
    assert is_internal(ipaddress.ip_address(address)) is internal


def test_an_internal_address_passes_only_inside_an_allowed_network() -> None:
    policy = EgressPolicy.build(endpoints=["10.1.2.3"], networks=["10.1.0.0/16"])
    policy.check_url("https://10.1.2.3/mcp")
    policy.check_address(ipaddress.ip_address("::ffff:10.1.9.9"))
    with pytest.raises(EgressRefused, match="MEMINI_MCP_ALLOWED_NETWORKS"):
        policy.check_address(ipaddress.ip_address("10.2.0.1"))
    with pytest.raises(EgressRefused):
        EgressPolicy.build(endpoints=["127.0.0.1"]).check_url("https://127.0.0.1/mcp")
    with pytest.raises(EgressRefused, match="did not resolve"):
        policy.check_addresses([])


def test_a_command_is_allowed_by_its_exact_absolute_path() -> None:
    policy = EgressPolicy.build(commands=["/usr/local/bin/hearth-mcp"])
    assert policy.check_command("/usr/local/bin/../bin/hearth-mcp") == "/usr/local/bin/hearth-mcp"
    for command in ("hearth-mcp", "/usr/bin/hearth-mcp", "/bin/sh", ""):
        with pytest.raises(EgressRefused):
            policy.check_command(command)


# --- The transport --------------------------------------------------------------------------------


def resolver(addresses: Mapping[str, list[str]], calls: list[str] | None = None) -> Any:
    async def resolve(host: str, port: int) -> list[IPAddress]:
        if calls is not None:
            calls.append(host)
        return [ipaddress.ip_address(a) for a in addresses.get(host, [])]

    return resolve


class RecordingBackend(httpcore2.AsyncNetworkBackend):
    """Records the connections asked for and opens none."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, int]] = []

    async def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        self.connects.append((host, port))
        raise httpcore2.ConnectError("synthetic: no network in tests")


async def test_the_pinned_backend_connects_to_the_address_it_checked() -> None:
    inner = RecordingBackend()
    policy = EgressPolicy.build(endpoints=[HOST], networks=["192.0.2.0/24"])
    backend = PinnedBackend(policy, resolver({HOST: ["192.0.2.7", "192.0.2.8"]}), inner)

    with pytest.raises(httpcore2.ConnectError):
        await backend.connect_tcp(HOST, 443)

    assert inner.connects == [("192.0.2.7", 443), ("192.0.2.8", 443)]  # never the name


async def test_the_pinned_backend_refuses_a_host_with_one_internal_address() -> None:
    inner = RecordingBackend()
    policy = EgressPolicy.build(endpoints=[HOST], networks=["192.0.2.0/24"])
    backend = PinnedBackend(policy, resolver({HOST: ["192.0.2.7", "169.254.169.254"]}), inner)

    with pytest.raises(EgressRefused):
        await backend.connect_tcp(HOST, 443)
    with pytest.raises(EgressRefused):
        await backend.connect_unix_socket("/run/docker.sock")

    assert inner.connects == []


async def test_a_name_that_resolves_to_an_internal_address_later_is_refused_at_connect() -> None:
    """DNS rebinding: the preflight sees a public address, the connection an internal one."""
    answers = iter([["8.8.4.4"], ["127.0.0.1"]])

    async def rebinding(host: str, port: int) -> list[IPAddress]:
        return [ipaddress.ip_address(a) for a in next(answers)]

    inner = RecordingBackend()
    await preflight(POLICY, URL, rebinding)
    with pytest.raises(EgressRefused):
        await PinnedBackend(POLICY, rebinding, inner).connect_tcp(HOST, 443)
    assert inner.connects == []


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("3", 3.0), (" 1.5 ", 1.5), ("-4", 0.0), ("soon", None), (None, None), ("nan", None)],
)
def test_retry_after_reads_seconds(value: str | None, seconds: float | None) -> None:
    assert retry_after(value) == seconds


def test_retry_after_reads_an_http_date() -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    assert retry_after("Wed, 18 Nov 2026 12:00:30 GMT", now=now) == 30.0


# --- Sessions against the fake server -------------------------------------------------------------


@asynccontextmanager
async def session_with(
    fake: FakeMcp, *, timeout: float = 5.0, policy: EgressPolicy = POLICY, app: Any = None
) -> AsyncGenerator[McpSession]:
    async with (
        fake.asgi() as served,
        open_session(
            HttpEndpoint(URL),
            policy=policy,
            timeout=timeout,
            token=SecretStr("synthetic-session-token"),
            http_transport=httpx2.ASGITransport(app=app or served),
        ) as session,
    ):
        yield session


def fake_hearth() -> FakeMcp:
    return FakeMcp(FakeHearth(threads=recent_threads(datetime.now(UTC))))


HISTORY: Final = {"channel": CHANNEL, "oldest": 0, "latest": 2_000_000_000, "limit": 100}


async def test_a_session_reports_the_server_and_its_tools_and_returns_raw_results() -> None:
    fake = fake_hearth()
    async with session_with(fake) as session:
        assert session.server == ServerInfo(name="hearth-mcp", version="2.3.1")
        assert [tool.name for tool in session.tools] == ["channel_history", "thread_replies"]
        outcome = await session.call("channel_history", HISTORY)

    assert outcome.failure is None
    assert outcome.result is not None
    document = json.loads(outcome.result["content"][0]["text"])
    assert len(document["messages"]) == 2
    assert set(fake.authorizations) == {"Bearer synthetic-session-token"}


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (429, "rate_limited", True),
        (500, "server_error", True),
        (503, "server_error", True),
        (403, "refused", False),
        (401, "unauthorized", False),
        (400, "protocol", False),
    ],
)
async def test_an_http_failure_of_a_call_is_classified_and_the_session_goes_on(
    status: int, kind: str, retryable: bool
) -> None:
    fake = fake_hearth()
    fake.answer_http(status, "7")
    async with session_with(fake) as session:
        failed = await session.call("channel_history", HISTORY)
        after = await session.call("channel_history", HISTORY)

    assert failed.result is None
    assert failed.failure is not None
    assert failed.failure.kind == kind
    assert failed.failure.retryable is retryable
    assert failed.failure.retry_after == (7.0 if kind == "rate_limited" else None)
    assert after.failure is None


async def test_a_call_past_its_timeout_fails_as_a_timeout() -> None:
    fake = fake_hearth()
    fake.fail("channel_history", "slow")
    async with session_with(fake, timeout=0.3) as session:
        outcome = await session.call("channel_history", HISTORY)
        after = await session.call("channel_history", HISTORY)

    assert outcome.failure == CallFailure("timeout", "no answer within the recipe's rate.timeout")
    assert after.failure is None


async def test_a_network_failure_fails_one_call_and_not_the_session() -> None:
    fake = fake_hearth()

    class Flaky(httpx2.AsyncBaseTransport):
        def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
            self.inner = inner
            self.fail_next = False

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            if self.fail_next and b"tools/call" in request.content:
                self.fail_next = False
                raise httpx2.ConnectError("synthetic: connection reset")
            return await self.inner.handle_async_request(request)

    async with fake.asgi() as app:
        flaky = Flaky(httpx2.ASGITransport(app=app))
        async with open_session(
            HttpEndpoint(URL), policy=POLICY, timeout=5.0, http_transport=flaky
        ) as session:
            flaky.fail_next = True
            failed = await session.call("channel_history", HISTORY)
            after = await session.call("channel_history", HISTORY)

    assert failed.failure is not None
    assert failed.failure.kind == "transport"
    assert "ConnectError" in failed.failure.reason
    assert after.failure is None


def test_a_transport_that_found_its_streams_closed_does_not_hide_the_failure() -> None:
    """A server that stalls the handshake: the session gives up on initialize, and the transport's
    request, timed out at the same moment, finds the session's streams closed. Both reach the
    task group; the timeout is the failure (tests/test_failures_db.py meets it on a socket)."""
    timeout = MCPError(code=types.REQUEST_TIMEOUT, message="Request 'initialize' timed out")
    raced = BaseExceptionGroup(
        "transport", [BaseExceptionGroup("session", [timeout]), anyio.ClosedResourceError()]
    )
    assert _unwrap(raced) is timeout
    assert _unwrap(BaseExceptionGroup("one", [anyio.BrokenResourceError()])) is not None
    assert _unwrap(BaseExceptionGroup("two", [ValueError("a"), timeout])) is None


async def test_an_is_error_result_is_a_result() -> None:
    fake = fake_hearth()
    fake.fail("channel_history", "error")
    async with session_with(fake) as session:
        outcome = await session.call("channel_history", HISTORY)

    assert outcome.failure is None
    assert outcome.is_error


async def test_a_redirect_out_of_the_allowed_path_is_refused_before_it_is_sent() -> None:
    fake = fake_hearth()
    redirected: list[str] = []

    async with fake.asgi() as served:

        async def app(scope: Any, receive: Any, send: Any) -> None:
            if scope["path"] == "/v1/mcp":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 307,
                        "headers": [(b"location", b"https://hearth.example/admin/mcp")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            redirected.append(scope["path"])
            await served(scope, receive, send)

        policy = EgressPolicy.build(endpoints=["https://hearth.example/v1"])
        with pytest.raises(EgressRefused, match="not in the allowlist"):
            async with open_session(
                HttpEndpoint("https://hearth.example/v1/mcp"),
                policy=policy,
                timeout=5.0,
                http_transport=httpx2.ASGITransport(app=app),
            ):
                pass  # pragma: no cover - the handshake is refused

    assert redirected == []


async def test_a_redirect_to_another_host_is_not_followed() -> None:
    fake = fake_hearth()
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(dict(scope["headers"])[b"host"].decode())
        await send(
            {
                "type": "http.response.start",
                "status": 307,
                "headers": [(b"location", b"https://other.example/mcp")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    policy = EgressPolicy.build(endpoints=[HOST, "other.example"])
    with pytest.raises(ConnectError):
        async with open_session(
            HttpEndpoint(URL),
            policy=policy,
            timeout=5.0,
            http_transport=httpx2.ASGITransport(app=app),
        ):
            pass  # pragma: no cover - the handshake fails
    assert set(seen) == {HOST}
    del fake


async def test_a_session_on_the_network_checks_its_host_before_connecting() -> None:
    calls: list[str] = []
    with pytest.raises(EgressRefused, match="internal address"):
        async with open_session(
            HttpEndpoint(URL),
            policy=POLICY,
            timeout=5.0,
            resolver=resolver({HOST: ["10.0.0.5"]}, calls),
        ):
            pass  # pragma: no cover - refused
    assert calls == [HOST]


async def test_a_stdio_command_outside_the_allowlist_is_not_started() -> None:
    with pytest.raises(EgressRefused, match="MEMINI_MCP_ALLOWED_COMMANDS"):
        async with open_session(
            StdioEndpoint(command="/bin/sh", args=("-c", "true")), policy=POLICY, timeout=1.0
        ):
            pass  # pragma: no cover - refused


# --- Budget and retries ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


async def test_the_budget_keeps_to_the_calls_per_minute_and_the_max_calls() -> None:
    clock = Clock()
    budget = Budget(Rate(calls_per_minute=2, max_calls=4), clock=clock, sleep=clock.sleep)

    assert [await budget.take() for _ in range(4)] == [True] * 4
    assert clock.waits == [60.0]  # the third call waits for the minute of the first
    assert budget.spent
    assert await budget.take() is False
    assert budget.calls == 4


async def test_a_pause_holds_every_call() -> None:
    clock = Clock()
    budget = Budget(Rate(calls_per_minute=100), clock=clock, sleep=clock.sleep)
    await budget.take()
    budget.pause(12.5)
    await budget.take()
    assert clock.waits == [12.5]


def test_the_retry_delay_backs_off_or_follows_the_server() -> None:
    policy = RetryPolicy(attempts=5, base=1.0, cap=5.0, max_retry_after=30.0)
    assert [policy.delay(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert policy.delay(1, retry_after=12.0) == 12.0
    assert policy.delay(1, retry_after=600.0) == 30.0
    with pytest.raises(ValueError, match="retry policy"):
        RetryPolicy(attempts=0)


# --- Drift and the calls to make ------------------------------------------------------------------


SENT: Final = ("channel", "oldest", "latest", "limit", "cursor")
TOOLS: Final = (
    ToolInfo("channel_history", {"properties": {key: {"type": "string"} for key in SENT}}),
    ToolInfo("thread_replies", {"type": "object"}),  # declares no properties: not checked
)


@pytest.mark.parametrize(
    ("server", "errors", "warnings"),
    [
        (ServerInfo("hearth-mcp", "2.9"), [], []),
        (ServerInfo("hearth-mcp", "3.0"), [], ["matches none of the recipe's server.versions"]),
        (ServerInfo("hearth-mcp", None), [], ["did not report its version"]),
        (ServerInfo("mill-mcp", "2.0"), ["another name than hearth-mcp"], []),
        (None, ["did not report its name"], []),
    ],
)
def test_drift_compares_the_server_with_the_recipe(
    server: ServerInfo | None, errors: list[str], warnings: list[str]
) -> None:
    drift = check_drift(hearth_recipe(), server, TOOLS)
    assert len(drift.errors) == len(errors)
    assert all(e in found for e, found in zip(errors, drift.errors, strict=True))
    assert len(drift.warnings) == len(warnings)
    assert all(w in found for w, found in zip(warnings, drift.warnings, strict=True))


def call(check: str = "page", *, tool: str = "channel_history", page: int = 0) -> MissingCall:
    return MissingCall(
        call="list",
        tool=tool,
        arguments={"channel": CHANNEL, "page": page},
        page=page,
        item_id=None,
        check=check,  # pyright: ignore[reportArgumentType]
        reason="synthetic",
    )


def test_the_calls_to_make_leave_out_max_pages_and_calls_out_of_tries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memini.executors import core

    named = (call(), call(page=1), call("max_pages", page=2), call(page=0))

    def missing(window: WindowInput) -> tuple[MissingCall, ...]:
        return named

    monkeypatch.setattr(core, "missing_calls", missing)
    window = WindowInput(hearth_recipe(), CHANNEL, WINDOW_1.start, WINDOW_1.end, ())

    assert calls_to_make(window, {}, 3) == [named[0], named[1]]
    assert calls_to_make(window, {call_identity(named[0]): 3}, 3) == [named[1]]


def test_a_window_with_nothing_stored_names_the_first_list_call() -> None:
    window = WindowInput(hearth_recipe(), CHANNEL, WINDOW_1.start, WINDOW_1.end, ())
    (first,) = calls_to_make(window, {}, 3)
    assert (first.call, first.tool, first.page) == ("list", "channel_history", 0)


# --- The OAuth refresher --------------------------------------------------------------------------


SECRET: Final = "s3cr3t"  # noqa: S105 - fake, for the Basic header


def oauth(endpoint: str = f"https://{HOST}/oauth/token", secret: str | None = SECRET) -> Any:
    now = datetime.now(UTC)
    info = CredentialInfo(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        workspace="acme",
        owner_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        source="hearth",
        kind="oauth",
        key_id="0" * 16,
        generation=1,
        expires_at=None,
        token_endpoint=endpoint,
        client_id="memini test",
        scope="read",
        refreshable=True,
        created_at=now,
        updated_at=now,
        refreshed_at=None,
    )
    return OAuthCredential(
        info,
        OAuthTokens(access_token=SecretStr("old-access"), refresh_token=SecretStr("old-refresh")),
        OAuthClient(
            token_endpoint=endpoint,
            client_id="memini test",
            client_secret=None if secret is None else SecretStr(secret),
            scope="read",
        ),
    )


def token_service(
    answer: Callable[[httpx2.Request], httpx2.Response], seen: list[httpx2.Request]
) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return answer(request)

    return httpx2.MockTransport(handle)


async def test_the_refresher_sends_the_refresh_token_with_basic_client_auth() -> None:
    seen: list[httpx2.Request] = []
    transport = token_service(
        lambda request: httpx2.Response(
            200,
            json={
                "access_token": "new-access",
                "token_type": "bearer",
                "expires_in": 600,
                "refresh_token": "new-refresh",
            },
        ),
        seen,
    )
    refresh = token_refresher(POLICY, http_transport=transport)

    before = datetime.now(UTC)
    tokens = await refresh(oauth())

    assert tokens.access_token.get_secret_value() == "new-access"
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.get_secret_value() == "new-refresh"
    assert tokens.expires_at is not None
    assert before + timedelta(seconds=590) < tokens.expires_at
    (request,) = seen
    assert dict(httpx2.QueryParams(request.content.decode())) == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
        "scope": "read",
    }
    assert request.headers["authorization"] == "Basic " + base64.b64encode(
        b"memini%20test:s3cr3t"
    ).decode("ascii")


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (httpx2.Response(400, json={"error": "invalid_grant", "error_description": "old-refresh"}),
         "HTTP 400, invalid_grant"),
        (httpx2.Response(200, json={"token_type": "bearer"}), "no access_token"),
        (httpx2.Response(200, json={"access_token": "x", "token_type": "mac"}), "not a bearer"),
        (httpx2.Response(200, text="old-refresh"), "not JSON"),
    ],
)  # fmt: skip
async def test_the_refresher_says_why_no_tokens_came_and_never_quotes_the_answer(
    answer: httpx2.Response, message: str
) -> None:
    refresh = token_refresher(POLICY, http_transport=token_service(lambda r: answer, []))
    with pytest.raises(RefreshFailed, match=message) as refused:
        await refresh(oauth(secret=None))
    assert "old-refresh" not in str(refused.value)


async def test_the_refresher_refuses_a_token_endpoint_outside_the_allowlist() -> None:
    seen: list[httpx2.Request] = []
    refresh = token_refresher(
        POLICY, http_transport=token_service(lambda r: httpx2.Response(200), seen)
    )
    with pytest.raises(EgressRefused):
        await refresh(oauth(endpoint="https://login.example/token"))
    assert seen == []


# --- Source settings ------------------------------------------------------------------------------


@pytest.fixture
def parser() -> Any:
    from hearth_helpers import hearth_parser

    register_parser("hearth.thread", hearth_parser(), replace=True)
    yield
    unregister_parser("hearth.thread")


def test_a_source_holds_its_recipe_text_with_its_name_and_digest(parser: None) -> None:
    text = HEARTH_RECIPE_PATH.read_text(encoding="utf-8")
    ref, recipe = inline_recipe(text)
    assert (ref.name, ref.toml, ref.bundled) == ("hearth", text, False)
    assert ref.digest is not None
    assert len(ref.digest) == 64

    config = McpConfig(endpoint=HttpEndpoint(URL), recipe=ref, window={"initial": "3d"})
    assert McpConfig.from_json(config.to_json()) == config
    stdio = McpConfig(
        endpoint=StdioEndpoint("/usr/bin/hearth", ("--stdio",), "HEARTH_TOKEN"),
        recipe=RecipeRef(name="hearth"),
        window={},
    )
    assert stdio.to_json()["recipe"] == {"bundled": "hearth"}
    assert McpConfig.from_json(stdio.to_json()) == stdio
    assert check_stream_keys(["C0002", "C0001", "C0002"], recipe) == ("C0001", "C0002")
    assert check_window({"backfill": None, "history": "90d"}, recipe) == {
        "backfill": None,
        "history": "90d",
    }


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("format = 1\n", "not a valid recipe"),
        ("format = [\n", "not TOML"),
        ("x" * (256 * 1024 + 1), "at most"),
    ],
)
def test_a_recipe_that_is_not_one_is_refused(parser: None, text: str, reason: str) -> None:
    with pytest.raises(McpConfigError, match=reason) as refused:
        inline_recipe(text)
    assert refused.value.setting == "recipe"


def test_every_stored_form_of_a_recipe_reads_as_the_same_recipe(parser: None) -> None:
    import tomllib

    from memini.executors.agent import RECIPE_KEY, source_recipe
    from memini.executors.source import load_recipe_ref, recipe_ref

    text = HEARTH_RECIPE_PATH.read_text(encoding="utf-8")
    held, recipe = inline_recipe(text)
    forms: list[object] = [held.to_json(), text, tomllib.loads(text)]
    for form in forms:
        ref = recipe_ref(form)
        assert ref is not None
        assert ref.name == "hearth"
        assert not ref.bundled
        assert load_recipe_ref(ref) == recipe
        assert source_recipe({RECIPE_KEY: form}) == recipe  # the agent executor reads it too
    assert recipe_ref({"bundled": "hearth"}) == RecipeRef(name="hearth")
    assert recipe_ref(None) is None
    assert recipe_ref(12) is None


def test_a_bundled_recipe_that_does_not_ship_is_refused() -> None:
    from memini.executors.source import bundled_recipe

    with pytest.raises(McpConfigError, match="no recipe named"):
        bundled_recipe("hearth")
    with pytest.raises(McpConfigError, match="recipe name"):
        bundled_recipe("../hearth")


@pytest.mark.parametrize(
    ("endpoint", "setting"),
    [
        (HttpEndpoint("https://elsewhere.example/mcp"), "endpoint.url"),
        (StdioEndpoint("/bin/sh"), "endpoint.command"),
        (StdioEndpoint("/usr/bin/hearth", ("a\nb",)), "endpoint.args"),
        (StdioEndpoint("/usr/bin/hearth", (), "PATH"), "endpoint.credential_env"),
        (StdioEndpoint("/usr/bin/hearth", (), "lower"), "endpoint.credential_env"),
    ],
)
def test_an_endpoint_the_policy_refuses_names_its_setting(
    endpoint: HttpEndpoint | StdioEndpoint, setting: str
) -> None:
    policy = EgressPolicy.build(endpoints=[HOST], commands=["/usr/bin/hearth"])
    with pytest.raises(McpConfigError) as refused:
        check_endpoint(endpoint, policy)
    assert refused.value.setting == setting


def test_stream_keys_and_window_are_checked_with_the_recipe() -> None:
    recipe = hearth_recipe()
    for keys in ([], ["general"], ["C0001"] * 1 + [f"C{n:04d}" for n in range(200)]):
        with pytest.raises(McpConfigError) as refused:
            check_stream_keys(keys, recipe)
        assert refused.value.setting == "streams"
    for window, setting in (
        ({"later": "1d"}, "window.later"),
        ({"initial": "soon"}, "window.initial"),
        ({"overlap": None}, "window.overlap"),
    ):
        with pytest.raises(McpConfigError) as refused:
            check_window(window, recipe)
        assert refused.value.setting == setting


async def test_a_budget_of_one_call_a_minute_waits_a_minute_for_each_next_call() -> None:
    clock = Clock()
    budget = Budget(Rate(calls_per_minute=1), clock=clock, sleep=clock.sleep)
    with anyio.fail_after(1):
        for _ in range(3):
            await budget.take()
    assert clock.waits == [60.0, 60.0]
