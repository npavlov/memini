"""The client side of the CLI: where the service is, the token, and HTTP calls to the API.

Client commands are HTTP clients of the running service (requirements, section 7). They read two
settings, from the environment only; they need no database URL and do not read the service's
settings or its TOML file:

- ``MEMINI_URL``: the service, e.g. ``http://127.0.0.1:6364`` (the default) or
  ``https://memini.example.com/prefix``. ``http://`` or ``https://``, and TLS is whatever the URL
  says. A URL with a user name or password is refused: the token is the credential.
- The token, a secret, in one of two forms: ``MEMINI_TOKEN``, the value itself, or
  ``MEMINI_TOKEN_FILE``, the path of a file that holds it (trailing line breaks are dropped).
  Setting both is an error. There is no option that takes the token on the command line, where
  shell history and ``ps`` would keep it.

The global options ``--url`` and ``--token-file`` override them for one run. The token is sent as
``Authorization: Bearer …`` and never printed; it is registered with :mod:`memini.logs`, and any
text from elsewhere is redacted before it is printed.

An error of the API (``{"error": {"code", "message", "request_id", "details"}}``) becomes one line,
``memini: <message> (<code>, HTTP <status>, request <id>)``, and an exit code:

- 401 and 403: :data:`~memini.cli.common.EXIT_DENIED`, the token was refused;
- 503 ``unavailable``, and a 502, 503 or 504 without the API's error body (a proxy in front):
  :data:`~memini.cli.common.EXIT_UNAVAILABLE`, as for a service that cannot be reached or does not
  answer within :data:`TIMEOUT` seconds;
- anything else: :data:`~memini.cli.common.EXIT_FAILURE`.

Redirects are not followed: the token goes to the URL given and nowhere else. The HTTP library logs
no request lines (:func:`quiet_http_logs`): a request line carries the query string, which can be
private text, such as a search query.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self, cast
from urllib.parse import quote, urlsplit

import httpx2
from pydantic import SecretStr

from memini import __version__, logs
from memini.auth import is_well_formed_token
from memini.cli.common import (
    EXIT_DENIED,
    EXIT_FAILURE,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    CommandError,
    printable,
)

__all__ = [
    "CLIENT_VARIABLES",
    "CONNECT_TIMEOUT",
    "DEFAULT_URL",
    "TIMEOUT",
    "TOKEN_FILE_VAR",
    "TOKEN_VAR",
    "URL_VAR",
    "Api",
    "ClientConfigError",
    "ClientSettings",
    "Json",
    "load_client_settings",
    "quiet_http_logs",
    "segment",
]

URL_VAR: Final = "MEMINI_URL"
TOKEN_VAR: Final = "MEMINI_TOKEN"  # noqa: S105 - the name of a variable, not a token
TOKEN_FILE_VAR: Final = "MEMINI_TOKEN_FILE"  # noqa: S105 - the name of a variable
DEFAULT_URL: Final = "http://127.0.0.1:6364"
CLIENT_VARIABLES: Final = (URL_VAR, TOKEN_VAR, TOKEN_FILE_VAR)
"""Every environment variable :func:`load_client_settings` reads."""

TIMEOUT: Final = 60.0
"""Seconds a request may take, from sending it to the end of the answer."""
CONNECT_TIMEOUT: Final = 10.0
"""Seconds to connect to the service."""

HTTP_LOGGERS: Final = ("httpx2", "httpcore2")
"""The loggers of the HTTP library: one INFO line per request, with its URL and query string."""

type Json = dict[str, Any]
type Params = Mapping[str, str | int | Sequence[str] | None]


class ClientConfigError(Exception):
    """The service URL or the token is missing or invalid. The text never holds a value."""


@dataclass(frozen=True, slots=True)
class ClientSettings:
    """Where the service is and the token to call it with."""

    url: str
    """The service's base URL, without a trailing slash and without credentials: safe to print."""
    token: SecretStr
    origin: str
    """Where the token came from, e.g. ``MEMINI_TOKEN_FILE``: for messages."""

    def redact(self, text: str) -> str:
        """``text`` without the token, and without anything :func:`memini.logs.redact` masks."""
        return logs.redact(text.replace(self.token.get_secret_value(), logs.REDACTED))


def load_client_settings(
    environ: Mapping[str, str], *, url: str | None = None, token_file: Path | None = None
) -> ClientSettings:
    """The client settings from ``environ``, with the ``--url`` and ``--token-file`` options.

    Raises:
        ClientConfigError: an invalid URL, no token, an unreadable token file, or a value that is
            not a token.
    """
    if url is not None:
        base = _check_url(url, "--url")
    else:
        base = _check_url(environ.get(URL_VAR) or DEFAULT_URL, URL_VAR)

    value = environ.get(TOKEN_VAR) or None
    file = environ.get(TOKEN_FILE_VAR) or None
    if token_file is not None:
        origin = f"--token-file {token_file}"
        value = _read_token_file(token_file, "--token-file")
    elif file is not None:
        if value is not None:
            raise ClientConfigError(f"{TOKEN_VAR}, {TOKEN_FILE_VAR}: both are set; set only one")
        origin = TOKEN_FILE_VAR
        value = _read_token_file(Path(file), TOKEN_FILE_VAR)
    else:
        origin = TOKEN_VAR
    if value is None:
        raise ClientConfigError(
            f"no token: set {TOKEN_VAR} or {TOKEN_FILE_VAR}. An admin creates one with"
            " `memini token create`; the first admin token comes from `memini bootstrap`, inside"
            " the service's container"
        )
    token = value.strip()
    if not is_well_formed_token(token):
        raise ClientConfigError(
            f"{origin}: not a Memini token (memini_ and 43 letters, digits, '-' or '_')"
        )
    logs.register_secret(token)
    return ClientSettings(url=base, token=SecretStr(token), origin=origin)


def _check_url(value: str, origin: str) -> str:
    expected = "expected the service's URL, such as http://127.0.0.1:6364"
    value = value.strip()
    if not value.isprintable() or " " in value:
        raise ClientConfigError(f"{origin}: invalid; {expected}")
    try:
        parts = urlsplit(value)
        port = parts.port  # raises ValueError for a port that is not a number in range
        host = parts.hostname
    except ValueError:
        raise ClientConfigError(f"{origin}: invalid; {expected}") from None
    del port
    if parts.scheme not in ("http", "https") or not host:
        raise ClientConfigError(f"{origin}: invalid; {expected}")
    if parts.username is not None or parts.password is not None:
        # Not echoed: the part before the host may be a password.
        raise ClientConfigError(
            f"{origin}: holds a user name or password; the token is the credential, in"
            f" {TOKEN_VAR} or {TOKEN_FILE_VAR}"
        )
    if parts.query or parts.fragment:
        raise ClientConfigError(f"{origin}: has a query or a fragment; {expected}")
    return value.rstrip("/")


def _read_token_file(path: Path, origin: str) -> str:
    try:
        return path.read_bytes().decode("utf-8").rstrip("\r\n")
    except OSError as error:
        raise ClientConfigError(
            f"{origin}: cannot read {path}: {error.strerror or type(error).__name__}"
        ) from None
    except UnicodeDecodeError:
        raise ClientConfigError(f"{origin}: {path} is not UTF-8 text") from None


def quiet_http_logs() -> None:
    """Keep the HTTP library's loggers to warnings and errors: no line per request."""
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def segment(value: str) -> str:
    """``value`` as one segment of a URL path, so it cannot reach another route."""
    return quote(value, safe="")


class Api:
    """Calls to the service's HTTP API with the token. A context manager; closes its connections.

    Paths are relative to the service URL and start with ``v1/``. A call returns the JSON object of
    a successful answer (``None`` for 204) or raises :class:`~memini.cli.common.CommandError`
    with the exit code for the failure; see the module.
    """

    def __init__(
        self,
        settings: ClientSettings,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self.settings = settings
        self._timeout = timeout
        self._http = httpx2.Client(
            base_url=settings.url + "/",
            headers={
                "Authorization": f"Bearer {settings.token.get_secret_value()}",
                "Accept": "application/json",
                "User-Agent": f"memini-cli/{__version__}",
            },
            timeout=httpx2.Timeout(timeout, connect=min(CONNECT_TIMEOUT, timeout)),
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: Params | None = None) -> Json:
        return self._expect_body(self.request("GET", path, params=params), path)

    def post(self, path: str, body: Json | None = None) -> Json:
        return self._expect_body(self.request("POST", path, body=body), path)

    def patch(self, path: str, body: Json) -> Json:
        return self._expect_body(self.request("PATCH", path, body=body), path)

    def delete(self, path: str) -> Json | None:
        return self.request("DELETE", path)

    def request(
        self, method: str, path: str, *, params: Params | None = None, body: Json | None = None
    ) -> Json | None:
        """One call; the JSON object of a successful answer, or ``None`` for one without a body.

        Raises:
            CommandError: the service cannot be reached, did not answer in time, refused the call
                or answered with something that is not the API's.
        """
        query = None if params is None else {k: v for k, v in params.items() if v is not None}
        try:
            response = self._http.request(method, path, params=query, json=body)
        except httpx2.TimeoutException:
            raise CommandError(
                f"the service at {self.settings.url} did not answer within {self._timeout:g}"
                " seconds",
                EXIT_UNAVAILABLE,
            ) from None
        except httpx2.TransportError as error:
            raise CommandError(
                f"cannot reach the service at {self.settings.url}: {self._reason(error)}",
                EXIT_UNAVAILABLE,
            ) from None
        if response.is_success:
            if response.status_code == 204 or not response.content:
                return None
            return self._json_object(response)
        raise self._failure(response)

    def _reason(self, error: httpx2.TransportError) -> str:
        text = str(error).strip().splitlines()
        reason = text[0] if text and text[0] else type(error).__name__
        return printable(self.settings.redact(reason))

    def _json_object(self, response: httpx2.Response) -> Json:
        try:
            body: object = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            raise CommandError(
                f"the service at {self.settings.url} answered HTTP {response.status_code} with"
                " something other than a JSON object; is MEMINI_URL the Memini service?"
            )
        return cast(Json, body)

    def _expect_body(self, body: Json | None, path: str) -> Json:
        if body is None:
            raise CommandError(
                f"the service at {self.settings.url} answered {path} without a body; is"
                " MEMINI_URL the Memini service?"
            )
        return body

    def _failure(self, response: httpx2.Response) -> CommandError:
        """The :class:`CommandError` for an answer that is not a success."""
        status = response.status_code
        error = _error_body(response)
        if error is None:
            if status in (502, 503, 504):
                return CommandError(
                    f"the service at {self.settings.url} is unavailable (HTTP {status})",
                    EXIT_UNAVAILABLE,
                )
            if 300 <= status < 400:
                return CommandError(
                    f"the service at {self.settings.url} answered with a redirect (HTTP {status}),"
                    " which is not followed; check MEMINI_URL",
                    EXIT_USAGE,
                )
            return CommandError(
                f"the service at {self.settings.url} answered HTTP {status} without the API's"
                " error body; is MEMINI_URL the Memini service?"
            )
        code = _text(error.get("code")) or "error"
        text = self.settings.redact(_text(error.get("message")) or f"HTTP {status}")
        request_id = _text(error.get("request_id"))
        where = f"{code}, HTTP {status}" + (f", request {request_id}" if request_id else "")
        line = f"{text} ({where})"
        fields = _fields(error.get("details"))
        if fields:
            line += f"; not valid: {fields}"
        if status == 401:
            return CommandError(f"{line}; check {self.settings.origin}", EXIT_DENIED)
        if status == 403:
            return CommandError(line, EXIT_DENIED)
        if status == 503 and code == "unavailable":
            return CommandError(line, EXIT_UNAVAILABLE)
        return CommandError(line, EXIT_FAILURE)


def _error_body(response: httpx2.Response) -> Mapping[str, object] | None:
    """The ``error`` object of an answer in the API's error shape, or ``None``."""
    try:
        body: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    error = cast(dict[str, object], body).get("error")
    if not isinstance(error, dict):
        return None
    return cast(dict[str, object], error)


def _text(value: object) -> str:
    return printable(value) if isinstance(value, str) else ""


def _fields(details: object) -> str:
    """``body.name (string_pattern_mismatch), query.limit (less_than_equal)`` from ``details``."""
    if not isinstance(details, list):
        return ""
    found: list[str] = []
    for detail in cast(list[object], details):
        if not isinstance(detail, dict):
            continue
        entry = cast(dict[str, object], detail)
        location = entry.get("location")
        parts = cast(list[object], location) if isinstance(location, list) else []
        where = ".".join(printable(str(part)) for part in parts) or "?"
        found.append(f"{where} ({_text(entry.get('type')) or 'invalid'})")
    return ", ".join(found)
