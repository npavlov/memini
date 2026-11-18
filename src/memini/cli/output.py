"""Readable output of what the API answers: times, durations, fields, JSON.

Values come from JSON, so every helper takes ``object`` and copes with ``None`` and with a type it
did not expect. Text that comes from the service goes through
:func:`~memini.cli.common.printable` before it is printed.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from memini.cli.arguments import format_duration
from memini.cli.common import format_time, printable

__all__ = [
    "NONE",
    "as_list",
    "as_mapping",
    "elapsed",
    "milliseconds",
    "print_fields",
    "print_json",
    "seconds",
    "text",
    "timestamp",
    "when",
]

NONE = "-"
"""What a missing value prints as."""


def as_mapping(value: object) -> Mapping[str, Any]:
    """``value`` if it is a JSON object, else an empty one."""
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def as_list(value: object) -> list[Any]:
    """``value`` if it is a JSON array, else an empty one."""
    return cast(list[Any], value) if isinstance(value, list) else []


def text(value: object, none: str = NONE) -> str:
    """A JSON value as one printable line; ``none`` for null."""
    if value is None:
        return none
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(text(item) for item in cast(list[object], value)) or none
    return printable(str(value))


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def when(value: object, none: str = NONE) -> str:
    """An ISO time from the API in UTC to the minute, e.g. ``2026-11-18T16:02Z``."""
    moment = _instant(value)
    return none if moment is None else format_time(moment)


def timestamp(value: object, none: str = NONE) -> str:
    """An ISO time from the API in UTC to the second, e.g. ``2026-11-18T16:02:03Z``."""
    moment = _instant(value)
    return none if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def milliseconds(value: object, none: str = NONE) -> str:
    """A duration in milliseconds, readably: ``120 ms``, ``1.5 s``, ``2m5s``."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return none
    if value < 1000:
        return f"{value:.0f} ms"
    if value < 60_000:
        return f"{value / 1000:.1f} s"
    return format_duration(value / 1000)


def seconds(value: object, none: str = NONE) -> str:
    """A duration in seconds as ``1h30m``."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return none
    return format_duration(value)


def elapsed(start: object, end: object, none: str = NONE) -> str:
    """The time from ``start`` to ``end``, two ISO times, readably; ``none`` if one is missing."""
    began, ended = _instant(start), _instant(end)
    if began is None or ended is None:
        return none
    return milliseconds((ended - began).total_seconds() * 1000)


def print_fields(fields: Iterable[tuple[str, str]]) -> None:
    """Print ``name  value`` lines with the values aligned."""
    rows = list(fields)
    width = max((len(name) for name, _ in rows), default=0)
    for name, value in rows:
        print(f"{name.ljust(width)}  {value}".rstrip())


def print_json(value: Mapping[str, Any] | Sequence[Any]) -> None:
    """Print a JSON value as the API sent it, indented. Characters a terminal could take for
    control sequences are escaped, as JSON escapes them, so the output stays valid JSON."""
    dumped = json.dumps(value, indent=2, ensure_ascii=False)
    if not dumped.isprintable():
        dumped = "".join(c if c.isprintable() or c == "\n" else json.dumps(c)[1:-1] for c in dumped)
    print(dumped)
