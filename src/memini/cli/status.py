"""The client command ``memini status``: whether the base is current, with a row per stream.

One call to ``GET /v1/status`` (:mod:`memini.api.status`): the same answer as the MCP ``status``
tool, whose definitions are in :mod:`memini.status`. Needs a token with the ``read`` scope; a token
limited to one workspace shows that workspace. ``--json`` prints the answer as it is.

What it prints: the verdict, the database and the index across workspaces, and a line when the
service's MCP endpoint is switched off; a table of the streams
(workspace, source, stream, last result, lag, how far back the history is loaded, index, current);
then, if any, the streams whose runs fail with the first line of the last error, and the workspaces
whose index is behind with the command that brings it up to date. It exits 0 whether or not the
base is current: the answer is in the output, and ``--json`` has it for scripts.

``--gate`` makes it the freshness gate (principle 10): the same output, and the exit code says the
verdict: 0 when the base is current, :data:`~memini.cli.common.EXIT_NOT_CURRENT` when it is not (a
stream or an index is behind, or the database is not reachable). A service that cannot be reached
or refuses the token exits as for any client command (3, 5). The verdict is the answer's
``current``, the one ``GET /v1/status?gate=true`` turns into its status code.
"""

import argparse
from collections.abc import Mapping
from typing import Any, Final, cast

from memini.cli.arguments import Commands
from memini.cli.client import Api
from memini.cli.common import EXIT_NOT_CURRENT, EXIT_OK, message, print_table
from memini.cli.output import (
    NONE,
    as_list,
    as_mapping,
    print_fields,
    print_json,
    seconds,
    text,
    when,
)

__all__ = ["add_commands", "status_command"]

_COMMIT: Final = 12
"""Hex digits of a commit shown in the table."""

_REASONS: Final = {
    "never_checked": "never checked",
    "lag": "lag",
    "not_indexed": "not indexed",
    "index_stale": "index stale",
}


def _last_result(stream: Mapping[str, Any]) -> str:
    run = as_mapping(stream.get("last_run"))
    if not run:
        return "never ran"
    status = text(run.get("status"))
    if run.get("finished_at") is None:
        result = f"{status} since {when(run.get('started_at'))}"
    else:
        result = f"{status} {when(run.get('finished_at'))}"
    failures = stream.get("failures")
    if isinstance(failures, int) and failures > 0 and not (failures == 1 and status == "failed"):
        result += f" ({failures} failed)"
    return result


def _history(stream: Mapping[str, Any]) -> str:
    git = as_mapping(stream.get("git"))
    if git:
        commit = git.get("commit")
        pin = text(commit)[:_COMMIT] if isinstance(commit, str) else NONE
        return f"{pin} (partial)" if git.get("partial") is True else pin
    start = stream.get("checked_from") or stream.get("checked_until")
    shown = when(start)
    loaded = stream.get("history_loaded")
    if loaded is True:
        shown += " (all)"
    elif loaded is False:
        shown += f" (loading to {when(stream.get('history_start'))})"
    return shown


def _index(stream: Mapping[str, Any], workspace: Mapping[str, Any]) -> str:
    behind = as_list(stream.get("behind"))
    not_indexed = stream.get("records_not_indexed")
    records = stream.get("records")
    if "index_stale" in behind:
        return "stale"
    if isinstance(not_indexed, int) and not_indexed > 0:
        return f"{not_indexed} of {text(records)} not indexed"
    if workspace.get("state") == "off":
        return "off"
    if not records:
        return NONE
    return "ok"


def _current(stream: Mapping[str, Any]) -> str:
    current = stream.get("current")
    if current is not False:
        return text(current)
    reasons = [_REASONS.get(str(reason), text(reason)) for reason in as_list(stream.get("behind"))]
    return f"no ({', '.join(reasons)})" if reasons else "no"


def _failure_line(workspace: str, stream: Mapping[str, Any]) -> str:
    failure = as_mapping(stream.get("last_failure"))
    failures = stream.get("failures")
    times = "once" if failures == 1 else f"{text(failures)} times"
    line = (
        f"{text(workspace)}/{text(stream.get('source'))} {text(stream.get('key'))}: failed {times}"
        f" in a row, last at {when(failure.get('finished_at'))} (run {text(failure.get('id'))})"
    )
    error = failure.get("error")
    return line if error is None else f"{line}: {text(error)}"


def _index_line(workspace: Mapping[str, Any]) -> str:
    index = as_mapping(workspace.get("index"))
    line = f"{text(workspace.get('name'))}: index {text(index.get('state'))}"
    changed = as_list(index.get("changed"))
    if changed:
        line += f" (changed: {text(changed)})"
    not_indexed = index.get("records_not_indexed")
    if isinstance(not_indexed, int) and not_indexed > 0:
        line += f", {not_indexed} records not indexed"
    rebuild = index.get("rebuild")
    if isinstance(rebuild, Mapping):
        return f"{line}; {_rebuild_words(cast(Mapping[str, Any], rebuild))}"
    reindex = index.get("reindex")
    return line if reindex is None else f"{line}; run `{text(reindex)}`"


def _rebuild_words(rebuild: Mapping[str, Any]) -> str:
    """``rebuilding: 312 of 1472 records (21%)``, or that the rebuild waits."""
    if rebuild.get("state") != "running":
        return "rebuild queued"
    done, total = rebuild.get("records_done"), rebuild.get("records_total")
    if not isinstance(done, int) or not isinstance(total, int):
        return "rebuilding"
    return f"rebuilding: {done} of {total} records ({text(rebuild.get('percent'))}%)"


def status_command(args: argparse.Namespace, api: Api) -> int:
    body = api.get("v1/status")
    _print(body, as_json=args.json)
    if args.gate and body.get("current") is not True:
        return EXIT_NOT_CURRENT
    return EXIT_OK


def _print(body: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        print_json(body)
        return
    database = as_mapping(body.get("database"))
    if database.get("reachable") is True:
        reached = (
            f"reachable, schema version {text(database.get('schema_version'))}"
            f" (this version expects {text(database.get('expected_schema_version'))})"
        )
    else:
        reached = "not reachable"
    index = as_mapping(body.get("index"))
    fields = [
        ("current", text(body.get("current"))),
        ("as of", when(body.get("as_of"))),
        ("version", text(body.get("version"))),
        ("database", reached),
        ("index", f"{text(index.get('state'))}: {text(index.get('note'))}"),
    ]
    if body.get("mcp_enabled") is False:  # on by default, and not said by an older service
        fields.append(("mcp", "off: the MCP endpoint is switched off (mcp_enabled)"))
    print_fields(fields)
    workspaces = [as_mapping(w) for w in as_list(body.get("workspaces"))]
    rows: list[tuple[str, ...]] = []
    failing: list[str] = []
    for workspace in workspaces:
        name = text(workspace.get("name"))
        workspace_index = as_mapping(workspace.get("index"))
        for stream in (as_mapping(s) for s in as_list(workspace.get("streams"))):
            key = text(stream.get("key"))
            rows.append(
                (
                    name,
                    text(stream.get("source")),
                    f"{key} (missing)" if stream.get("missing") is True else key,
                    _last_result(stream),
                    seconds(stream.get("lag_seconds")),
                    _history(stream),
                    _index(stream, workspace_index),
                    _current(stream),
                )
            )
            if stream.get("last_failure"):
                failing.append(_failure_line(name, stream))
    if rows:
        print()
        print_table(
            (
                "WORKSPACE",
                "SOURCE",
                "STREAM",
                "LAST RESULT",
                "LAG",
                "HISTORY FROM",
                "INDEX",
                "CURRENT",
            ),
            rows,
        )
    else:
        message("no streams yet: add a source with `memini source add`")
    behind = [_index_line(w) for w in workspaces if as_mapping(w.get("index")).get("behind")]
    for title, lines in (("failing", failing), ("index behind", behind)):
        if lines:
            print(f"\n{title}:")
            for line in lines:
                print(f"  {line}")


def add_commands(commands: Commands) -> None:
    """Add ``status``."""
    status = commands.add_client(
        "status",
        help="show whether the base is current, stream by stream",
        description="Show whether the base is current: per stream its last result, lag, how far"
        " back its history is loaded and whether its records are indexed; per workspace its"
        " index and the command that brings it up to date. The same answer as the MCP status"
        f" tool. Exits 0 whether or not the base is current, unless --gate: then {EXIT_NOT_CURRENT}"
        " when it is not.",
    )
    status.add_argument("--json", action="store_true", help="print the API's JSON answer")
    status.add_argument(
        "--gate",
        action="store_true",
        help=f"the freshness gate: exit {EXIT_NOT_CURRENT} when the base is not current",
    )
    status.set_defaults(client=status_command)
