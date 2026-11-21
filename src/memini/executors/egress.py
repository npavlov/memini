"""Where the service executor may connect: the egress policy of mcp sources.

A source names an MCP server the service reaches on its own, so whoever may configure sources could
otherwise point the service at anything it can reach: a database next to it, a cloud metadata
service, an admin port on localhost (principle 11). The policy closes that, before any connection:

**Endpoints** (``mcp_allowed_endpoints``). An HTTP endpoint must match an entry:

- a host name, ``mcp.example.com``, optionally with a port, ``mcp.example.com:8443``: any path on
  that host, at that port or the default port of the scheme. ``*.example.com`` matches every host
  below ``example.com`` (not ``example.com`` itself);
- a URL prefix, ``https://mcp.example.com/v1``: the same scheme, host and port, and a path that is
  the prefix's or below it (``/v1`` and ``/v1/mcp``, not ``/v10``).

The list is empty by default: no HTTP endpoint is allowed until the operator names one. The scheme
is https; plain http needs ``mcp_allow_http``. A URL never holds a user name, a password, a query
or a fragment: a credential comes from the credential store, and a stored URL is shown and logged.

**Internal addresses** (``mcp_allowed_networks``). Every address an endpoint's host resolves to is
checked, and one that is not a public unicast address (loopback, private, link-local and so the
cloud metadata address 169.254.169.254, carrier-grade NAT, unique local, multicast, reserved,
unspecified, and an IPv4 address inside an IPv6 one) is refused unless it is in a network listed
here. A host with one refused address is refused whole: a resolver that answers with a public and a
private address could otherwise steer the connection. The check runs when the connection is made,
on the addresses the connection then uses (:mod:`memini.executors.transport` connects to the
address it checked, so a DNS answer that changes in between cannot slip through), and again for
every request, a redirect included.

**Redirects.** The MCP SDK follows a redirect only within the endpoint's origin (same scheme, host
and port, or http to https on the same host), so none crosses to another host; each one is a new
request, checked again against the allowlist, the path prefix included.

**Commands.** A stdio source must match an entry in ``mcp_allowed_invocations`` (executable,
argument vector and credential variable) or an executable in ``mcp_allowed_commands``. Profiles
take precedence when both settings list a path. Unprofiled commands accept source-supplied
arguments. The process receives the SDK's default environment plus the configured credential.

Every refusal is an :class:`EgressRefused` whose message names the rule and the setting, never the
URL, host or address: a message goes into run reports and logs.
"""

import ipaddress
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Self
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:  # memini.config checks its endpoint rules with this module
    from memini.config import Settings, StdioInvocation

__all__ = [
    "DEFAULT_PORTS",
    "MAX_URL_LENGTH",
    "EgressPolicy",
    "EgressRefused",
    "EndpointRule",
    "HttpTarget",
    "IPAddress",
    "IPNetwork",
    "embedded_ipv4",
    "is_internal",
    "parse_endpoint_url",
]

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
type Scheme = Literal["https", "http"]

DEFAULT_PORTS: Final[dict[str, int]] = {"https": 443, "http": 80}
MAX_URL_LENGTH: Final = 2048

_HOST: Final = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_HOST_RULE: Final = re.compile(r"(?P<host>(?:\*\.)?[^:/\s]+|\[[0-9a-fA-F:.]+\])(?::(?P<port>\d+))?")
_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")


class EgressRefused(Exception):
    """A connection the egress policy does not allow. The message names the rule and the setting
    that would allow it, never the URL, the host or an address."""


# --- Addresses -----------------------------------------------------------------------------------


def embedded_ipv4(address: IPAddress) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries, if it is one of the forms that reach it: mapped
    (``::ffff:a.b.c.d``), 6to4, Teredo (the server), or the NAT64 well-known prefix."""
    if isinstance(address, ipaddress.IPv4Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[0]
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_internal(address: IPAddress) -> bool:
    """Whether ``address`` is anything but a public unicast address: loopback, private,
    link-local (the metadata service of clouds), shared (carrier-grade NAT), unique local,
    multicast, reserved, unspecified, documentation; or an IPv6 address carrying an IPv4 address
    that is one of those."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return is_internal(address.ipv4_mapped)  # a socket to it reaches the IPv4 address
    inner = embedded_ipv4(address)
    if inner is not None and is_internal(inner):
        return True
    return (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
    )


def _literal_address(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


# --- URLs ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpTarget:
    """An endpoint URL that passed the checks of its form (:func:`parse_endpoint_url`)."""

    url: str
    """The URL as it is stored and used: the scheme and the host in lower case."""
    scheme: Scheme
    host: str
    """Lower case; an IPv6 address without brackets."""
    port: int
    """The port, or the default port of the scheme."""
    path: str


def _check_host(host: str | None) -> str:
    name = (host or "").lower()
    if _literal_address(name) is not None:
        return name
    if len(name) > 253 or _HOST.fullmatch(name) is None:
        raise EgressRefused("the endpoint's host is missing or not a valid host name")
    return name


def parse_endpoint_url(url: str, *, allow_http: bool = False) -> HttpTarget:
    """``url`` as an :class:`HttpTarget`, or :class:`EgressRefused` naming why its form is refused:
    not https (or http with ``allow_http``), a user name, password, query or fragment, no host, a
    port that is not one, whitespace or control characters, longer than :data:`MAX_URL_LENGTH`.
    The allowlist is not consulted here (:meth:`EgressPolicy.check_url`)."""
    if not url or len(url) > MAX_URL_LENGTH or url != url.strip() or not url.isprintable():
        raise EgressRefused(
            f"an endpoint is a URL of at most {MAX_URL_LENGTH} characters, without whitespace or"
            " control characters"
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise EgressRefused("the endpoint is not a URL with a valid host and port") from None
    scheme = parts.scheme.lower()
    if scheme == "http" and not allow_http:
        raise EgressRefused(
            "plain http:// endpoints are not allowed (no TLS); use https://, or set"
            " MEMINI_MCP_ALLOW_HTTP=true"
        )
    if scheme not in ("https", "http"):
        raise EgressRefused("an endpoint is an https:// URL")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise EgressRefused(
            "an endpoint URL holds no user name or password: the credential comes from the"
            " credential store (memini credentials set)"
        )
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise EgressRefused("an endpoint URL has no query or fragment")
    if port == 0:
        raise EgressRefused("the endpoint's port is not a number from 1 to 65535")
    host = _check_host(parts.hostname)
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    path = parts.path or "/"
    typed: Scheme = "https" if scheme == "https" else "http"
    return HttpTarget(
        url=urlunsplit((typed, netloc, path, "", "")),
        scheme=typed,
        host=host,
        port=port if port is not None else DEFAULT_PORTS[typed],
        path=path,
    )


# --- The allowlist -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointRule:
    """One entry of ``mcp_allowed_endpoints``: a host (with ``*.`` for the hosts below a domain,
    and an optional port), or a URL prefix (scheme, host, port and path)."""

    host: str
    port: int | None = None
    """``None``: the default port of the URL's scheme."""
    scheme: Scheme | None = None
    """Set for a URL prefix only."""
    path: str = ""
    """The path prefix of a URL prefix, without a trailing ``/``; empty: any path."""

    @classmethod
    def parse(cls, text: str) -> Self:
        """The rule ``text`` writes. Raises :class:`ValueError` naming what is wrong."""
        entry = text.strip()
        if "://" in entry:
            target = parse_endpoint_url(entry, allow_http=True)
            explicit = urlsplit(entry).port
            return cls(
                host=target.host,
                port=explicit if explicit is not None else None,
                scheme=target.scheme,
                path=target.path.rstrip("/"),
            )
        match = _HOST_RULE.fullmatch(entry)
        if match is None:
            raise ValueError("an endpoint rule is a host name, host:port or a URL prefix")
        host = match.group("host").lower().strip("[]")
        wildcard = host.startswith("*.")
        name = host.removeprefix("*.")
        if _literal_address(name) is None and (
            len(name) > 253 or _HOST.fullmatch(name) is None or (wildcard and "." not in name)
        ):
            raise ValueError("an endpoint rule names a valid host, or *.domain")
        if wildcard and _literal_address(name) is not None:
            raise ValueError("*. goes before a domain name, not an address")
        port = match.group("port")
        if port is not None and not 1 <= int(port) <= 65535:
            raise ValueError("an endpoint rule's port is a number from 1 to 65535")
        return cls(host=host, port=None if port is None else int(port))

    def _host_matches(self, host: str) -> bool:
        if self.host.startswith("*."):
            return host.endswith(self.host[1:])
        return host == self.host

    def allows(self, target: HttpTarget) -> bool:
        """Whether ``target`` is inside this rule."""
        if not self._host_matches(target.host):
            return False
        if self.scheme is not None and target.scheme != self.scheme:
            return False
        port = self.port if self.port is not None else DEFAULT_PORTS[target.scheme]
        if target.port != port:
            return False
        if not self.path:
            return True
        return target.path == self.path or target.path.startswith(self.path + "/")


# --- The policy ----------------------------------------------------------------------------------


def _networks(values: Iterable[str]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(value, strict=False) for value in values)


@dataclass(frozen=True, slots=True, kw_only=True)
class EgressPolicy:
    """What the service executor may connect to; see the module. Build it with
    :meth:`from_settings`; tests build it directly."""

    endpoints: tuple[EndpointRule, ...] = ()
    networks: tuple[IPNetwork, ...] = ()
    """Internal networks an endpoint may resolve to all the same."""
    allow_http: bool = False
    commands: tuple[str, ...] = ()
    """Absolute paths of the executables a stdio source may run."""

    invocations: tuple["StdioInvocation", ...] = ()

    def __post_init__(self) -> None:
        if any(not os.path.isabs(command) for command in self.commands):
            raise ValueError("an allowed command is an absolute path")

    @classmethod
    def from_settings(cls, settings: "Settings") -> Self:
        """The policy of the ``mcp_*`` settings."""
        return cls(
            endpoints=tuple(EndpointRule.parse(entry) for entry in settings.mcp_allowed_endpoints),
            networks=_networks(settings.mcp_allowed_networks),
            allow_http=settings.mcp_allow_http,
            commands=tuple(os.path.normpath(str(path)) for path in settings.mcp_allowed_commands),
            invocations=settings.mcp_allowed_invocations,
        )

    @classmethod
    def build(
        cls,
        *,
        endpoints: Iterable[str] = (),
        networks: Iterable[str] = (),
        allow_http: bool = False,
        commands: Iterable[str] = (),
    ) -> Self:
        """A policy from the settings' text forms."""
        return cls(
            endpoints=tuple(EndpointRule.parse(entry) for entry in endpoints),
            networks=_networks(networks),
            allow_http=allow_http,
            commands=tuple(os.path.normpath(command) for command in commands),
        )

    def check_address(self, address: IPAddress) -> None:
        """Refuse ``address`` if it is internal (:func:`is_internal`) and in no allowed network."""
        if not is_internal(address):
            return
        candidates = [address]
        inner = embedded_ipv4(address)
        if inner is not None:
            candidates.append(inner)
        if any(candidate in network for candidate in candidates for network in self.networks):
            return
        raise EgressRefused(
            "the endpoint's host is an internal address (loopback, private, link-local or"
            " another that is not public); list its network in MEMINI_MCP_ALLOWED_NETWORKS to"
            " allow it"
        )

    def check_addresses(self, addresses: Iterable[IPAddress]) -> None:
        """Refuse a host unless every one of its addresses passes :meth:`check_address`."""
        found = list(addresses)
        if not found:
            raise EgressRefused("the endpoint's host did not resolve to an address")
        for address in found:
            self.check_address(address)

    def check_url(self, url: str) -> HttpTarget:
        """``url`` if its form is accepted, it is inside an allowed endpoint and, when its host is
        an address, that address is allowed; :class:`EgressRefused` otherwise. Host names are
        resolved and checked when a connection is made (:mod:`memini.executors.transport`)."""
        target = parse_endpoint_url(url, allow_http=self.allow_http)
        if not any(rule.allows(target) for rule in self.endpoints):
            raise EgressRefused(
                "the endpoint is not in the allowlist; list its host or a URL prefix of it in"
                " MEMINI_MCP_ALLOWED_ENDPOINTS"
            )
        literal = _literal_address(target.host)
        if literal is not None:
            self.check_address(literal)
        return target

    def check_command(
        self, command: str, args: tuple[str, ...] = (), credential_env: str | None = None
    ) -> str:
        """``command``, normalized, if it is one of the allowed executables; otherwise
        :class:`EgressRefused`."""
        if not command or not os.path.isabs(command) or "\0" in command:
            raise EgressRefused("a stdio command is the absolute path of an executable")
        normalized = os.path.normpath(command)
        profiles = [p for p in self.invocations if os.path.normpath(p.command) == normalized]
        if profiles:
            if any(p.args == args and p.credential_env == credential_env for p in profiles):
                return normalized
            raise EgressRefused("the stdio invocation must match MEMINI_MCP_ALLOWED_INVOCATIONS")
        if normalized not in self.commands:
            raise EgressRefused(
                "the command is not allowed; list its absolute path in MEMINI_MCP_ALLOWED_COMMANDS"
            )
        return normalized
