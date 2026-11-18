"""Structured logs: one JSON object per line, with the request id and the run id, redacted.

Call :func:`configure_logging` once at start, before the HTTP server or the workers start. It puts
one handler on the root logger, so every logger of the process writes through it: Memini's own
loggers, and the loggers of uvicorn, FastAPI and psycopg, whose own handlers it removes. Calling it
again replaces that handler; it never adds a second one. Run uvicorn with ``log_config=None``,
otherwise uvicorn installs its own handlers when it starts.

A line in JSON mode (the default)::

    {"timestamp": "2026-01-02T03:04:05.678Z", "level": "INFO", "logger": "memini.runs",
     "message": "run finished: succeeded", "request_id": "…", "run_id": "…",
     "extra": {"kind": "sync"}, "exception": {"type": "…", "message": "…", "traceback": "…"}}

(one line in the output; wrapped here). ``timestamp`` is UTC, ISO 8601, in milliseconds.
``request_id`` is present while an HTTP request is handled (:data:`request_id_var`), ``run_id``
while a run is recorded (:data:`run_id_var`), ``extra`` when the call
passed ``extra={...}``, ``exception`` when it passed ``exc_info``, ``stack`` when it passed
``stack_info``. A message with line breaks stays on one line: JSON escapes them.

Redaction is the last line of defence: code should not log secrets in the first place. Every string
that goes into a line, keys included, is redacted after the message is formatted with its arguments
and after the exception is formatted with its traceback. :func:`redact` replaces with
:data:`REDACTED`:

- Memini bearer tokens: ``memini_`` followed by 20 or more of ``A-Z a-z 0-9 _ -``.
- The credentials of an ``Authorization`` header, whatever the scheme, and a ``Bearer`` value of 16
  or more characters anywhere.
- The password in a URL: ``scheme://user:password@host`` keeps scheme, user and host.
- The value after ``password``, ``passwd``, ``secret``, ``api_key``, ``access_token`` or
  ``refresh_token`` and ``=`` or ``:``, as in a libpq connection string ``password=…``.
- Every value registered with :func:`register_secret` or :func:`register_url_secrets`.

An ``extra`` field whose key names a secret (``password``, ``token``, ``authorization``,
``db_password`` …) is replaced whole. A ``SecretStr`` renders as ``**********``; this module never
unwraps one.
"""

import contextlib
import json as _json
import logging
import math
import re
import sys
import threading
import traceback
import uuid
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, TextIO, cast, override
from urllib.parse import unquote, urlsplit

__all__ = [
    "MIN_SECRET_LENGTH",
    "REDACTED",
    "ROUTED_LOGGERS",
    "JsonFormatter",
    "TextFormatter",
    "clear_secrets",
    "configure_logging",
    "current_run_id",
    "redact",
    "register_secret",
    "register_url_secrets",
    "request_id_var",
    "run_id_var",
]

REDACTED: Final = "[REDACTED]"
"""What a redacted value is replaced with."""

MIN_SECRET_LENGTH: Final = 8
"""Registered secrets shorter than this are ignored: they would mask ordinary words."""

ROUTED_LOGGERS: Final = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "psycopg",
    "psycopg.pool",
    "asyncio",
)
"""Loggers of libraries whose own handlers :func:`configure_logging` removes, so that they write
through the root handler like every other logger."""

run_id_var: ContextVar[uuid.UUID | None] = ContextVar("memini_run_id", default=None)
"""The id of the run being recorded (:func:`memini.runs.record_run`), or ``None`` outside a run."""

# Defined here, where it is read, and set by memini.api.request_id: this module imports nothing of
# memini, so any module may import it without pulling in the HTTP application.
request_id_var: ContextVar[str | None] = ContextVar("memini_request_id", default=None)
"""The id of the HTTP request being handled (:class:`memini.api.request_id.RequestIdMiddleware`),
or ``None`` outside a request."""


def current_run_id() -> uuid.UUID | None:
    """The id of the run being recorded, or ``None`` outside a run."""
    return run_id_var.get()


# --- Redaction -----------------------------------------------------------------------------------

_TOKEN: Final = re.compile(r"memini_[A-Za-z0-9_-]{20,}")
# The header name, optional quotes and separator, an optional scheme, then the credentials.
_AUTHORIZATION: Final = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:(bearer|basic|digest|token|negotiate)\s+)?"
    r"[^\s\"',;\\]+"
)
_BEARER: Final = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{16,}=*")
# Greedy password up to the last "@" before a space or a quote: an unencoded "@" or "/" in a
# password still leaves no part of it behind.
_URL_PASSWORD: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s:/?#@\"'<>]*):[^\s\"'<>]*@")
_KEY_VALUE: Final = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)"
    r"([\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;\\]+"
)
# Keys of extra fields whose value is a secret, after lower-casing and "-" -> "_".
_SECRET_KEY: Final = re.compile(
    r"(?:.*_)?(?:password|passwd|secret|token|api_?key|access_?key|private_?key|secret_?key"
    r"|authorization|cookie|set_cookie|credentials?)"
)


def _authorization(match: re.Match[str]) -> str:
    scheme = match.group(2)
    return f"{match.group(1)}{scheme} {REDACTED}" if scheme else f"{match.group(1)}{REDACTED}"


class _SecretRegistry:
    """Exact values to mask. Writes take a lock; reads use the compiled pattern as it stands."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()
        self.pattern: re.Pattern[str] | None = None

    def add(self, values: Iterable[str]) -> bool:
        fresh = {value for value in values if len(value) >= MIN_SECRET_LENGTH}
        if not fresh:
            return False
        with self._lock:
            self._values |= fresh
            # Longest first, so a secret that contains another is masked whole.
            ordered = sorted(self._values, key=len, reverse=True)
            self.pattern = re.compile("|".join(re.escape(value) for value in ordered))
        return True

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self.pattern = None


_secrets: Final = _SecretRegistry()


def register_secret(value: str) -> bool:
    """Mask every occurrence of ``value`` in log lines from now on.

    For values known at start that the pattern rules might miss: a database password, an API key
    of an LLM provider. Returns ``False`` and registers nothing if ``value`` is shorter than
    :data:`MIN_SECRET_LENGTH`: so short a value would mask ordinary words. The rules still mask it
    where it appears in a recognizable form, such as the password of a URL.
    """
    return _secrets.add((value,))


def register_url_secrets(url: str) -> bool:
    """Register the password of ``url``, as written and percent-decoded.

    For the database URL: ``register_url_secrets(settings.database_url.get_secret_value())`` at
    start. Returns whether anything was registered; a URL without a password registers nothing.
    """
    try:
        password = urlsplit(url).password
    except ValueError:
        return False
    if not password:
        return False
    return _secrets.add((password, unquote(password)))


def clear_secrets() -> None:
    """Forget every registered secret. For tests."""
    _secrets.clear()


def redact(text: str) -> str:
    """``text`` with tokens, credentials, URL passwords and registered secrets masked."""
    pattern = _secrets.pattern
    if pattern is not None:
        text = pattern.sub(REDACTED, text)
    text = _TOKEN.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(rf"\1\2:{REDACTED}@", text)
    text = _AUTHORIZATION.sub(_authorization, text)
    text = _BEARER.sub(rf"\1 {REDACTED}", text)
    return _KEY_VALUE.sub(rf"\1\2{REDACTED}", text)


def _is_secret_key(key: str) -> bool:
    return _SECRET_KEY.fullmatch(key.lower().replace("-", "_")) is not None


type _Json = str | int | float | bool | list[_Json] | dict[str, _Json] | None

_MAX_DEPTH: Final = 8


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__} that cannot be shown>"


def _clean(value: object, depth: int = 0) -> _Json:
    """``value`` as JSON-ready data, every string in it redacted. Never unwraps a secret."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return redact(value)
    if depth < _MAX_DEPTH:
        if isinstance(value, Mapping):
            mapping = cast(Mapping[object, object], value)
            return {
                redact(_safe_str(key)): REDACTED
                if _is_secret_key(_safe_str(key))
                else _clean(item, depth + 1)
                for key, item in mapping.items()
            }
        if isinstance(value, list | tuple | set | frozenset):
            members = cast(Iterable[object], value)
            return [_clean(item, depth + 1) for item in members]
    # Anything else as its str(): a SecretStr gives "**********", a UUID or a datetime its text.
    return redact(_safe_str(value))


# --- Formatting ----------------------------------------------------------------------------------

# Attributes every LogRecord has; the others came from extra={...}.
_RECORD_ATTRIBUTES: Final = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}
# Set by uvicorn on its records: the message again, with terminal colours.
_IGNORED_EXTRAS: Final = frozenset({"color_message"})

type _ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def _timestamp(record: logging.LogRecord) -> str:
    moment = datetime.fromtimestamp(record.created, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:
        return f"{_safe_str(record.msg)} (the log arguments could not be formatted)"


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RECORD_ATTRIBUTES and key not in _IGNORED_EXTRAS and not key.startswith("_")
    }


def _exc_info(record: logging.LogRecord) -> _ExcInfo | None:
    info = record.exc_info
    if info is None or info[0] is None:
        return None
    return info


def _type_name(exc_type: type[BaseException]) -> str:
    module = exc_type.__module__
    return exc_type.__qualname__ if module == "builtins" else f"{module}.{exc_type.__qualname__}"


def _traceback(info: _ExcInfo) -> str:
    try:
        return "".join(traceback.format_exception(*info)).rstrip("\n")
    except Exception:
        return f"{_type_name(info[0])} (the traceback could not be formatted)"


class JsonFormatter(logging.Formatter):
    """One JSON object per record, on one line, every string in it redacted."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": _message(record),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            entry["request_id"] = request_id
        run_id = run_id_var.get()
        if run_id is not None:
            entry["run_id"] = str(run_id)
        extras = _extras(record)
        if extras:
            entry["extra"] = extras
        info = _exc_info(record)
        if info is not None:
            entry["exception"] = {
                "type": _type_name(info[0]),
                "message": _safe_str(info[1]),
                "traceback": _traceback(info),
            }
        if record.stack_info:
            entry["stack"] = record.stack_info
        # Redacted as a whole, keys included, after everything above was rendered to text.
        return _json.dumps(_clean(entry), ensure_ascii=False, separators=(", ", ": "))


class TextFormatter(logging.Formatter):
    """A line for people, for local development: time, level, logger, message, context. Redacted."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        parts = [_timestamp(record), f"{record.levelname:<8}", f"{record.name}:", _message(record)]
        context: dict[str, object] = {}
        request_id = request_id_var.get()
        if request_id is not None:
            context["request_id"] = request_id
        run_id = run_id_var.get()
        if run_id is not None:
            context["run_id"] = run_id
        context |= _extras(record)
        for key, value in context.items():
            shown = REDACTED if _is_secret_key(key) else _json.dumps(_clean(value))
            parts.append(f"{key}={shown}")
        line = " ".join(parts)
        info = _exc_info(record)
        if info is not None:
            line += "\n" + _traceback(info)
        if record.stack_info:
            line += "\n" + record.stack_info
        return redact(line)


class _Handler(logging.StreamHandler[TextIO]):
    """The one handler :func:`configure_logging` installs; recognized by its class."""

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        # The default prints the record's message and arguments to stderr, unredacted. This names
        # the failure only.
        if not logging.raiseExceptions:
            return
        error = sys.exception()
        kind = type(error).__name__ if error is not None else "unknown error"
        with contextlib.suppress(Exception):  # nowhere left to report it
            sys.stderr.write(f"memini: a log record from {record.name} was lost ({kind})\n")


def _level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    number = logging.getLevelNamesMapping().get(level.upper())
    if number is None:
        raise ValueError(f"unknown log level {level!r}")
    return number


def configure_logging(
    level: str | int = "INFO", *, json: bool = True, stream: TextIO | None = None
) -> None:
    """Send every log record of the process through one redacting handler.

    Args:
        level: the lowest level written, a name such as ``"INFO"`` (``settings.log_level``) or a
            number. It is set on the root logger.
        json: one JSON object per line (the default) or, for local development, a line for people.
        stream: where lines go; standard error by default.

    Idempotent: a second call replaces the handler of the first, with the new arguments. Handlers
    that others put on the root logger stay; the handlers of :data:`ROUTED_LOGGERS` are removed and
    those loggers propagate to the root.

    Raises:
        ValueError: ``level`` is not a level name.
    """
    number = _level(level)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _Handler):
            root.removeHandler(handler)
    handler = _Handler(sys.stderr if stream is None else stream)
    handler.setFormatter(JsonFormatter() if json else TextFormatter())
    root.addHandler(handler)
    root.setLevel(number)

    for name in ROUTED_LOGGERS:
        logger = logging.getLogger(name)
        for routed in list(logger.handlers):
            logger.removeHandler(routed)
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
