"""Client commands for runs: ``memini runs`` lists them, ``memini run`` shows one with its report.

Both read through the HTTP API (:mod:`memini.api.runs`) and need the ``read`` scope. The report is
printed readably when it is of version 1 (:mod:`memini.runs` describes it), and as JSON otherwise;
``--json`` prints the API's answer as it is.
"""

import argparse
from collections.abc import Mapping
from typing import Any, Final
from uuid import UUID

from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api, segment
from memini.cli.common import EXIT_OK, message, print_table
from memini.cli.output import (
    as_list,
    as_mapping,
    elapsed,
    milliseconds,
    print_fields,
    print_json,
    text,
    timestamp,
    when,
)
from memini.runs import DEFAULT_RUN_PAGE, MAX_RUN_PAGE

__all__ = ["add_commands", "print_run"]

REPORT_VERSION: Final = 1
"""The report version this module prints field by field."""

_ERROR_WIDTH: Final = 60


def _limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= MAX_RUN_PAGE:
        raise argparse.ArgumentTypeError(f"a number from 1 to {MAX_RUN_PAGE}")
    return limit


def _short(value: object) -> str:
    line = text(value)
    return line if len(line) <= _ERROR_WIDTH else line[: _ERROR_WIDTH - 1] + "…"


def runs_command(args: argparse.Namespace, api: Api) -> int:
    workspace: str = args.workspace
    body = api.get(
        f"v1/workspaces/{segment(workspace)}/runs",
        {
            "sources": args.source,
            "streams": args.stream,
            "limit": args.limit,
            "cursor": args.cursor,
        },
    )
    if args.json:
        print_json(body)
        return EXIT_OK
    found = [as_mapping(run) for run in as_list(body.get("items"))]
    if not found:
        message("no runs")
        return EXIT_OK
    print_table(
        ("ID", "KIND", "STATUS", "SOURCE", "STREAM", "STARTED", "DURATION", "ERROR"),
        (
            (
                text(run.get("id")),
                text(run.get("kind")),
                text(run.get("status")),
                text(run.get("source")),
                text(run.get("stream")),
                when(run.get("started_at")),
                elapsed(run.get("started_at"), run.get("finished_at")),
                _short(run.get("error")),
            )
            for run in found
        ),
    )
    more = body.get("next_cursor")
    if isinstance(more, str):
        message(f"more runs: add --cursor {more}")
    return EXIT_OK


def run_command(args: argparse.Namespace, api: Api) -> int:
    run_id: UUID = args.id
    run = api.get(f"v1/runs/{run_id}")
    if args.json:
        print_json(run)
    else:
        print_run(run)
    return EXIT_OK


def print_run(run: Mapping[str, Any]) -> None:
    """Print a run and its report readably: the run's fields, its steps, counts and LLM use."""
    report = as_mapping(run.get("report"))
    fields = [
        ("run", text(run.get("id"))),
        ("kind", text(run.get("kind"))),
        ("status", text(run.get("status"))),
        ("workspace", text(run.get("workspace"))),
        ("source", text(run.get("source"))),
        ("stream", text(run.get("stream"))),
        ("started", timestamp(run.get("started_at"))),
        ("finished", timestamp(run.get("finished_at"), "running")),
    ]
    if report.get("version") == REPORT_VERSION:
        fields.append(("duration", milliseconds(report.get("duration_ms"))))
    if run.get("error") is not None:
        fields.append(("error", text(run.get("error"))))
    print_fields(fields)
    if not report:
        return
    if report.get("version") != REPORT_VERSION:
        print(f"\nreport (version {text(report.get('version'))}, shown as JSON):")
        print_json(report)
        return

    steps = [as_mapping(step) for step in as_list(report.get("steps"))]
    if steps:
        print("\nsteps")
        print_table(
            ("  NAME", "STATUS", "STARTED", "DURATION", "ERROR"),
            (
                (
                    "  " + text(step.get("name")),
                    text(step.get("status")),
                    timestamp(step.get("started_at")),
                    milliseconds(step.get("duration_ms"), "running"),
                    text(step.get("error"), ""),
                )
                for step in steps
            ),
        )
    omitted = report.get("steps_omitted")
    if isinstance(omitted, int) and omitted > 0:
        print(f"  … and {omitted} more steps, timed but not listed")

    counts = as_mapping(report.get("counts"))
    if counts:
        print("\ncounts")
        width = max(len(text(name)) for name in counts)
        for name, value in sorted(counts.items()):
            print(f"  {text(name).ljust(width)}  {text(value)}")

    llm = as_mapping(report.get("llm"))
    calls = llm.get("calls")
    if isinstance(calls, int) and calls > 0:
        print(
            f"\nllm: {calls} calls, {text(llm.get('input_tokens'))} input tokens,"
            f" {text(llm.get('output_tokens'))} output tokens"
        )
        for model, usage in as_mapping(llm.get("by_model")).items():
            use = as_mapping(usage)
            print(
                f"  {text(model)}: {text(use.get('calls'))} calls,"
                f" {text(use.get('input_tokens'))} in, {text(use.get('output_tokens'))} out"
            )


def add_commands(commands: Commands) -> None:
    """Add ``runs`` and ``run``."""
    runs = commands.add_client(
        "runs",
        help="list runs, newest first",
        description="List the runs of a workspace, newest first: syncs, reindexes and other"
        " work the service did, with their status.",
    )
    runs.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")
    runs.add_argument(
        "--source",
        action="append",
        type=parse_source_name,
        metavar="NAME",
        help="only runs of this source; repeat for more",
    )
    runs.add_argument(
        "--stream",
        action="append",
        metavar="KEY",
        help="only runs of this stream (refs/heads/main); needs --source; repeat for more",
    )
    runs.add_argument(
        "--limit",
        type=_limit,
        default=DEFAULT_RUN_PAGE,
        help=f"runs per page, 1 to {MAX_RUN_PAGE} (default {DEFAULT_RUN_PAGE})",
    )
    runs.add_argument("--cursor", metavar="CURSOR", help="the next page, as the last page said")
    runs.add_argument("--json", action="store_true", help="print the API's JSON answer")
    runs.set_defaults(client=runs_command)

    run = commands.add_client(
        "run",
        help="show a run with its report",
        description="Show one run with its report: steps and their durations, counts, LLM use.",
    )
    run.add_argument("id", type=UUID, help="the run's id, as `runs` shows")
    run.add_argument("--json", action="store_true", help="print the API's JSON answer")
    run.set_defaults(client=run_command)
