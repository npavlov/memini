"""``memini list-meetings``, ``list-messages``, ``get-meeting`` and ``get-thread``: thin clients
of ``GET /v1/meetings``, ``/v1/messages``, ``/v1/meetings/{id}`` and ``/v1/threads/{id}``, the MCP
tools of the same names.

The options are the tools' arguments (:mod:`memini.cli.reading`); the service checks them.
``--persons`` takes ``me`` (the default), a person id or an alias; ``--query``, if given, keeps the
meetings or messages whose text holds a word of it. Each result prints as its locator, kind, place
and time, its link, then its title or text: the source's text, data, printed with control
characters escaped. ``--json`` prints the API's answer as it is. Needs the ``read`` scope.
"""

import argparse
from collections.abc import Mapping
from typing import Any, Final, get_args

from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api, segment
from memini.cli.common import EXIT_OK, message, printable
from memini.cli.output import as_list, as_mapping, print_fields, print_json, text, when
from memini.cli.reading import (
    add_filter,
    add_include_sensitive,
    add_json,
    add_limit,
    add_time_window,
    left_out,
    time_params,
)
from memini.comms import MESSAGE_ROLES, MessageRole
from memini.search.model import DEFAULT_LIMIT, MAX_LIMIT

__all__ = [
    "TEXT_LINES",
    "add_commands",
    "get_meeting_command",
    "get_thread_command",
    "list_meetings_command",
    "list_messages_command",
]

TEXT_LINES: Final = 6
"""Lines of a snippet or a message printed by a list; the rest is cut, ``--json`` has all of it."""


def _params(args: argparse.Namespace) -> dict[str, str | int | list[str] | None]:
    return {
        "persons": args.persons,
        "query": args.query,
        **time_params(args),
        "workspaces": args.workspaces,
        "sources": args.sources,
        "projects": args.projects,
        "domains": args.domains,
        "limit": args.limit,
        "include_sensitive": "true" if args.include_sensitive else None,
    }


def _print_text(value: object, *, cut: bool = False, lines_max: int | None = TEXT_LINES) -> None:
    lines = value.splitlines() if isinstance(value, str) else []
    shown = lines if lines_max is None else lines[:lines_max]
    for line in shown:
        print(f"    {printable(line.expandtabs(4))}".rstrip())
    if len(shown) < len(lines) or cut:
        print("    …")


def list_meetings_command(args: argparse.Namespace, api: Api) -> int:
    body = api.get("v1/meetings", _params(args))
    if args.json:
        print_json(body)
        return EXIT_OK
    meetings = [as_mapping(meeting) for meeting in as_list(body.get("items"))]
    left_out(body, "meetings")
    if not meetings:
        message("no meetings")
        return EXIT_OK
    for number, meeting in enumerate(meetings, start=1):
        if number > 1:
            print()
        place = f"{text(meeting.get('workspace'))}/{text(meeting.get('source'))}"
        print(
            f"{text(meeting.get('locator'))}  (meeting, {place},"
            f" {when(meeting.get('occurred_at'))})"
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


def list_messages_command(args: argparse.Namespace, api: Api) -> int:
    params = _params(args)
    params["roles"] = args.roles
    body = api.get("v1/messages", params)
    if args.json:
        print_json(body)
        return EXIT_OK
    messages = [as_mapping(each) for each in as_list(body.get("items"))]
    left_out(body, "messages")
    if not messages:
        message("no messages")
        return EXIT_OK
    for number, each in enumerate(messages, start=1):
        if number > 1:
            print()
        place = f"{text(each.get('workspace'))}/{text(each.get('source'))}"
        print(
            f"{text(each.get('locator'))}  (message from {text(each.get('author'))}, {place},"
            f" {when(each.get('occurred_at'))}; {text(each.get('roles'))})"
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


def _record_fields(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        ("id", text(record.get("id"))),
        ("locator", text(record.get("locator"))),
        ("place", f"{text(record.get('workspace'))}/{text(record.get('source'))}"),
        ("occurred", when(record.get("occurred_at"))),
        ("link", text(record.get("link"))),
    ]


def get_thread_command(args: argparse.Namespace, api: Api) -> int:
    body = api.get(f"v1/threads/{segment(args.id)}")
    if args.json:
        print_json(body)
        return EXIT_OK
    fields = _record_fields(body)
    fields[1:1] = [("title", text(body.get("title"))), ("channel", text(body.get("channel")))]
    print_fields(fields)
    for each in map(as_mapping, as_list(body.get("messages"))):
        print()
        author, at = text(each.get("author")), when(each.get("occurred_at"))
        print(f"{author}, {at}  ({text(each.get('anchor'))})")
        _print_text(each.get("text"), cut=each.get("truncated") is True, lines_max=None)
    return EXIT_OK


def get_meeting_command(args: argparse.Namespace, api: Api) -> int:
    params = {"include_transcript": "true" if args.include_transcript else None}
    body = api.get(f"v1/meetings/{segment(args.id)}", params)
    if args.json:
        print_json(body)
        return EXIT_OK
    fields = _record_fields(body)
    fields[1:1] = [("title", text(body.get("title")))]
    fields.append(("participants", text(body.get("participants"))))
    print_fields(fields)
    summary = body.get("summary")
    if isinstance(summary, str):
        print("\nsummary:")
        _print_text(summary, lines_max=None)
    items = [as_mapping(item) for item in as_list(body.get("action_items"))]
    if items:
        print("\naction items:")
        for item in items:
            owner = item.get("owner")
            print(f"  - {text(item.get('text'))}" + ("" if owner is None else f" ({text(owner)})"))
    if body.get("transcript") is not None:
        print("\ntranscript:")
        for each in map(as_mapping, as_list(body.get("transcript"))):
            print(f"  {text(each.get('speaker'))}: {text(each.get('text'))}")
    return EXIT_OK


def _list_options(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument(
        "--query", metavar="TEXT", help=f"words of the topic: only {what} whose text holds one"
    )
    add_filter(
        parser,
        "persons",
        "whose: me (default), a person id, or an alias (a mail address, a chat user id, a"
        " display name)",
        metavar="PERSON",
    )
    add_time_window(parser, what)
    add_filter(parser, "workspaces", "only this workspace", type=parse_workspace_name)
    add_filter(parser, "sources", "only this source", type=parse_source_name)
    add_filter(parser, "projects", f"only {what} linked to this project of the catalog")
    add_filter(parser, "domains", f"only {what} linked to this domain (domain or domain/subdomain)")
    add_include_sensitive(parser)
    add_limit(parser, DEFAULT_LIMIT, MAX_LIMIT)
    add_json(parser)


def add_commands(commands: Commands) -> None:
    """Add ``list-messages``, ``get-thread``, ``list-meetings`` and ``get-meeting``."""
    messages = commands.add_client(
        "list-messages",
        help="messages to, mentioning or from persons (default: to or mentioning me)",
        description="List messages one by one: by default those sent to the persons or that"
        " mention them; --roles author lists their own. Newest first, or with --query the best"
        " match first: the MCP tool list_messages.",
    )
    _list_options(messages, "messages")
    roles: tuple[MessageRole, ...] = get_args(MessageRole)
    add_filter(
        messages,
        "roles",
        f"what the person is in the message (default: {' and '.join(MESSAGE_ROLES)})",
        choices=roles,
    )
    messages.set_defaults(client=list_messages_command)

    thread = commands.add_client(
        "get-thread",
        help="a whole thread by its id",
        description="A chat or mail thread with all its messages: the MCP tool get_thread.",
    )
    thread.add_argument("id", help="the thread's id, as results give it")
    add_json(thread)
    thread.set_defaults(client=get_thread_command)

    meetings = commands.add_client(
        "list-meetings",
        help="meetings persons took part in (default: me)",
        description="List the meetings persons took part in, newest first, or with --query"
        " those whose notes hold a word of it, best match first: the MCP tool list_meetings.",
    )
    _list_options(meetings, "meetings")
    meetings.set_defaults(client=list_meetings_command)

    meeting = commands.add_client(
        "get-meeting",
        help="a meeting's notes by its id",
        description="A meeting's notes, summary and action items, and with --include-transcript"
        " its transcript: the MCP tool get_meeting.",
    )
    meeting.add_argument("id", help="the meeting's id, as results give it")
    meeting.add_argument(
        "--include-transcript", action="store_true", help="also the transcript, segment by segment"
    )
    add_json(meeting)
    meeting.set_defaults(client=get_meeting_command)
