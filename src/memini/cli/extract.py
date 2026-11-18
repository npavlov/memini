"""Client command ``memini extract``: enqueue the decision extraction of stored records that the
current extractor has not read, through the HTTP API (``POST /v1/workspaces/{workspace}/extract``,
:mod:`memini.api.extract`). Needs the ``admin`` scope.

It is the backfill after an LLM provider is configured (ingest enqueues extraction only while one
is) and the re-run after the extractor changes (a new prompt version or another model). It prints
how many records it enqueued, found queued already, found read by the current extractor, or
skipped because no LLM may read their source, per source with the extractor; ``--dry-run`` counts
without enqueuing, ``--json`` prints the API's answer. The jobs run in the service's worker; their
runs appear in ``memini runs <workspace>``.
"""

import argparse
from typing import Any

from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api, segment
from memini.cli.common import EXIT_OK, print_table
from memini.cli.decisions import parse_time
from memini.cli.output import NONE, as_list, as_mapping, print_fields, print_json, text

__all__ = ["add_commands", "extract_command"]

_COUNTS = ("records", "current", "enqueued", "queued", "skipped")


def extract_command(args: argparse.Namespace, api: Api) -> int:
    body: dict[str, Any] = {"dry_run": bool(args.dry_run)}
    if args.sources:
        body["sources"] = list(dict.fromkeys(args.sources))
    if args.since is not None:
        body["since"] = args.since.isoformat()
    workspace: str = args.workspace
    answer = api.post(f"v1/workspaces/{segment(workspace)}/extract", body)
    if args.json:
        print_json(answer)
        return EXIT_OK
    totals = as_mapping(answer.get("totals"))
    enqueued = "would enqueue" if answer.get("dry_run") else "enqueued"
    print_fields(
        [
            ("workspace", text(answer.get("workspace"))),
            ("records", f"{text(totals.get('records'))} live threads and meetings"),
            ("current", f"{text(totals.get('current'))} read by the current extractor already"),
            (enqueued, text(totals.get("enqueued"))),
            ("queued", f"{text(totals.get('queued'))} queued or running already"),
            ("skipped", f"{text(totals.get('skipped'))} of sources no configured LLM may read"),
        ]
    )
    sources = [as_mapping(source) for source in as_list(answer.get("sources"))]
    if sources:
        print()
        print_table(
            ("SOURCE", "EXTRACTOR", *(name.upper() for name in _COUNTS)),
            (
                (
                    text(source.get("source")),
                    text(source.get("extractor"))
                    if source.get("extractor") is not None
                    else f"{NONE} ({text(source.get('skipped_reason'))})",
                    *(text(source.get(name)) for name in _COUNTS),
                )
                for source in sources
            ),
        )
    return EXIT_OK


def add_commands(commands: Commands) -> None:
    """Add ``extract``."""
    extract = commands.add_client(
        "extract",
        help="extract decisions from stored records the current extractor has not read (admin)",
        description="Enqueue the decision extraction of every live thread and meeting of the"
        " workspace whose current content the current extractor of its source (the model its LLM"
        " policies allow, and the prompt version) has not read: records stored before an LLM"
        " provider was configured, and every record after a new prompt version or another"
        " model. Oldest first. A record whose extraction is queued or running already gets no"
        " second job, so running it again enqueues nothing while the jobs wait. Items recorded"
        " by hand are never touched; earlier items stay in the journal, and the supersession"
        " check of the new reading links or skips them.",
    )
    extract.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")
    extract.add_argument(
        "--source",
        dest="sources",
        action="append",
        type=parse_source_name,
        metavar="NAME",
        help="only the records of this source; repeat for more (default: every source)",
    )
    extract.add_argument(
        "--since",
        type=parse_time,
        metavar="TIME",
        help="only records that occurred at or after this time (ISO 8601, with Z)",
    )
    extract.add_argument(
        "--dry-run", action="store_true", help="count the records, and enqueue nothing"
    )
    extract.add_argument("--json", action="store_true", help="print the API's JSON answer")
    extract.set_defaults(client=extract_command)
