"""Where a repository lives: parse it, check it against the allowlist, before any process starts.

Accepted forms, with the transport each one uses:

- ``https://host[:port]/path``: https. A token may be sent, only to the hosts listed in
  ``git_https_token_hosts`` (:mod:`memini.git.command`).
- ``http://host[:port]/path``: http, only when ``git_allow_http`` is set. Off by default: plain http
  carries the code and any credential in clear text, and anyone on the path can change what is
  fetched (principle 11).
- ``ssh://[user@]host[:port]/path`` and the scp-like ``[user@]host:path``: ssh, with the key and the
  known_hosts file from the settings.
- An absolute path, or ``file:///path``: a repository on this machine, only under one of
  ``git_local_roots`` and never inside the mirror directory. Off by default: a local source lets
  whoever configures sources read any repository the service user can read, including the mirrors
  of other workspaces. The path is resolved (symlinks, ``..``) before it is checked, and git gets
  the resolved path.

Refused always: other schemes (``git://``, ``ftp://`` …), git's transport helpers
(``ext::…``, ``fd::…``), relative paths, a user name or password in an http(s) URL (credentials
come from the settings, never from a URL that is stored in the database), queries and fragments,
host names or paths that start with ``-``, whitespace at the ends and control characters.

git enforces the same transport again: every command runs with ``protocol.allow=never`` and only
the transport of its remote allowed.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from memini.git.errors import RemoteNotAllowed
from memini.git.options import GitOptions

__all__ = ["Remote", "Transport", "check_destination", "parse_remote"]

type Transport = Literal["https", "http", "ssh", "file"]

_HELPER: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*::")
_SCHEME: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://")
# A host name or IPv4 address, or an IPv6 address (which has a colon).
_HOST: Final = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|[0-9a-f.]*:[0-9a-f:.]*")
_USER: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
# [user@]host:path, where the host has no "/" (otherwise git reads a local path) and the path
# does not start with ":" (host::path names a transport helper) or "-".
_SCP_LIKE: Final = re.compile(
    r"(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]*)@)?"
    r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])"
    r":(?P<path>[^:\-].*)"
)


@dataclass(frozen=True, slots=True)
class Remote:
    """A source location that passed the checks: what git is given, and how it is reached.

    ``url`` never holds a credential, so it can be logged and shown in errors.
    """

    transport: Transport
    url: str
    host: str | None = None
    """The lower-case host name of a network remote; ``None`` for a local one."""
    port: int | None = None

    def __str__(self) -> str:
        return self.url


def parse_remote(location: str, options: GitOptions) -> Remote:
    """``location`` as a :class:`Remote`, or :class:`RemoteNotAllowed` naming the reason."""
    remote = _parse_remote(location, options)
    check_destination(remote, options)
    return remote


def check_destination(remote: Remote, options: GitOptions) -> None:
    if options.egress_proxy is None or remote.transport == "file":
        return
    if remote.transport != "https" or remote.host is None:
        raise RemoteNotAllowed("restricted network Git requires an HTTPS remote")
    host = f"[{remote.host}]" if ":" in remote.host else remote.host
    if f"https://{host}:{remote.port or 443}" not in options.allowed_origins:
        raise RemoteNotAllowed("the Git origin is not in MEMINI_GIT_ALLOWED_ORIGINS")


def _parse_remote(location: str, options: GitOptions) -> Remote:
    if not location or location != location.strip() or not location.isprintable():
        raise RemoteNotAllowed(
            "a source location is a URL or an absolute path, without whitespace at the ends or"
            " control characters"
        )
    if location.startswith("/"):
        return _local(location, options)
    if _HELPER.match(location):
        raise RemoteNotAllowed("git transport helpers (<transport>::<address>) are not allowed")
    if match := _SCHEME.match(location):
        scheme = match.group(1).lower()
        if scheme in ("https", "http"):
            return _http(scheme, location, options)
        if scheme == "ssh":
            return _ssh_url(location)
        if scheme == "file":
            return _file_url(location, options)
        raise RemoteNotAllowed(
            f"the scheme {scheme}:// is not allowed; use https://, ssh://, user@host:path or an"
            " absolute path"
        )
    if match := _SCP_LIKE.fullmatch(location):
        host = _check_host(match.group("host").strip("[]"))
        return Remote("ssh", location, host)
    raise RemoteNotAllowed(
        "not a URL, an scp-like ssh address (user@host:path) or an absolute path"
    )


def _split(location: str) -> tuple[str, str, str, int | None]:
    """Scheme, netloc, path and port of a URL with no query or fragment."""
    parts = urlsplit(location)
    if parts.query or parts.fragment or "?" in location or "#" in location:
        raise RemoteNotAllowed("a source URL has no query or fragment")
    try:
        port = parts.port
    except ValueError:
        raise RemoteNotAllowed(
            "the port of the source URL is not a number from 1 to 65535"
        ) from None
    if port == 0:
        raise RemoteNotAllowed("the port of the source URL is not a number from 1 to 65535")
    if parts.path in ("", "/"):
        raise RemoteNotAllowed("the source URL names no repository path")
    return parts.scheme.lower(), parts.netloc, parts.path, port


def _check_host(host: str | None) -> str:
    name = (host or "").lower()
    if not name or _HOST.fullmatch(name) is None:
        raise RemoteNotAllowed("the host of the source is missing or not a valid host name")
    return name


def _http(scheme: str, location: str, options: GitOptions) -> Remote:
    if scheme == "http" and not options.allow_http:
        raise RemoteNotAllowed(
            "plain http:// remotes are not allowed (no TLS); use https://, or set"
            " MEMINI_GIT_ALLOW_HTTP=true"
        )
    _, netloc, path, port = _split(location)
    parts = urlsplit(location)
    if parts.username is not None or parts.password is not None or "@" in netloc:
        raise RemoteNotAllowed(
            "an http(s) source URL holds no user name or password; the token comes from"
            " MEMINI_GIT_HTTPS_TOKEN"
        )
    host = _check_host(parts.hostname)
    transport: Transport = "https" if scheme == "https" else "http"
    return Remote(transport, urlunsplit((scheme, netloc, path, "", "")), host, port)


def _ssh_url(location: str) -> Remote:
    _, netloc, path, port = _split(location)
    parts = urlsplit(location)
    if parts.password is not None:
        raise RemoteNotAllowed(
            "an ssh source URL holds no password; the key comes from the settings"
        )
    if parts.username is not None and _USER.fullmatch(parts.username) is None:
        raise RemoteNotAllowed("the user name of the ssh source URL is not valid")
    host = _check_host(parts.hostname)
    return Remote("ssh", urlunsplit(("ssh", netloc, path, "", "")), host, port)


def _file_url(location: str, options: GitOptions) -> Remote:
    parts = urlsplit(location)
    if parts.query or parts.fragment or "?" in location or "#" in location:
        raise RemoteNotAllowed("a source URL has no query or fragment")
    if parts.netloc not in ("", "localhost"):
        raise RemoteNotAllowed("a file:// source URL names no host other than localhost")
    return _local(unquote(parts.path), options)


def _local(path: str, options: GitOptions) -> Remote:
    if not options.local_roots:
        raise RemoteNotAllowed(
            "local repositories are not allowed as sources; list the directories they may be in"
            " in MEMINI_GIT_LOCAL_ROOTS"
        )
    if not path.startswith("/") or "\0" in path:
        raise RemoteNotAllowed("a local source is an absolute path")
    resolved = Path(os.path.realpath(path))
    mirrors = Path(os.path.realpath(options.mirror_dir))
    if resolved.is_relative_to(mirrors):
        raise RemoteNotAllowed("a local source cannot be inside the mirror directory")
    roots = [Path(os.path.realpath(root)) for root in options.local_roots]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise RemoteNotAllowed(
            "the local source is outside the directories in MEMINI_GIT_LOCAL_ROOTS"
        )
    return Remote("file", str(resolved))
