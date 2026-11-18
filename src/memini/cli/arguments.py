"""Argument types of the commands, and the registry that lists the commands in two groups.

``memini --help`` lists client commands (HTTP clients of the running service) apart from operator
commands (on the database, inside the service's container). :class:`Commands` keeps that listing:
:meth:`Commands.add_client` adds a client command, :meth:`Commands.add_parser` an operator command,
with the arguments of :meth:`argparse._SubParsersAction.add_parser`, and
:meth:`Commands.add_operator_subcommand` an operator command inside a group of client commands
(``workspace llm``). A client command sets ``client=`` with
:meth:`argparse.ArgumentParser.set_defaults` (a :data:`ClientCommand`); an operator command sets
``command=`` (an :data:`OperatorCommand`). What a command sets decides how it runs, whatever group
it is in.
"""

import argparse
import re
import textwrap
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any, Final, NoReturn

from memini.cli.client import TOKEN_FILE_VAR, TOKEN_VAR, Api
from memini.config import Settings
from memini.logs import redact
from memini.sources import MAX_SCHEDULE, MIN_SCHEDULE
from memini.token_admin import MAX_TOKEN_LIFETIME
from memini.workspaces import check_handle

__all__ = [
    "HELP_WIDTH",
    "ClientCommand",
    "Commands",
    "OfflineCommand",
    "OperatorCommand",
    "Parser",
    "format_duration",
    "parse_duration",
    "parse_lifetime",
    "parse_schedule",
    "parse_source_name",
    "parse_token_name",
    "parse_workspace_name",
]

type OperatorCommand = Callable[[argparse.Namespace, Settings], Coroutine[Any, Any, int]]
"""An operator command: runs with the service's settings, on the database."""
type ClientCommand = Callable[[argparse.Namespace, Api], int]
"""A client command: runs with a client of the service's HTTP API."""
type OfflineCommand = Callable[[argparse.Namespace], Coroutine[Any, Any, int]]
"""An offline command: reads files only; needs neither the service nor the database."""

HELP_WIDTH: Final = 79
"""The width the hand-made parts of ``memini --help`` are wrapped to."""

_UNITS: Final = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_UNIT_SECONDS: Final = (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
_DURATION: Final = re.compile(r"(?:\d+[smhdw])+")
_DURATION_PART: Final = re.compile(r"(\d+)([smhdw])")


# --- Argument types ------------------------------------------------------------------------------


def parse_duration(value: str) -> timedelta:
    """A duration such as ``30d``, ``12h`` or ``1w2d``: numbers with units s, m, h, d, w; at most
    36525 days."""
    return _duration(value, "a duration is at most 36525d")


def parse_lifetime(value: str) -> timedelta:
    """The lifetime of a token: a duration of at most :data:`MAX_TOKEN_LIFETIME`."""
    return _duration(
        value,
        "a duration is at most 36525d; leave out --expires-in for a token that does not expire",
    )


def _duration(value: str, too_long: str) -> timedelta:
    text = value.strip().lower()
    if _DURATION.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(
            "a duration is a number and a unit, such as 90d, 12h or 1w2d (units: s, m, h, d, w)"
        )
    try:
        total = sum(
            (
                timedelta(**{_UNITS[unit]: int(number)})
                for number, unit in _DURATION_PART.findall(text)
            ),
            timedelta(),
        )
    except OverflowError:
        raise argparse.ArgumentTypeError(too_long) from None
    if total <= timedelta():
        raise argparse.ArgumentTypeError("a duration must be longer than zero")
    if total > MAX_TOKEN_LIFETIME:
        raise argparse.ArgumentTypeError(too_long)
    return total


def parse_schedule(value: str) -> timedelta:
    """How often a source syncs: a duration from one minute to 366 days."""
    schedule = parse_duration(value)
    if not MIN_SCHEDULE <= schedule <= MAX_SCHEDULE:
        raise argparse.ArgumentTypeError("a schedule is from 1m to 366d")
    return schedule


def format_duration(seconds: float) -> str:
    """Seconds as a duration :func:`parse_duration` reads: 3600 is ``1h``, 5400 is ``1h30m``."""
    left = round(seconds)
    if left <= 0:
        return "0s"
    parts: list[str] = []
    for unit, size in _UNIT_SECONDS:
        count, left = divmod(left, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)


def _handle(kind: str, value: str) -> str:
    try:
        return check_handle(kind, value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parse_workspace_name(value: str) -> str:
    return _handle("workspace", value)


def parse_source_name(value: str) -> str:
    return _handle("source", value)


def parse_token_name(value: str) -> str:
    name = value.strip()
    if not name or not name.isprintable():
        raise argparse.ArgumentTypeError("a token name is printable text and not empty")
    return name


# --- The parser and the command registry ---------------------------------------------------------


class Parser(argparse.ArgumentParser):
    """The parser of ``memini`` and its subcommands: no abbreviated options, and errors that never
    repeat a token.

    Without abbreviations, ``--token`` is no option at all, not ``--token-file``: a token given on
    the command line is refused, with its value masked, rather than taken for a file name.
    Subparsers are of this class too: argparse makes them of the class of their parent.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        text = redact(message)
        if "--token" in message and "--token-file" not in message:
            text += (
                f"; the token goes in {TOKEN_VAR} or {TOKEN_FILE_VAR} (or --token-file), never on"
                " the command line"
            )
        super().error(text)


class Commands:
    """The commands of ``memini``, kept for the grouped listing in ``memini --help``.

    Wraps the ``add_parser`` of the top-level subparsers. Commands are added without argparse's
    own listing; :meth:`listing` writes the two groups instead.
    """

    def __init__(self, add_parser: Callable[..., argparse.ArgumentParser]) -> None:
        self._add = add_parser
        self._client: list[tuple[str, str]] = []
        self._operator: list[tuple[str, str]] = []
        self._offline: list[tuple[str, str]] = []

    def add_client(self, name: str, *, help: str, **kwargs: Any) -> argparse.ArgumentParser:
        """Add a client command: an HTTP client of the running service."""
        self._client.append((name, help))
        kwargs.setdefault("description", help[:1].upper() + help[1:] + ".")
        return self._add(name, **kwargs)

    def add_offline(self, name: str, *, help: str, **kwargs: Any) -> argparse.ArgumentParser:
        """Add an offline command: it reads files only, with no service and no database. Its
        subcommands set ``offline`` (an :data:`OfflineCommand`) as their default."""
        self._offline.append((name, help))
        kwargs.setdefault("description", help[:1].upper() + help[1:] + ".")
        return self._add(name, **kwargs)

    def add_parser(self, name: str, *, help: str, **kwargs: Any) -> argparse.ArgumentParser:
        """Add an operator command: it works on the database, inside the service's container."""
        self._operator.append((name, help))
        kwargs["description"] = _operator_description(name, help, kwargs.get("description"))
        return self._add(name, **kwargs)

    def add_operator_subcommand(
        self,
        add_parser: Callable[..., argparse.ArgumentParser],
        group: str,
        name: str,
        *,
        help: str,
        **kwargs: Any,
    ) -> argparse.ArgumentParser:
        """Add an operator command to a group of client commands, with the group's
        ``add_parser``: the group's help marks it, and ``memini --help`` lists it as
        ``<group> <name>`` among the operator commands."""
        full = f"{group} {name}"
        self._operator.append((full, help))
        kwargs["description"] = _operator_description(full, help, kwargs.get("description"))
        return add_parser(name, help=f"{help} (an operator command)", **kwargs)

    def listing(self) -> str:
        """The commands in two groups, for the epilog of ``memini --help``."""
        width = max(len(name) for name, _ in (*self._client, *self._operator, *self._offline)) + 2

        def rows(commands: list[tuple[str, str]]) -> str:
            return "\n".join(f"  {name.ljust(width)}{text}" for name, text in commands)

        client = textwrap.fill(
            "client commands, HTTP clients of the running service at MEMINI_URL (default"
            " http://127.0.0.1:6364) with the token in MEMINI_TOKEN or MEMINI_TOKEN_FILE:",
            HELP_WIDTH,
        )
        operator = textwrap.fill(
            "operator commands, on the database, inside the service's container"
            " (docker compose exec memini memini <command>):",
            HELP_WIDTH,
        )
        listing = f"{client}\n{rows(self._client)}\n\n{operator}\n{rows(self._operator)}"
        if self._offline:
            offline = "offline commands, on files only, with no service and no database:"
            listing += f"\n\n{offline}\n{rows(self._offline)}"
        return listing


def _operator_description(command: str, help: str, description: str | None) -> str:
    text = description or help[:1].upper() + help[1:] + "."
    return (
        f"{text} An operator command: it works on the database directly and runs where the"
        " service's settings are, inside its container (docker compose exec memini memini"
        f" {command})."
    )
