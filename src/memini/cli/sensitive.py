"""Client commands ``memini sensitive mark|unmark <workspace> <id>`` and ``memini sensitive list
<workspace>``: records marked sensitive, through the HTTP API (``PUT
/v1/workspaces/{workspace}/records/{id}/sensitive``, ``GET
/v1/workspaces/{workspace}/records/sensitive``, :mod:`memini.api.sensitive`). Need the ``admin``
scope.

A record is named by its id, as results give it. A marked record stays stored and indexed and is
left out of answers unless a reader asks for sensitive records with a token that has the
``sensitive`` scope.
"""

import argparse

from memini.cli.arguments import Commands, parse_workspace_name
from memini.cli.client import Api, Json, segment
from memini.cli.common import EXIT_OK, print_table
from memini.cli.forget import record_id_argument, record_path
from memini.cli.output import as_list, as_mapping, print_fields, print_json, text, timestamp

__all__ = ["add_commands"]


def _mark(args: argparse.Namespace, api: Api, *, sensitive: bool) -> int:
    body: Json = {"sensitive": sensitive}
    reason: str | None = getattr(args, "reason", None)
    if reason is not None:
        body["reason"] = reason
    workspace: str = args.workspace
    answer = api.put(record_path(workspace, args.id, "sensitive"), body)
    if args.json:
        print_json(answer)
        return EXIT_OK
    print_fields(
        [
            ("record", text(answer.get("id"))),
            ("kind", text(answer.get("kind"))),
            ("source", text(answer.get("source"))),
            ("external id", text(answer.get("external_id"))),
            ("sensitive", "yes" if answer.get("sensitive") else "no"),
            ("reason", text(answer.get("reason"))),
            ("marked at", timestamp(answer.get("marked_at"))),
            ("changed", "yes" if answer.get("changed") else "no, it was so already"),
        ]
    )
    return EXIT_OK


def mark_command(args: argparse.Namespace, api: Api) -> int:
    return _mark(args, api, sensitive=True)


def unmark_command(args: argparse.Namespace, api: Api) -> int:
    return _mark(args, api, sensitive=False)


def list_command(args: argparse.Namespace, api: Api) -> int:
    workspace: str = args.workspace
    answer = api.get(f"v1/workspaces/{segment(workspace)}/records/sensitive")
    if args.json:
        print_json(answer)
        return EXIT_OK
    rows = [as_mapping(each) for each in as_list(answer.get("items"))]
    if not rows:
        print(f"No record of {workspace} is marked sensitive.")
        return EXIT_OK
    print_table(
        ["RECORD", "KIND", "SOURCE", "EXTERNAL ID", "MARKED", "REASON"],
        [
            [
                text(row.get("id"))[:12],
                text(row.get("kind")),
                text(row.get("source")),
                text(row.get("external_id")),
                timestamp(row.get("marked_at")),
                text(row.get("reason")),
            ]
            for row in rows
        ],
    )
    return EXIT_OK


def _record_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")
    parser.add_argument("id", type=record_id_argument, help="the record's id, as results give it")
    parser.add_argument("--json", action="store_true", help="print the API's JSON answer")


def add_commands(commands: Commands) -> None:
    """Add ``sensitive``."""
    sensitive = commands.add_client(
        "sensitive",
        help="mark records sensitive, clear marks, list them (admin)",
        description="A record marked sensitive stays stored and indexed, and is left out of"
        " answers unless a reader asks for sensitive records with a token that has the"
        " sensitive scope. No model reads it.",
    )
    actions = sensitive.add_subparsers(metavar="<command>", required=True)
    mark = actions.add_parser(
        "mark", help="mark a record sensitive", description="Mark one record sensitive."
    )
    _record_options(mark)
    mark.add_argument("--reason", help="why, for whoever lists the marks (at most 200 characters)")
    mark.set_defaults(client=mark_command)
    unmark = actions.add_parser(
        "unmark", help="clear a record's mark", description="Clear the mark of one record."
    )
    _record_options(unmark)
    unmark.set_defaults(client=unmark_command)
    listing = actions.add_parser(
        "list",
        help="list the records marked sensitive",
        description="The records of a workspace marked sensitive, last marked first: names and"
        " reasons, no content.",
    )
    listing.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")
    listing.add_argument("--json", action="store_true", help="print the API's JSON answer")
    listing.set_defaults(client=list_command)
