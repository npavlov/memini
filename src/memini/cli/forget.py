"""Client command ``memini forget``: the hard delete of one record, through the HTTP API
(``POST /v1/workspaces/{workspace}/forget``, :mod:`memini.api.forget`). Needs the ``admin`` scope.

The record is named by ``--record`` (its id, as search results give it) or by ``--source`` and
``--external-id`` (with ``--kind`` when the source has records of two kinds under that id). The
command prints the audit entry and what was deleted; ``--json`` prints the API's answer.
"""

import argparse
import re
from typing import Any, Final

from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api, segment
from memini.cli.common import EXIT_OK, EXIT_USAGE, CommandError
from memini.cli.output import as_mapping, print_fields, print_json, text, timestamp
from memini.ids import RECORD_KINDS

__all__ = ["add_commands", "forget_command"]

_RECORD_ID: Final = re.compile(r"[0-9a-f]{64}")


def _record_id(value: str) -> str:
    if not _RECORD_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("a record id is 64 lowercase hex characters")
    return value


def forget_command(args: argparse.Namespace, api: Api) -> int:
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
    workspace: str = args.workspace
    answer = api.post(
        f"v1/workspaces/{segment(workspace)}/forget",
        {key: value for key, value in body.items() if value is not None},
    )
    if args.json:
        print_json(answer)
        return EXIT_OK
    removed = as_mapping(answer.get("removed"))
    print_fields(
        [
            ("forgot", text(answer.get("record_id"))),
            ("kind", text(answer.get("record_kind"))),
            ("source", text(answer.get("source"))),
            ("workspace", text(answer.get("workspace"))),
            ("at", timestamp(answer.get("forgotten_at"))),
            ("audit entry", text(answer.get("id"))),
            ("chunks deleted", text(removed.get("chunks"))),
            ("symbols deleted", text(removed.get("symbols"))),
            ("raw responses deleted", text(removed.get("raw_responses"))),
            ("knowledge items citing it", text(answer.get("citing_items"))),
        ]
    )
    return EXIT_OK


def add_commands(commands: Commands) -> None:
    """Add ``forget``."""
    forget = commands.add_client(
        "forget",
        help="delete one record and its index for good (admin)",
        description="Delete one record, with its chunks, symbols and raw responses, and keep an"
        " audit entry of who and when. Knowledge items that cite it stay and keep their"
        " reference. A later sync of its source does not store it again. Cannot be undone.",
    )
    forget.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")
    which = forget.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--record", metavar="ID", type=_record_id, help="the record's id, as search results give it"
    )
    which.add_argument(
        "--source", type=parse_source_name, help="the record's source; with --external-id"
    )
    forget.add_argument(
        "--external-id",
        metavar="ID",
        help="the record's id in its source (a thread key, a meeting id, refs/heads/main:path)",
    )
    forget.add_argument(
        "--kind",
        choices=sorted(RECORD_KINDS),
        help="the record's kind, when the source has two under that external id",
    )
    forget.add_argument("--json", action="store_true", help="print the API's JSON answer")
    forget.set_defaults(client=forget_command)
