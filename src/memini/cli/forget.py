"""Client command ``memini forget <workspace> <id>``: the hard delete of one record, through the
HTTP API (``POST /v1/workspaces/{workspace}/records/{id}/forget``, :mod:`memini.api.forget`).
Needs the ``admin`` scope.

The record is named by its id, as results give it. The command prints the audit entry and what
was deleted; ``--json`` prints the API's answer.
"""

import argparse
import re
from typing import Final

from memini.cli.arguments import Commands, parse_workspace_name
from memini.cli.client import Api, segment
from memini.cli.common import EXIT_OK
from memini.cli.output import as_mapping, print_fields, print_json, text, timestamp

__all__ = ["add_commands", "forget_command", "record_id_argument", "record_path"]

_RECORD_ID: Final = re.compile(r"[0-9a-f]{64}")


def record_id_argument(value: str) -> str:
    if not _RECORD_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("a record id is 64 lowercase hex characters")
    return value


def record_path(workspace: str, record: str, action: str) -> str:
    """The API path of an action on one record: ``forget`` or ``sensitive``."""
    return f"v1/workspaces/{segment(workspace)}/records/{record}/{action}"


def forget_command(args: argparse.Namespace, api: Api) -> int:
    answer = api.post(record_path(args.workspace, args.id, "forget"), {})
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
    forget.add_argument("id", type=record_id_argument, help="the record's id, as results give it")
    forget.add_argument("--json", action="store_true", help="print the API's JSON answer")
    forget.set_defaults(client=forget_command)
