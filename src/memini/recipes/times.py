"""Times in a recipe: durations, the formats a window bound is sent in, and the formats a timestamp
is read in.

**Durations** (``overlap = "2d"``) are numbers with units ``s``, ``m``, ``h``, ``d`` and ``w``, as
in ``90d``, ``12h`` or ``1w2d``: the syntax of ``memini token create --expires-in``.

**Window bounds** (``{from:unix}`` in an argument) are rendered in UTC in one of these formats:

- ``rfc3339`` (the default): ``2026-11-18T14:02:11Z``, to the second;
- ``unix``: seconds since the epoch, an integer;
- ``unix_ms``: milliseconds since the epoch, an integer;
- ``date``: ``2026-11-18``;
- a ``strftime`` pattern of ``%Y``, ``%m``, ``%d``, ``%H``, ``%M``, ``%S`` and ``%%`` with any
  other characters, such as ``%Y/%m/%d``. Directives that depend on the locale are not allowed.

A format is as precise as its smallest unit, and a bound is rendered so that the range a call asks
for covers the window: ``from`` is rounded down and ``to`` up to that unit. A window
``[2026-11-17T09:30Z, 2026-11-18T14:02Z)`` in ``date`` asks for ``2026-11-17`` to ``2026-11-19``.
The call then returns more than the window holds, which is harmless: records are upserted.

**Timestamps** in a response (``timestamp`` of a listed item) are read in one of:

- ``rfc3339`` (the default): ISO 8601 with a date and a time, ``Z`` or an offset;
- ``unix``: seconds since the epoch, as a number or a string of digits with an optional fraction
  (``"1731900000.000100"``); read exactly, without a float;
- ``unix_ms``: milliseconds since the epoch, an integer or a string of digits;
- a ``strptime`` pattern of ``%Y``, ``%m``, ``%d``, ``%H``, ``%M``, ``%S``, ``%f``, ``%z`` and
  ``%%``.

A time without a zone is in the recipe's ``timezone`` (an IANA name), or in UTC without one.
"""

import re
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "BOUND_FORMATS",
    "DURATION_PATTERN",
    "MAX_DURATION",
    "TIMESTAMP_FORMATS",
    "Bound",
    "check_bound_format",
    "check_timestamp_format",
    "check_timezone",
    "format_bound",
    "format_duration",
    "parse_duration",
    "parse_timestamp",
]

Bound = Literal["from", "to"]

BOUND_FORMATS: Final = ("rfc3339", "unix", "unix_ms", "date")
"""Named formats of a window bound; a ``strftime`` pattern is the other choice."""
TIMESTAMP_FORMATS: Final = ("rfc3339", "unix", "unix_ms")
"""Named formats of a timestamp in a response; a ``strptime`` pattern is the other choice."""

DURATION_PATTERN: Final = r"^([0-9]+[smhdw])+$"
"""A duration in the JSON Schema."""
MAX_DURATION: Final = timedelta(days=36525)

_DURATION: Final = re.compile(DURATION_PATTERN)
_DURATION_PART: Final = re.compile(r"([0-9]+)([smhdw])")
_UNIT_SECONDS: Final = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}

_BOUND_DIRECTIVES: Final = frozenset("YmdHMS%")
_TIMESTAMP_DIRECTIVES: Final = frozenset("YmdHMSfz%")

type _Unit = Literal["ms", "s", "min", "h", "d", "month", "year"]
_DIRECTIVE_UNITS: Final[dict[str, _Unit]] = {
    "S": "s",
    "M": "min",
    "H": "h",
    "d": "d",
    "m": "month",
    "Y": "year",
}
_UNIT_ORDER: Final[tuple[_Unit, ...]] = ("ms", "s", "min", "h", "d", "month", "year")
_NAMED_UNITS: Final[dict[str, _Unit]] = {"rfc3339": "s", "unix": "s", "unix_ms": "ms", "date": "d"}

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_UNIX_TEXT: Final = re.compile(r"[0-9]{1,12}(\.[0-9]{1,9})?")
_UNIX_MS_TEXT: Final = re.compile(r"[0-9]{1,15}")


# --- Durations -----------------------------------------------------------------------------------


def parse_duration(value: object) -> timedelta:
    """A duration such as ``30d``, ``12h`` or ``1w2d`` as a :class:`timedelta`.

    Raises ``ValueError`` for anything else, for zero, and for more than :data:`MAX_DURATION`.
    A :class:`timedelta` is taken as it is, so a model can be built in code.
    """
    if isinstance(value, timedelta):
        total = value
    else:
        if not isinstance(value, str) or _DURATION.fullmatch(value) is None:
            raise ValueError(
                "a duration is numbers with units, such as 90d, 12h or 1w2d (units: s, m, h, d, w)"
            )
        seconds = sum(int(n) * _UNIT_SECONDS[unit] for n, unit in _DURATION_PART.findall(value))
        if seconds > MAX_DURATION.total_seconds():
            raise ValueError("a duration is at most 36525d")
        total = timedelta(seconds=seconds)
    if total <= timedelta():
        raise ValueError("a duration must be longer than zero")
    if total > MAX_DURATION:
        raise ValueError("a duration is at most 36525d")
    return total


def format_duration(value: timedelta) -> str:
    """The canonical text of a duration: the largest units first, ``7d`` as ``1w``.

    Parts of a second are dropped; a recipe's durations have none.
    """
    seconds = int(value.total_seconds())
    parts: list[str] = []
    for unit, size in _UNIT_SECONDS.items():
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


# --- Window bounds -------------------------------------------------------------------------------


def check_bound_format(value: str) -> str:
    """``value`` if it is a format a window bound can be rendered in; raises ``ValueError``."""
    if value in BOUND_FORMATS:
        return value
    if "%" not in value:
        raise ValueError(
            f"a bound format is one of {', '.join(BOUND_FORMATS)} or a strftime pattern"
        )
    _check_directives(value, _BOUND_DIRECTIVES)
    if _pattern_unit(value) is None:
        raise ValueError("a strftime pattern needs at least one of %Y, %m, %d, %H, %M, %S")
    return value


def format_bound(moment: datetime, fmt: str, bound: Bound) -> str | int:
    """``moment``, a bound of a window, in ``fmt``: rounded down for ``from``, up for ``to``.

    ``unix`` and ``unix_ms`` give an integer, the other formats a string. ``moment`` must have a
    zone; it is rendered in UTC.
    """
    if moment.tzinfo is None:
        raise ValueError("a window bound needs a time zone")
    unit = _NAMED_UNITS.get(fmt) or _pattern_unit(fmt)
    if unit is None:  # check_bound_format refuses such a format
        raise ValueError(f"not a bound format: {fmt}")
    utc = moment.astimezone(UTC)
    rounded = _floor(utc, unit) if bound == "from" else _ceil(utc, unit)
    match fmt:
        case "rfc3339":
            return rounded.strftime("%Y-%m-%dT%H:%M:%SZ")
        case "unix":
            return (rounded - _EPOCH) // timedelta(seconds=1)
        case "unix_ms":
            return (rounded - _EPOCH) // timedelta(milliseconds=1)
        case "date":
            return rounded.strftime("%Y-%m-%d")
        case _:
            return rounded.strftime(fmt)


def _pattern_unit(pattern: str) -> _Unit | None:
    """The smallest unit a ``strftime`` pattern shows."""
    shown = _directives(pattern)
    units: list[_Unit] = [_DIRECTIVE_UNITS[d] for d in shown if d in _DIRECTIVE_UNITS]
    for unit in _UNIT_ORDER:
        if unit in units:
            return unit
    return None


def _floor(moment: datetime, unit: _Unit) -> datetime:
    match unit:
        case "ms":
            return moment.replace(microsecond=moment.microsecond // 1000 * 1000)
        case "s":
            return moment.replace(microsecond=0)
        case "min":
            return moment.replace(second=0, microsecond=0)
        case "h":
            return moment.replace(minute=0, second=0, microsecond=0)
        case "d":
            return moment.replace(hour=0, minute=0, second=0, microsecond=0)
        case "month":
            return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        case "year":
            return moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _ceil(moment: datetime, unit: _Unit) -> datetime:
    floor = _floor(moment, unit)
    if floor == moment:
        return floor
    match unit:
        case "ms":
            return floor + timedelta(milliseconds=1)
        case "s":
            return floor + timedelta(seconds=1)
        case "min":
            return floor + timedelta(minutes=1)
        case "h":
            return floor + timedelta(hours=1)
        case "d":
            return floor + timedelta(days=1)
        case "month":
            if floor.month == 12:
                return floor.replace(year=floor.year + 1, month=1)
            return floor.replace(month=floor.month + 1)
        case "year":
            return floor.replace(year=floor.year + 1)


# --- Timestamps ----------------------------------------------------------------------------------


def check_timestamp_format(value: str) -> str:
    """``value`` if timestamps can be read in it; raises ``ValueError``."""
    if value in TIMESTAMP_FORMATS:
        return value
    if "%" not in value:
        raise ValueError(
            f"a timestamp format is one of {', '.join(TIMESTAMP_FORMATS)} or a strptime pattern"
        )
    _check_directives(value, _TIMESTAMP_DIRECTIVES)
    directives = set(_directives(value))
    if not {"Y", "m", "d"} <= directives:
        raise ValueError("a strptime pattern needs %Y, %m and %d")
    return value


def check_timezone(value: str) -> str:
    """``value`` if it is an IANA time zone name this system knows; raises ``ValueError``."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("not a known IANA time zone name, such as UTC or Europe/Lisbon") from None
    return value


def parse_timestamp(value: object, fmt: str, timezone: str | None = None) -> datetime:
    """A timestamp read from a response, as an aware :class:`datetime`.

    ``fmt`` is a format :func:`check_timestamp_format` accepts. A time without a zone is taken in
    ``timezone``, or in UTC. Raises ``ValueError`` when ``value`` does not fit the format; the
    message never holds the value.
    """
    zone: tzinfo = ZoneInfo(timezone) if timezone else UTC
    match fmt:
        case "unix":
            return _EPOCH + timedelta(microseconds=_unix_micros(value, scale=1_000_000))
        case "unix_ms":
            return _EPOCH + timedelta(microseconds=_unix_micros(value, scale=1000))
        case "rfc3339":
            # A date alone is not a time: 10 characters, YYYY-MM-DD.
            if not isinstance(value, str) or len(value) <= 10:
                raise ValueError("not an RFC 3339 time with a date and a time")
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError("not an RFC 3339 time with a date and a time") from None
        case _:
            if not isinstance(value, str):
                raise ValueError("a timestamp in a strptime pattern is a string")
            try:
                parsed = datetime.strptime(value, fmt)  # the zone, if it has none, is set below
            except ValueError:
                raise ValueError("the timestamp does not fit the recipe's pattern") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _unix_micros(value: object, *, scale: int) -> int:
    """Microseconds since the epoch from seconds (``scale`` 10**6) or milliseconds (10**3)."""
    if isinstance(value, bool):
        raise ValueError("a unix timestamp is a number or a string of digits")
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    elif isinstance(value, str) and (
        (_UNIX_TEXT if scale == 1_000_000 else _UNIX_MS_TEXT).fullmatch(value)
    ):
        try:
            number = Decimal(value)
        except InvalidOperation:  # the pattern lets none through
            raise ValueError("a unix timestamp is a number or a string of digits") from None
    else:
        raise ValueError("a unix timestamp is a number or a string of digits")
    if not number.is_finite() or number < 0:
        raise ValueError("a unix timestamp is a finite number, not negative")
    micros = int(number * scale)
    if micros > (datetime(9999, 1, 1, tzinfo=UTC) - _EPOCH) // timedelta(microseconds=1):
        raise ValueError("a unix timestamp after the year 9999")
    return micros


# --- Patterns ------------------------------------------------------------------------------------


def _directives(pattern: str) -> list[str]:
    """The directive letters of a ``strftime`` pattern, ``%%`` included as ``%``."""
    found: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern[index] == "%":
            if index + 1 >= len(pattern):
                raise ValueError("a strftime pattern ends with a lone %")
            found.append(pattern[index + 1])
            index += 2
        else:
            index += 1
    return found


def _check_directives(pattern: str, allowed: frozenset[str]) -> None:
    for directive in _directives(pattern):
        if directive not in allowed:
            names = " ".join(f"%{d}" for d in "YmdHMSfz%" if d in allowed)
            raise ValueError(f"%{directive} is not allowed in this pattern; use {names}")
