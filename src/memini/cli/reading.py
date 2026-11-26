"""The options the read commands share, named as the arguments of their MCP tools.

A read command is one MCP tool: ``memini search-code`` is ``search_code``, and its options are the
tool's arguments with dashes (``--case-sensitive``, ``--include-sensitive``). A list filter keeps
its plural name and takes one value per use; repeat it for more (``--persons me --persons
bo@example.test`` is ``persons: ["me", "bo@example.test"]``). The service checks the values.
"""

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from memini.cli.arguments import parse_workspace_name
from memini.cli.client import Api
from memini.cli.common import EXIT_USAGE, CommandError, message
from memini.cli.output import as_list, as_mapping

__all__ = [
    "add_filter",
    "add_include_sensitive",
    "add_json",
    "add_limit",
    "add_time_window",
    "add_workspace",
    "left_out",
    "parse_time",
    "resolve_workspace",
    "time_params",
]


def parse_time(value: str) -> datetime:
    """An ISO 8601 time with a zone offset or ``Z``, e.g. ``2026-03-02T10:00:00Z``."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "a time is ISO 8601 with a zone offset or Z, e.g. 2026-03-02T10:00:00Z"
        )
    return moment


def add_filter(
    parser: argparse.ArgumentParser,
    name: str,
    help: str,
    *,
    metavar: str | None = None,
    choices: Sequence[str] | None = None,
    type: Callable[[str], Any] | None = None,
) -> None:
    """A list filter: ``--<name>`` in the plural, one value per use, repeated for more."""
    kwargs: dict[str, Any] = {"action": "append", "help": f"{help}; repeat for more"}
    if choices is not None:
        kwargs["choices"] = tuple(choices)
    else:
        kwargs["metavar"] = metavar or "NAME"
    if type is not None:
        kwargs["type"] = type
    parser.add_argument(f"--{name.replace('_', '-')}", **kwargs)


def add_workspace(parser: argparse.ArgumentParser, what: str) -> None:
    """``--workspace``: the one workspace the object is in; default: the only one the token
    reads."""
    parser.add_argument(
        "--workspace",
        type=parse_workspace_name,
        metavar="NAME",
        help=f"the workspace of the {what}; default: the only workspace the token may read",
    )


def add_limit(parser: argparse.ArgumentParser, default: int, maximum: int) -> None:
    def limit(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            number = 0
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f"a number from 1 to {maximum}")
        return number

    parser.add_argument(
        "--limit",
        type=limit,
        default=default,
        help=f"the most results, 1 to {maximum} (default {default})",
    )


def add_time_window(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument(
        "--since",
        type=parse_time,
        metavar="TIME",
        help=f"only {what} from this time on (ISO 8601 with a zone or Z; inclusive)",
    )
    parser.add_argument(
        "--until", type=parse_time, metavar="TIME", help=f"only {what} before this time"
    )


def time_params(args: argparse.Namespace) -> dict[str, str | None]:
    since: datetime | None = args.since
    until: datetime | None = args.until
    return {
        "since": None if since is None else since.isoformat(),
        "until": None if until is None else until.isoformat(),
    }


def add_include_sensitive(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-sensitive",
        action="store_true",
        help="also records marked sensitive; needs a token with the sensitive scope",
    )


def add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the API's JSON answer")


def left_out(body: Mapping[str, object], what: str) -> None:
    """Say on stderr how many results of records marked sensitive the service left out."""
    count = body.get("left_out")
    if isinstance(count, int) and not isinstance(count, bool) and count:
        message(
            f"{count} {what} marked sensitive left out; --include-sensitive reads them, with a"
            " token that has the sensitive scope"
        )


def resolve_workspace(api: Api, workspace: str | None) -> str:
    """``workspace``, or the only workspace the token may read when it is not given."""
    if workspace is not None:
        return workspace
    names = [
        name
        for name in (
            as_mapping(each).get("name") for each in as_list(api.get("v1/workspaces").get("items"))
        )
        if isinstance(name, str)
    ]
    if len(names) == 1:
        return names[0]
    if not names:
        raise CommandError("the token may read no workspace")
    raise CommandError(
        "the token may read more than one workspace: name it with --workspace", EXIT_USAGE
    )
