"""An MCP session with an mcp source, for the service executor: the official SDK's client over
streamable HTTP or stdio, its calls returned as raw results or as failures to retry.

**Connecting** (:func:`open_session`). An HTTP endpoint is checked against the egress policy first:
its URL, and, when the session reaches the network, every address its host resolves to
(:mod:`memini.executors.egress`); a refusal raises :class:`~memini.executors.egress.EgressRefused`
before any connection. The session then runs on :class:`~memini.executors.transport.
GuardedTransport`, which checks every request again, a redirect's too. The credential is sent as
``Authorization: Bearer`` by that transport; the SDK's OAuth provider is never used (the credential
store refreshes OAuth tokens: :mod:`memini.executors.oauth`). A stdio endpoint must be an allowed
command; the credential goes into the environment variable the source names, and the server's
stderr is discarded (it is not redacted, and may hold what the server read). The SDK negotiates the
protocol (``mode="auto"``: ``server/discover``, else the initialize handshake), its response cache
is off, and every request has the recipe's ``rate.timeout``. On entry the session lists the
server's tools, for the drift check (:func:`memini.executors.core.check_drift`).

**Calls** (:meth:`McpSession.call`). A call returns the result object as the server sent it, a JSON
object, before anything reads it: the SDK's output schema validation is not applied, as the store
keeps what the server said and the recipe's checks judge it. A call that has no result is a
:class:`CallFailure`, classified by what the transport saw (:class:`~memini.executors.transport.
CallProbe`) and the JSON-RPC error:

- ``timeout``: no answer within ``rate.timeout``. Retried.
- ``transport``: the network failed (refused, reset, timed out below HTTP). Retried.
- ``rate_limited``: HTTP 429, with the server's ``Retry-After``. Retried after a pause of every
  call.
- ``server_error``: HTTP 408 or 5xx, or a JSON-RPC internal error. Retried.
- ``unauthorized``: HTTP 401. After one OAuth refresh the call goes on; otherwise the run ends.
- ``refused``: HTTP 403. Not retried.
- ``closed``: the connection ended (a stdio server exited). The run ends.
- ``protocol``: any other error, or an answer that is not a tool result. Not retried.

A result with ``isError`` is a result: it is stored, and the executor retries it as a failed call
(docs/recipes.md, Decoding). No failure's ``reason`` holds a value of the request or the answer.
"""

import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import RootModel, SecretStr, ValidationError

from memini import __version__
from memini.executors.core import ToolInfo
from memini.executors.egress import EgressPolicy, EgressRefused
from memini.executors.source import Endpoint, HttpEndpoint
from memini.executors.transport import (
    BearerToken,
    CallProbe,
    GuardedTransport,
    Resolver,
    network_transport,
    preflight,
    probing,
    system_resolver,
)
from memini.responses import ServerInfo

__all__ = [
    "CLIENT_NAME",
    "MAX_TOOL_PAGES",
    "CallFailure",
    "CallOutcome",
    "ConnectError",
    "FailureKind",
    "McpSession",
    "open_session",
]

CLIENT_NAME: Final = "memini"
"""``clientInfo.name`` the service executor sends."""

MAX_TOOL_PAGES: Final = 100
"""Pages of ``tools/list`` read at most; a server that pages on is read that far."""

FailureKind = Literal[
    "timeout",
    "transport",
    "rate_limited",
    "server_error",
    "unauthorized",
    "refused",
    "closed",
    "protocol",
]


class _RawResult(RootModel[dict[str, Any]]):
    """A result read as the JSON object it is, nothing taken out or added."""


@dataclass(frozen=True, slots=True)
class CallFailure:
    """Why a call has no result; see the module for the kinds."""

    kind: FailureKind
    reason: str
    """For the run report and the log: the kind and the HTTP status or JSON-RPC code, never a
    value of the request or the answer."""
    retry_after: float | None = None
    """Seconds the server asked to wait (``rate_limited``)."""

    @property
    def retryable(self) -> bool:
        return self.kind in ("timeout", "transport", "rate_limited", "server_error")

    @property
    def fatal(self) -> bool:
        """The session cannot go on: no later call can succeed."""
        return self.kind in ("unauthorized", "closed")


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """A call's result, as the server sent it, or why there is none."""

    result: dict[str, Any] | None
    failure: CallFailure | None = None

    @property
    def is_error(self) -> bool:
        """The server answered with a result that says the call failed (``isError``)."""
        return self.result is not None and self.result.get("isError") is True


class ConnectError(Exception):
    """The session could not be opened: the handshake failed or timed out, or the server refused
    the credential. ``failure`` says how, as for a call."""

    def __init__(self, failure: CallFailure) -> None:
        super().__init__(f"could not open an MCP session with the source: {failure.reason}")
        self.failure = failure


def _classify(error: MCPError | None, probe: CallProbe) -> CallFailure:
    status = probe.status
    code = None if error is None else error.code
    where = f"HTTP {status}" if status is not None else f"JSON-RPC error {code}"
    if probe.transport_error is not None:
        return CallFailure("transport", f"the network failed ({probe.transport_error})")
    if code == types.REQUEST_TIMEOUT:
        return CallFailure("timeout", "no answer within the recipe's rate.timeout")
    if status == 429:
        return CallFailure(
            "rate_limited",
            "the server asked to slow down (HTTP 429)",
            retry_after=probe.retry_after,
        )
    if status == 401:
        return CallFailure("unauthorized", "the server refused the credential (HTTP 401)")
    if status == 403:
        return CallFailure("refused", "the server refused the call (HTTP 403)")
    if status is not None and (status == 408 or status >= 500):
        return CallFailure("server_error", f"the server failed ({where})")
    if code == types.CONNECTION_CLOSED:
        return CallFailure("closed", "the connection to the server ended")
    if code == types.INTERNAL_ERROR and (status is None or status < 400):
        return CallFailure("server_error", f"the server failed (JSON-RPC error {code})")
    return CallFailure("protocol", f"the server answered with an error ({where})")


def _is_tool_result(raw: Mapping[str, Any]) -> bool:
    result_type = raw.get("resultType", "complete")
    return result_type == "complete" and isinstance(raw.get("content"), list)


def _unwrap(group: BaseExceptionGroup[BaseException]) -> BaseException | None:
    """The exception a task group of the SDK wrapped: a refusal of the egress policy if the group
    holds one, else its only exception; ``None`` when it holds several."""
    refused, _ = group.split(EgressRefused)
    leaves: list[BaseException] = []

    def flatten(found: BaseException) -> None:
        if isinstance(found, BaseExceptionGroup):
            for inner in cast(BaseExceptionGroup[BaseException], found).exceptions:
                flatten(inner)
        else:
            leaves.append(found)

    flatten(refused if refused is not None else group)
    if refused is not None or len(leaves) == 1:
        return leaves[0]
    return None


class McpSession:
    """An open session with the server of an mcp source; see the module."""

    def __init__(
        self,
        client: Client,
        bearer: BearerToken,
        *,
        timeout: float,
        server: ServerInfo | None,
        tools: tuple[ToolInfo, ...],
    ) -> None:
        self._client = client
        self._bearer = bearer
        self._timeout = timeout
        self.server = server
        """What the server reported (``serverInfo``); ``None`` when it did not identify itself."""
        self.tools = tools
        """The tools the server lists, with their input schemas."""

    def use_token(self, token: SecretStr | None) -> None:
        """Send ``token`` with the requests from now on (an OAuth credential just refreshed).
        Only an HTTP session reads it; a stdio server got its credential when it started."""
        self._bearer.set(token)

    async def _send(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(name=tool, arguments=dict(arguments))
        )
        result = await self._client.session.send_request(
            request, _RawResult, request_read_timeout_seconds=self._timeout
        )
        return result.root

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> CallOutcome:
        """Call ``tool`` with ``arguments``; the result as the server sent it, or the failure."""
        with probing() as probe:
            try:
                try:
                    raw = await self._send(tool, arguments)
                except MCPError as error:
                    if error.code != types.HEADER_MISMATCH:
                        raise
                    # 2026-07-28: the tool's listing is stale; the spec's recovery is to list the
                    # tools again and send the call once more.
                    await _list_tools(self._client)
                    raw = await self._send(tool, arguments)
            except MCPError as error:
                return CallOutcome(None, _classify(error, probe))
            except ValidationError:
                return CallOutcome(
                    None, CallFailure("protocol", "the answer is not a valid tools/call result")
                )
        if not _is_tool_result(raw):
            return CallOutcome(
                None,
                CallFailure("protocol", "the answer is not a complete tool result (content)"),
            )
        return CallOutcome(raw)


async def _list_tools(client: Client) -> tuple[ToolInfo, ...]:
    found: list[ToolInfo] = []
    cursor: str | None = None
    for _ in range(MAX_TOOL_PAGES):
        page = await client.list_tools(cursor=cursor)
        found.extend(ToolInfo(tool.name, dict(tool.input_schema)) for tool in page.tools)
        cursor = page.next_cursor
        if not cursor:
            break
    return tuple(found)


@asynccontextmanager
async def open_session(
    endpoint: Endpoint,
    *,
    policy: EgressPolicy,
    timeout: float,
    token: SecretStr | None = None,
    http_transport: httpx2.AsyncBaseTransport | None = None,
    resolver: Resolver = system_resolver,
    max_connections: int = 16,
) -> AsyncGenerator[McpSession]:
    """An open session with ``endpoint`` for the duration of the block; see the module.

    ``http_transport`` replaces the transport that reaches the network (tests: an in-process
    server); the guard still checks every request, but no address is resolved. ``resolver`` is
    how host names are resolved for the egress check.

    The SDK runs its transport in task groups, which wrap what is raised in them, the block's own
    exceptions included: the one exception a group holds is raised as it is, and a refusal of the
    egress policy before anything else.

    Raises:
        EgressRefused: the endpoint, an address of its host, or a request of the session is not
            allowed; raised before any connection to it.
        ConnectError: the session could not be opened.
    """
    try:
        async with _open(
            endpoint,
            policy=policy,
            timeout=timeout,
            token=token,
            http_transport=http_transport,
            resolver=resolver,
            max_connections=max_connections,
        ) as session:
            yield session
    except BaseExceptionGroup as group:
        found = _unwrap(group)
        if found is None:
            raise
        raise found from None


@asynccontextmanager
async def _open(
    endpoint: Endpoint,
    *,
    policy: EgressPolicy,
    timeout: float,
    token: SecretStr | None,
    http_transport: httpx2.AsyncBaseTransport | None,
    resolver: Resolver,
    max_connections: int,
) -> AsyncGenerator[McpSession]:
    bearer = BearerToken()
    async with AsyncExitStack() as stack:
        if isinstance(endpoint, HttpEndpoint):
            if http_transport is None:
                await preflight(policy, endpoint.url, resolver)
                inner = network_transport(
                    policy, resolver=resolver, max_connections=max_connections
                )
            else:
                policy.check_url(endpoint.url)
                inner = http_transport
            bearer.set(token)
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    transport=GuardedTransport(inner, policy, bearer),
                    timeout=httpx2.Timeout(timeout),
                    follow_redirects=False,
                    trust_env=False,
                    headers={"User-Agent": f"{CLIENT_NAME}/{__version__}"},
                )
            )
            transport = streamable_http_client(endpoint.url, http_client=http)
        else:
            command = policy.check_command(endpoint.command)
            env: dict[str, str] = {}
            if endpoint.credential_env is not None and token is not None:
                env[endpoint.credential_env] = token.get_secret_value()
            errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
            transport = stdio_client(
                StdioServerParameters(command=command, args=list(endpoint.args), env=env),
                errlog=errlog,
            )
        client = Client(
            transport,
            mode="auto",
            cache=None,
            read_timeout_seconds=timeout,
            client_info=types.Implementation(name=CLIENT_NAME, version=__version__),
        )
        with probing() as probe:
            try:
                await stack.enter_async_context(client)
                tools = await _list_tools(client)
            except MCPError as error:
                raise ConnectError(_classify(error, probe)) from None
            except BaseExceptionGroup as group:  # the handshake failed inside the SDK's tasks
                found = _unwrap(group)
                if not isinstance(found, MCPError):
                    raise
                raise ConnectError(_classify(found, probe)) from None
        info = client.server_info
        server = None if info is None else ServerInfo(name=info.name, version=info.version)
        yield McpSession(client, bearer, timeout=timeout, server=server, tools=tools)
