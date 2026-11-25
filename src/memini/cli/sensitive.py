"""Client commands ``memini sensitive mark|unmark|list``: records marked sensitive, through the
HTTP API (``PUT`` and ``GET /v1/workspaces/{workspace}/records/sensitive``,
:mod:`memini.api.sensitive`). Need the ``admin`` scope.

A record is named as ``memini forget`` names one: by ``--record`` (its id, as search results give
it) or by ``--source`` and ``--external-id`` (with ``--kind`` when the source has records of two
kinds under that id). A marked record stays stored and indexed and is left out of answers unless a
reader asks for sensitive records with a token that has the ``sensitive`` scope.
"""

import argparse
from typing import Any

from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api, segment
from memini.cli.common import EXIT_OK, EXIT_USAGE, CommandError, print_table
from memini.cli.forget import record_id_argument
from memini.cli.output import as_list, as_mapping, print_fields, print_json, text, timestamp
from memini.ids import RECORD_KINDS

__all__ = ["add_commands"]


def _target(args: argparse.Namespace) -> dict[str, Any]:
    if args.record is not None and (args.external_id is not None or args.kind is not None):
        raise CommandError(
            "--record names the record alone; leave out --external-id and --kind", EXIT_USAGE
        )
    if args.source is not None and args.external_id is None:
        raise CommandError("--source needs --external-id", EXIT_USAGE)
    body: dict[str, Any] = (
        {"record_id": args.record}
        if args.record is not None
        else {"source": args.source, "external_id": args.external_id, "record_kind": args.kind}
    )
    return {key: value for key, value in body.items() if value is not None}


def _mark(args: argparse.Namespace, api: Api, *, sensitive: bool) -> int:
    body = _target(args) | {"sensitive": sensitive}
    reason: str | None = getattr(args, "reason", None)
    if reason is not None:
        body["reason"] = reason
    workspace: str = args.workspace
    answer = api.put(f"v1/workspaces/{segment(workspace)}/records/sensitive", body)
    if args.json:
        print_json(answer)
        return EXIT_OK
    print_fields(
        [
            ("record", text(answer.get("record_id"))),
            ("kind", text(answer.get("record_kind"))),
            ("source", text(answer.get("source"))),
            ("external id", text(answer.get("external_id"))),
            ("sensitive", "yes" if answer.get("sensitive") else "no"),
            ("reason", text(answer.get("reason"))),
            ("marked at", timestamp(answer.get("sensitive_at"))),
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
    rows = [as_mapping(each) for each in as_list(answer.get("records"))]
    if not rows:
        print(f"No record of {workspace} is marked sensitive.")
        return EXIT_OK
    print_table(
        ["RECORD", "KIND", "SOURCE", "EXTERNAL ID", "MARKED", "REASON"],
        [
            [
                text(row.get("record_id"))[:12],
                text(row.get("record_kind")),
                text(row.get("source")),
                text(row.get("external_id")),
                timestamp(row.get("sensitive_at")),
                text(row.get("reason")),
            ]
            for row in rows
        ],
    )
    return EXIT_OK


def _record_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--record",
        metavar="ID",
        type=record_id_argument,
        help="the record's id, as search results give it",
    )
    which.add_argument(
        "--source", type=parse_source_name, help="the record's source; with --external-id"
    )
    parser.add_argument(
        "--external-id",
        metavar="ID",
        help="the record's id in its source (a thread key, a meeting id, refs/heads/main:path)",
    )
    parser.add_argument(
        "--kind",
        choices=sorted(RECORD_KINDS),
        help="the record's kind, when the source has two under that external id",
    )
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
