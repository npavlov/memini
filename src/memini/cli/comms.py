"""``memini meetings`` and ``memini messages``: thin clients of ``GET /v1/meetings`` and
``GET /v1/messages``, the MCP tools ``list_meetings`` and ``list_messages``.

The arguments are the tools'; the service checks them. ``--person`` takes ``me`` (the default), a
person id or an alias; a query, if given, keeps the meetings or messages whose text holds a word of
it. Each result prints as its locator, kind, place and time, its link, then its title or text: the
source's text, data, printed with control characters escaped. ``--json`` prints the API's answer
as it is. Needs the ``read`` scope.
"""

import argparse
from typing import Final, get_args

from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api
from memini.cli.common import EXIT_OK, message, printable
from memini.cli.decisions import parse_time
from memini.cli.output import as_list, as_mapping, print_json, text, when
from memini.comms import MESSAGE_ROLES, MessageRole
from memini.search.model import DEFAULT_LIMIT, MAX_LIMIT

__all__ = ["TEXT_LINES", "add_commands", "meetings_command", "messages_command"]

TEXT_LINES: Final = 6
"""Lines of a snippet or a message printed; the rest is cut, ``--json`` has all of it."""


def _limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"a number from 1 to {MAX_LIMIT}")
    return limit


def _params(args: argparse.Namespace) -> dict[str, str | int | list[str] | None]:
    return {
        "person": args.person,
        "query": args.query,
        "since": None if args.since is None else args.since.isoformat(),
        "until": None if args.until is None else args.until.isoformat(),
        "workspaces": args.workspace,
        "sources": args.source,
        "limit": args.limit,
        "include_sensitive": "true" if args.include_sensitive else None,
    }


def _print_text(value: object, *, cut: bool = False) -> None:
    lines = value.splitlines() if isinstance(value, str) else []
    for line in lines[:TEXT_LINES]:
        print(f"    {printable(line.expandtabs(4))}".rstrip())
    if len(lines) > TEXT_LINES or cut:
        print("    …")


def meetings_command(args: argparse.Namespace, api: Api) -> int:
    body = api.get("v1/meetings", _params(args))
    if args.json:
        print_json(body)
        return EXIT_OK
    meetings = [as_mapping(meeting) for meeting in as_list(body.get("meetings"))]
    if not meetings:
        message("no meetings")
        return EXIT_OK
    for number, meeting in enumerate(meetings, start=1):
        if number > 1:
            print()
        place = f"{text(meeting.get('workspace'))}/{text(meeting.get('source'))}"
        print(
            f"{text(meeting.get('locator'))}  (meeting, {place},"
            f" {when(meeting.get('started_at') or meeting.get('occurred_at'))})"
        )
        print(f"  {text(meeting.get('title'))}")
        participants = meeting.get("participants")
        if participants:
            print(f"  with {text(participants)}")
        link = meeting.get("link")
        if isinstance(link, str):
            print(f"  link: {text(link)}")
        _print_text(meeting.get("snippet"), cut=meeting.get("truncated") is True)
    return EXIT_OK


def messages_command(args: argparse.Namespace, api: Api) -> int:
    params = _params(args)
    params["roles"] = args.role
    body = api.get("v1/messages", params)
    if args.json:
        print_json(body)
        return EXIT_OK
    messages = [as_mapping(each) for each in as_list(body.get("messages"))]
    if not messages:
        message("no messages")
        return EXIT_OK
    for number, each in enumerate(messages, start=1):
        if number > 1:
            print()
        place = f"{text(each.get('workspace'))}/{text(each.get('source'))}"
        print(
            f"{text(each.get('locator'))}  (message from {text(each.get('author'))}, {place},"
            f" {when(each.get('sent_at'))}; {text(each.get('roles'))})"
        )
        thread = " · ".join(
            part for part in (each.get("channel"), each.get("title")) if isinstance(part, str)
        )
        if thread:
            print(f"  in {printable(thread)}")
        link = each.get("link")
        if isinstance(link, str):
            print(f"  link: {text(link)}")
        _print_text(each.get("text"), cut=each.get("truncated") is True)
    return EXIT_OK


def _common(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument(
        "query", nargs="?", help=f"words of the topic: only {what} whose text holds one"
    )
    parser.add_argument(
        "--person",
        default="me",
        help="whose: me (default), a person id, or an alias (a mail address, a chat user id, a"
        " display name)",
    )
    parser.add_argument(
        "--since", type=parse_time, metavar="TIME", help="from this time on (ISO 8601, with Z)"
    )
    parser.add_argument(
        "--include-sensitive",
        action="store_true",
        help="also records marked sensitive; needs a token with the sensitive scope",
    )
    parser.add_argument("--until", type=parse_time, metavar="TIME", help="before this time")
    parser.add_argument(
        "--workspace",
        action="append",
        type=parse_workspace_name,
        metavar="NAME",
        help="only this workspace; repeat for more",
    )
    parser.add_argument(
        "--source",
        action="append",
        type=parse_source_name,
        metavar="NAME",
        help="only this source; repeat for more",
    )
    parser.add_argument(
        "--limit",
        type=_limit,
        default=DEFAULT_LIMIT,
        help=f"the most results, 1 to {MAX_LIMIT} (default {DEFAULT_LIMIT})",
    )
    parser.add_argument("--json", action="store_true", help="print the API's JSON answer")


def add_commands(commands: Commands) -> None:
    """Add ``meetings`` and ``messages``."""
    meetings = commands.add_client(
        "meetings",
        help="meetings a person took part in (default: me)",
        description="List the meetings a person took part in, newest first, or with a query those"
        " whose notes hold a word of it, best match first: the MCP tool list_meetings.",
    )
    _common(meetings, "meetings")
    meetings.set_defaults(client=meetings_command)

    messages = commands.add_client(
        "messages",
        help="messages to, mentioning or from a person (default: to or mentioning me)",
        description="List messages one by one: by default those sent to the person or that"
        " mention them; --role author lists the person's own. Newest first, or with a query the"
        " best match first: the MCP tool list_messages.",
    )
    _common(messages, "messages")
    roles: tuple[MessageRole, ...] = get_args(MessageRole)
    messages.add_argument(
        "--role",
        action="append",
        choices=roles,
        help="what the person is in the message; repeat for more"
        f" (default: {' and '.join(MESSAGE_ROLES)})",
    )
    messages.set_defaults(client=messages_command)
