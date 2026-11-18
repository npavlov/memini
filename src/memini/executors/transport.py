"""The HTTP transport of the service executor: the egress policy on every request, the credential
on every request, and what each request of a call came to.

Three layers, outermost first:

- :class:`GuardedTransport` (an httpx2 transport) checks each request's URL against the policy
  (:meth:`~memini.executors.egress.EgressPolicy.check_url`): the first request, and every request a
  redirect makes, before it is sent. It sets the bearer token (:class:`BearerToken`), which is never
  one of the client's default headers, so it goes only where the guard lets a request go. It
  records the outcome of each request in the :class:`CallProbe` of the call it serves, if any: the
  MCP SDK turns an HTTP error into a JSON-RPC error that has lost the status, and a 429's
  ``Retry-After``. The SDK runs each request in the context of the call that sent it, so a context
  variable carries the probe there. A failure of the network (refused, reset, timed out) becomes a
  ``502`` answer that the probe marks as such: one call fails, not the whole session.
- :func:`network_transport` is the inner transport that reaches the network: an httpx2 transport
  whose connection pool uses :class:`PinnedBackend`, with no proxy from the environment.
- :class:`PinnedBackend` resolves the host itself, refuses it unless every address passes
  :meth:`~memini.executors.egress.EgressPolicy.check_addresses`, and connects to an address it
  checked. TLS still verifies the certificate for the host name, which httpcore sends as SNI.

A refusal raises :class:`~memini.executors.egress.EgressRefused` before any socket is opened.
"""

import email.utils
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast

import anyio
import httpcore2
import httpx2
from pydantic import SecretStr

from memini.executors.egress import EgressPolicy, EgressRefused, IPAddress

__all__ = [
    "BearerToken",
    "CallProbe",
    "GuardedTransport",
    "PinnedBackend",
    "Resolver",
    "network_transport",
    "preflight",
    "probing",
    "retry_after",
    "system_resolver",
]

type Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]
"""Resolves a host name and port to the addresses a connection may use."""

TRANSPORT_FAILURE_STATUS: Final = 502
"""The status of the answer that stands for a request the network failed."""


@dataclass
class CallProbe:
    """What the HTTP requests of one call came to, as the transport saw them; the last one wins."""

    status: int | None = None
    """The HTTP status of the answer."""
    retry_after: float | None = None
    """Seconds the server asked to wait (``Retry-After``), when it said."""
    transport_error: str | None = None
    """The kind of network failure (``ConnectError``, ``ReadTimeout``, …), when the request
    failed below HTTP."""


_PROBE: ContextVar[CallProbe | None] = ContextVar("memini_mcp_call_probe", default=None)


@contextmanager
def probing() -> Generator[CallProbe]:
    """A probe for the requests made inside the block (and the tasks it starts)."""
    probe = CallProbe()
    token = _PROBE.set(probe)
    try:
        yield probe
    finally:
        _PROBE.reset(token)


def retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds from a ``Retry-After`` header: a number of seconds or an HTTP date."""
    if value is None:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN, infinite
        return None
    return max(0.0, seconds)


class BearerToken:
    """The credential sent with each request as ``Authorization: Bearer …``; replaced after an
    OAuth refresh. Held as a ``SecretStr``: its ``repr`` shows nothing."""

    def __init__(self, token: SecretStr | None = None) -> None:
        self._token = token

    def set(self, token: SecretStr | None) -> None:
        self._token = token

    def header(self) -> str | None:
        if self._token is None:
            return None
        return f"Bearer {self._token.get_secret_value()}"


class GuardedTransport(httpx2.AsyncBaseTransport):
    """The egress policy and the credential on every request; see the module."""

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        policy: EgressPolicy,
        bearer: BearerToken | None = None,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._bearer = bearer or BearerToken()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        probe = _PROBE.get()
        self._policy.check_url(str(request.url))  # raises EgressRefused: nothing is sent
        header = self._bearer.header()
        if header is not None:
            request.headers["Authorization"] = header
        try:
            response = await self._inner.handle_async_request(request)
        except httpx2.TransportError as error:
            if probe is not None:
                probe.transport_error = type(error).__name__
                probe.status = TRANSPORT_FAILURE_STATUS
            return httpx2.Response(TRANSPORT_FAILURE_STATUS, request=request)
        if probe is not None:
            probe.status = response.status_code
            probe.retry_after = retry_after(response.headers.get("retry-after"))
            probe.transport_error = None
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    """The addresses of ``host`` for a TCP connection, by the system's resolver, without
    repeats."""
    try:
        infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    found: list[IPAddress] = []
    for info in infos:
        text = str(info[4][0]).split("%", 1)[0]  # an IPv6 zone index is not part of the address
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if address not in found:
            found.append(address)
    return found


async def preflight(policy: EgressPolicy, url: str, resolver: Resolver = system_resolver) -> None:
    """Check ``url`` and every address of its host before a session or a request reaches the
    network, so that a refusal comes before anything starts; :class:`PinnedBackend` checks again
    at each connection.

    Raises:
        EgressRefused: the URL or an address of its host is not allowed.
    """
    target = policy.check_url(url)
    try:
        ipaddress.ip_address(target.host)
    except ValueError:
        policy.check_addresses(await resolver(target.host, target.port))
    # An address: check_url has checked it.


class PinnedBackend(httpcore2.AsyncNetworkBackend):
    """Connects only to addresses the policy allows, and to the address it checked; see the
    module."""

    def __init__(
        self,
        policy: EgressPolicy,
        resolver: Resolver = system_resolver,
        inner: httpcore2.AsyncNetworkBackend | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        # AnyIOBackend is typed as a stand-in when anyio is missing; anyio is a dependency.
        self._inner = inner or cast(httpcore2.AsyncNetworkBackend, httpcore2.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        try:
            addresses: list[IPAddress] = [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            addresses = await self._resolver(host, port)
        self._policy.check_addresses(addresses)  # raises EgressRefused: no socket is opened
        failure: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as error:
                failure = error
        if failure is None:  # pragma: no cover - check_addresses refuses an empty list
            raise httpcore2.ConnectError("no address to connect to")
        raise failure

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        raise EgressRefused("an MCP endpoint is reached over TCP, never a unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PinnedHTTPTransport(httpx2.AsyncHTTPTransport):
    """httpx2's transport with a connection pool on :class:`PinnedBackend`."""

    def __init__(self, backend: PinnedBackend, *, max_connections: int) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=httpx2.create_ssl_context(trust_env=False),
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=5.0,
            network_backend=backend,
        )


def network_transport(
    policy: EgressPolicy,
    *,
    resolver: Resolver = system_resolver,
    backend: httpcore2.AsyncNetworkBackend | None = None,
    max_connections: int = 16,
) -> httpx2.AsyncBaseTransport:
    """The transport that reaches the network for the service executor: connections only to
    addresses the policy allows (:class:`PinnedBackend` over ``backend``), no proxy from the
    environment, no retries of its own."""
    return _PinnedHTTPTransport(
        PinnedBackend(policy, resolver, backend), max_connections=max_connections
    )
