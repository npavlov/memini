"""Client commands for workspaces, sources, streams and syncs: ``memini workspace``, ``source``,
``streams`` and ``sync``.

Each is one or a few calls to the HTTP API (:mod:`memini.api.workspaces`,
:mod:`memini.api.sources`); the service checks scopes and the token's workspace limit, checks a
source's location and enqueues syncs. Reading needs a token with the ``read`` scope, changing one
with ``admin``.

The filters of a push source (:mod:`memini.filters`) are set with ``source add --kind push`` and
changed with ``source edit``: each filter option there replaces that one rule, the others stay
(the command reads the source first, then sends every rule, as the API takes them).
``source prune`` applies the current rules to the records the source stored before.
"""

import argparse
import time
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final, get_args

from memini.cli import admin
from memini.cli.arguments import (
    Commands,
    format_duration,
    parse_duration,
    parse_schedule,
    parse_source_name,
    parse_workspace_name,
)
from memini.cli.client import Api, Json, segment
from memini.cli.common import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CommandError, message, print_table
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
from memini.sources import MAX_RETENTION_DAYS, LlmPolicy, Visibility

__all__ = ["FILTER_LISTS", "POLL_INTERVAL", "add_commands", "source_path", "sync_command"]

POLL_INTERVAL: Final = 1.0
"""Seconds between two looks at the streams while ``sync --wait`` waits."""

DEFAULT_WAIT: Final = timedelta(minutes=10)

FILTER_LISTS: Final = (
    ("allow_channel", "allow_channels", "GLOB", "store only threads posted in a matching channel"),
    (
        "allow_domain",
        "allow_domains",
        "DOMAIN",
        "keep only messages from or to an address in this domain or below it",
    ),
    ("exclude_author", "exclude_authors", "GLOB", "leave out messages by a matching author"),
    (
        "exclude_title",
        "exclude_titles",
        "GLOB",
        "leave out meetings, documents and threads with a matching title",
    ),
)
"""The list rules of a push source's filters: option, rule, metavar, what it does."""


def source_path(workspace: str, source: str | None = None) -> str:
    """The API path of a workspace's sources, or of one source."""
    path = f"v1/workspaces/{segment(workspace)}/sources"
    return path if source is None else f"{path}/{segment(source)}"


def _retention_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        days = 0
    if not 1 <= days <= MAX_RETENTION_DAYS:
        raise argparse.ArgumentTypeError(f"a number of days from 1 to {MAX_RETENTION_DAYS}")
    return days


# --- workspace -----------------------------------------------------------------------------------


def create_workspace_command(args: argparse.Namespace, api: Api) -> int:
    name: str = args.name
    created = api.post("v1/workspaces", {"name": name})
    print(f"created workspace {text(created.get('name'))} ({text(created.get('id'))})")
    return EXIT_OK


def list_workspaces_command(args: argparse.Namespace, api: Api) -> int:
    body = api.get("v1/workspaces")
    if args.json:
        print_json(body)
        return EXIT_OK
    found = [as_mapping(w) for w in as_list(body.get("workspaces"))]
    if not found:
        message("no workspaces yet; create one with `memini workspace create <name>`")
        return EXIT_OK
    print_table(
        ("NAME", "CREATED", "ID"),
        ((text(w.get("name")), when(w.get("created_at")), text(w.get("id"))) for w in found),
    )
    return EXIT_OK


# --- source --------------------------------------------------------------------------------------


def _schedule(value: object) -> str:
    return "when triggered" if value is None else f"every {seconds(value)}"


def _print_source(source: Mapping[str, Any]) -> None:
    retention = source.get("retention_days")
    print_fields(
        [
            ("name", text(source.get("name"))),
            ("workspace", text(source.get("workspace"))),
            ("id", text(source.get("id"))),
            ("kind", text(source.get("kind"))),
            ("location", text(source.get("location"))),
            ("refs", text(source.get("refs"))),
            ("schedule", _schedule(source.get("schedule_seconds"))),
            ("include", text(source.get("include"), "every file")),
            ("exclude", text(source.get("exclude"))),
            ("builtin excludes", text(source.get("builtin_excludes"))),
            ("visibility", text(source.get("visibility"))),
            ("retention", "keep records" if retention is None else f"{text(retention)} days"),
            ("llm policy", text(source.get("llm_policy"))),
            *_filter_fields(source.get("filters")),
            ("created", when(source.get("created_at"))),
            ("updated", when(source.get("updated_at"))),
        ]
    )


def _filter_fields(value: object) -> list[tuple[str, str]]:
    """The filter rules of a push source, one line each; none for a git source."""
    filters = as_mapping(value)
    if not isinstance(value, dict):  # null for a source of another kind than push
        return []
    fields = [
        (rule.replace("_", " "), text(filters.get(rule), "any")) for _, rule, _, _ in FILTER_LISTS
    ]
    fields.append(("exclude automated", text(filters.get("exclude_automated"), "no")))
    return fields


def _filters_given(args: argparse.Namespace) -> bool:
    return any(getattr(args, option) is not None for option, _, _, _ in FILTER_LISTS) or bool(
        getattr(args, "exclude_automated", None)
    )


def _filters(args: argparse.Namespace, current: Mapping[str, Any]) -> Json:
    """The filter rules to send: ``current`` with the rules the options give replaced."""
    rules: Json = {rule: list(as_list(current.get(rule))) for _, rule, _, _ in FILTER_LISTS}
    rules["exclude_automated"] = current.get("exclude_automated") is True
    for option, rule, _, _ in FILTER_LISTS:
        if getattr(args, f"no_{rule}", False):
            rules[rule] = []
        elif getattr(args, option) is not None:
            rules[rule] = getattr(args, option)
    if args.exclude_automated is not None:
        rules["exclude_automated"] = args.exclude_automated
    return rules


_GIT_ONLY: Final = ("location", "ref", "schedule", "include", "exclude", "no_builtin_excludes")
_PUSH_ONLY: Final = (*(option for option, _, _, _ in FILTER_LISTS), "exclude_automated")


def _option(name: str) -> str:
    return "--" + name.replace("_", "-")


def add_source_command(args: argparse.Namespace, api: Api) -> int:
    kind: str = args.kind
    workspace: str = args.workspace
    schedule: timedelta | None = args.schedule
    body: Json = {"kind": kind, "name": args.name}
    if kind == "git":
        missing = [_option(name) for name in ("location", "ref") if not getattr(args, name)]
        if missing:
            raise CommandError(
                f"a git source needs {' and '.join(missing)}; see `memini source add --help`",
                EXIT_USAGE,
            )
        given = [_option(name) for name in _PUSH_ONLY if getattr(args, name)]
        if given:
            raise CommandError(
                f"{', '.join(given)}: only for a push source; a git source selects files with"
                " --include and --exclude",
                EXIT_USAGE,
            )
        body |= {
            "location": args.location,
            "refs": args.ref,
            "schedule_seconds": None if schedule is None else int(schedule.total_seconds()),
            "include": args.include or [],
            "exclude": args.exclude or [],
            "builtin_excludes": not args.no_builtin_excludes,
        }
    else:
        given = [_option(name) for name in _GIT_ONLY if getattr(args, name)]
        if given:
            raise CommandError(
                f"{', '.join(given)}: only for a git source; a push source takes its records at"
                " POST …/sources/<name>/records",
                EXIT_USAGE,
            )
        if _filters_given(args):
            body["filters"] = {rule: value for rule, value in _filters(args, {}).items() if value}
    for option in ("visibility", "retention_days", "llm_policy"):
        value = getattr(args, option)
        if value is not None:
            body[option] = value
    source = api.post(source_path(workspace), body)
    message(f"added source {text(source.get('name'))} to workspace {workspace}")
    _print_source(source)
    if kind == "push":
        message(
            f"clients send its records to POST /v1/workspaces/{workspace}/sources/{args.name}"
            "/records, with a token with the ingest scope"
        )
    elif schedule is None:
        message(f"it syncs when triggered: `memini sync {workspace} {args.name}`")
    return EXIT_OK


def list_sources_command(args: argparse.Namespace, api: Api) -> int:
    workspace: str = args.workspace
    body = api.get(source_path(workspace))
    if args.json:
        print_json(body)
        return EXIT_OK
    found = [as_mapping(s) for s in as_list(body.get("sources"))]
    if not found:
        message(f"workspace {workspace} has no sources; add one with `memini source add`")
        return EXIT_OK
    print_table(
        ("NAME", "KIND", "REFS", "SCHEDULE", "LOCATION"),
        (
            (
                text(s.get("name")),
                text(s.get("kind")),
                text(s.get("refs")),
                _schedule(s.get("schedule_seconds")),
                text(s.get("location")),
            )
            for s in found
        ),
    )
    return EXIT_OK


def show_source_command(args: argparse.Namespace, api: Api) -> int:
    source = api.get(source_path(args.workspace, args.source))
    if args.json:
        print_json(source)
    else:
        _print_source(source)
    return EXIT_OK


def edit_source_command(args: argparse.Namespace, api: Api) -> int:
    changes: Json = {}
    if args.name is not None:
        changes["name"] = args.name
    if args.ref is not None:
        changes["refs"] = args.ref
    if args.no_schedule:
        changes["schedule_seconds"] = None
    elif args.schedule is not None:
        changes["schedule_seconds"] = int(args.schedule.total_seconds())
    for option in ("include", "exclude"):
        if getattr(args, f"no_{option}"):
            changes[option] = []
        elif getattr(args, option) is not None:
            changes[option] = getattr(args, option)
    if args.builtin_excludes is not None:
        changes["builtin_excludes"] = args.builtin_excludes
    if args.no_retention:
        changes["retention_days"] = None
    elif args.retention_days is not None:
        changes["retention_days"] = args.retention_days
    for option in ("visibility", "llm_policy"):
        if getattr(args, option) is not None:
            changes[option] = getattr(args, option)
    if args.no_filters:
        changes["filters"] = {}
    elif _filters_given(args) or any(getattr(args, f"no_{rule}") for _, rule, _, _ in FILTER_LISTS):
        # The API takes every rule at once: the ones not given here stay as they are.
        current = api.get(source_path(args.workspace, args.source))
        changes["filters"] = _filters(args, as_mapping(current.get("filters")))
    if not changes:
        raise CommandError("nothing to change; see `memini source edit --help`", EXIT_USAGE)
    source = api.patch(source_path(args.workspace, args.source), changes)
    message(f"changed source {text(source.get('name'))} of workspace {args.workspace}")
    _print_source(source)
    return EXIT_OK


def prune_source_command(args: argparse.Namespace, api: Api) -> int:
    """Apply a push source's filters to its stored records and print what that did."""
    workspace: str = args.workspace
    source: str = args.source
    body = api.post(f"{source_path(workspace, source)}/prune")
    if args.json:
        print_json(body)
        return EXIT_OK
    if body.get("rules") is False:
        message(f"source {source} has no filters: nothing to prune")
    counts = as_mapping(body.get("counts"))
    print_fields(
        [
            ("records read", text(counts.get("scanned"))),
            ("deleted", text(counts.get("deleted"))),
            ("trimmed", text(counts.get("trimmed"))),
            ("messages removed", text(counts.get("messages_removed"))),
            ("unchanged", text(counts.get("unchanged"))),
            ("unreadable", text(counts.get("unreadable"))),
            ("chunks deleted", text(body.get("chunks_deleted"))),
            ("chunks written", text(body.get("chunks_written"))),
            ("run", text(body.get("run_id"))),
        ]
    )
    not_indexed = body.get("records_not_indexed")
    if isinstance(not_indexed, int) and not_indexed > 0:
        message(
            f"{not_indexed} trimmed records lost their chunks and are not searchable until"
            " `memini reindex` indexes them"
        )
    return EXIT_OK


def remove_source_command(args: argparse.Namespace, api: Api) -> int:
    api.delete(source_path(args.workspace, args.source))
    print(
        f"removed source {args.source} of workspace {args.workspace}, with its streams, runs,"
        " records and mirror"
    )
    return EXIT_OK


# --- streams and sync ----------------------------------------------------------------------------


def _last_run(run: Mapping[str, Any]) -> str:
    if not run:
        return "never ran"
    return f"{text(run.get('status'))} {when(run.get('started_at'))}"


def _pending(job: Mapping[str, Any]) -> str:
    if not job:
        return NONE
    status = text(job.get("status"))
    attempts = job.get("attempts")
    if isinstance(attempts, int) and attempts > 1:
        status += f" (attempt {attempts})"
    return status


def streams_command(args: argparse.Namespace, api: Api) -> int:
    body = api.get(f"{source_path(args.workspace, args.source)}/streams")
    if args.json:
        print_json(body)
        return EXIT_OK
    found = [as_mapping(s) for s in as_list(body.get("streams"))]
    print_table(
        ("STREAM", "SCHEDULE", "CHECKED UNTIL", "LAG", "CURRENT", "LAST RUN", "SYNC", "RUN ID"),
        (
            (
                text(s.get("key")) + (" (missing)" if s.get("missing") is True else ""),
                _schedule(s.get("schedule_seconds")),
                when(s.get("checked_until")),
                seconds(s.get("lag_seconds")),
                text(s.get("current")),
                _last_run(as_mapping(s.get("last_run"))),
                _pending(as_mapping(s.get("pending_sync"))),
                text(as_mapping(s.get("last_run")).get("id")),
            )
            for s in found
        ),
    )
    return EXIT_OK


def sync_command(args: argparse.Namespace, api: Api) -> int:
    """Enqueue a sync of a source's streams; with ``--wait``, wait for the runs and report them."""
    workspace: str = args.workspace
    source: str = args.source
    streams: list[str] | None = args.stream
    accepted = api.post(
        f"{source_path(workspace, source)}/sync", None if streams is None else {"streams": streams}
    )
    jobs = [as_mapping(job) for job in as_list(accepted.get("jobs"))]
    for job in jobs:
        state = "queued" if job.get("created") else "already queued or running"
        print(f"{text(job.get('stream'))}: sync {state}, job {text(job.get('job_id'))}")
    if not args.wait:
        message(
            f"follow it with `memini streams {workspace} {source}` and"
            f" `memini runs {workspace} --source {source}`"
        )
        return EXIT_OK
    waiting = {str(job.get("stream")): str(job.get("job_id")) for job in jobs}
    finished = _wait_for_syncs(api, workspace, source, waiting, args.wait_timeout)
    failed = False
    for key, run in finished.items():
        if not run:
            print(f"{text(key)}: no run")
            failed = True
            continue
        status = text(run.get("status"))
        error = run.get("error")
        print(
            f"{text(key)}: run {text(run.get('id'))} {status}"
            + ("" if error is None else f": {text(error)}")
        )
        failed = failed or status != "succeeded"
    message(f"the report of a run: `memini run <run id>`; all runs: `memini runs {workspace}`")
    return EXIT_FAILURE if failed else EXIT_OK


def _wait_for_syncs(
    api: Api, workspace: str, source: str, waiting: dict[str, str], timeout: timedelta
) -> dict[str, Mapping[str, Any]]:
    """Wait until no stream in ``waiting`` (key to job id) has that job queued or running; return
    the newest run of each, ``{}`` for a stream that has none."""
    deadline = time.monotonic() + timeout.total_seconds()
    finished: dict[str, Mapping[str, Any]] = {}
    path = f"{source_path(workspace, source)}/streams"
    reported_attempts: dict[str, int] = {}
    while True:
        streams = {
            str(s.get("key")): s
            for s in (as_mapping(item) for item in as_list(api.get(path).get("streams")))
        }
        for key, job_id in list(waiting.items()):
            stream = as_mapping(streams.get(key))
            pending = as_mapping(stream.get("pending_sync"))
            if pending and str(pending.get("id")) == job_id:
                attempts = pending.get("attempts")
                last_error = pending.get("last_error")
                if (
                    isinstance(attempts, int)
                    and last_error is not None
                    and attempts > reported_attempts.get(key, 0)
                    and pending.get("status") == "queued"
                ):
                    reported_attempts[key] = attempts
                    message(f"{text(key)}: attempt {attempts} failed, retried: {text(last_error)}")
                continue
            finished[key] = as_mapping(stream.get("last_run"))
            del waiting[key]
        if not waiting:
            return finished
        if time.monotonic() >= deadline:
            keys = ", ".join(text(key) for key in waiting)
            raise CommandError(
                f"still syncing after {format_duration(timeout.total_seconds())}: {keys}; see"
                f" `memini streams {workspace} {source}`"
            )
        time.sleep(POLL_INTERVAL)


# --- Parsers -------------------------------------------------------------------------------------


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the API's JSON answer")


def _workspace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    _workspace_argument(parser)
    parser.add_argument("source", type=parse_source_name, help="the source's name")


def _settings_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--visibility",
        choices=get_args(Visibility),
        help="private (the default): only the owner reads it; workspace: everyone in the workspace",
    )
    parser.add_argument(
        "--llm-policy",
        choices=get_args(LlmPolicy),
        help="which LLM may read its content: none, local (the default) or cloud",
    )


def _filter_options(parser: argparse.ArgumentParser, *, edit: bool) -> None:
    """The filter options of ``source add`` and, with ``edit``, of ``source edit``."""
    for option, rule, metavar, does in FILTER_LISTS:
        if not edit:
            parser.add_argument(
                _option(option),
                action="append",
                metavar=metavar,
                help=f"push: {does}; repeat for more",
            )
            continue
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            _option(option),
            action="append",
            metavar=metavar,
            help=f"{does}; repeat for more; replaces this rule",
        )
        group.add_argument(_option(f"no_{rule}"), action="store_true", help=f"clear {rule}")
    if not edit:
        parser.add_argument(
            "--exclude-automated",
            action="store_const",
            const=True,
            help="push: leave out automated mail (no-reply senders, Auto-Submitted, bulk)",
        )
        return
    automated = parser.add_mutually_exclusive_group()
    automated.add_argument(
        "--exclude-automated",
        dest="exclude_automated",
        action="store_const",
        const=True,
        help="leave out automated mail (no-reply senders, Auto-Submitted, bulk)",
    )
    automated.add_argument(
        "--no-exclude-automated",
        dest="exclude_automated",
        action="store_const",
        const=False,
        help="keep automated mail",
    )
    parser.add_argument("--no-filters", action="store_true", help="clear every filter rule")


def add_commands(commands: Commands) -> None:
    """Add ``workspace``, ``source``, ``streams`` and ``sync``."""
    workspace = commands.add_client(
        "workspace",
        help="create and list workspaces",
        description="Create and list workspaces. `workspace llm` is an operator command: it runs"
        " inside the service's container.",
    )
    workspace_commands = workspace.add_subparsers(metavar="<command>", required=True)
    create = workspace_commands.add_parser(
        "create", help="create a workspace (admin)", description="Create a workspace."
    )
    create.add_argument(
        "name",
        type=parse_workspace_name,
        help="1 to 63 lowercase letters, digits, '-' or '_', starting with a letter or digit",
    )
    create.set_defaults(client=create_workspace_command)
    listing = workspace_commands.add_parser(
        "list", help="list workspaces", description="List the workspaces the token may read."
    )
    _json_option(listing)
    listing.set_defaults(client=list_workspaces_command)
    # An operator command in a group of clients: no token, an admin one included, may opt a
    # workspace in to a cloud LLM, so the API has no route for it (principle 8).
    llm = commands.add_operator_subcommand(
        workspace_commands.add_parser,
        "workspace",
        "llm",
        help=admin.WORKSPACE_LLM_HELP,
        description=admin.WORKSPACE_LLM_DESCRIPTION,
    )
    admin.configure_workspace_llm(llm)

    source = commands.add_client("source", help="add, list, show, change and remove sources")
    source_commands = source.add_subparsers(metavar="<command>", required=True)
    add = source_commands.add_parser(
        "add",
        help="add a git repository or a push source (admin)",
        description="Add a source to a workspace. A git repository (the default kind) needs"
        " --location and --ref, and gets one stream per ref; the service checks the location at"
        " once and fetches it at the first sync. A push source (--kind push) takes batches of"
        " records that clients send it, and has none of the git options; its filters say which"
        " records and messages it stores. Patterns (GLOB) are matched against the whole value,"
        " ignoring case: * any characters, ? one, [abc] one of a set.",
    )
    _workspace_argument(add)
    add.add_argument("name", type=parse_source_name, help="the source's name in the workspace")
    add.add_argument(
        "--kind", choices=("git", "push"), default="git", help="the kind of source (default: git)"
    )
    add.add_argument(
        "--location",
        metavar="URL_OR_PATH",
        help="git: an https:// or ssh:// URL, git@host:path, or a local path the service allows;"
        " never with a password: the service holds the credentials",
    )
    add.add_argument(
        "--ref",
        action="append",
        metavar="REF",
        help="git: a branch (main), refs/heads/<branch> or refs/tags/<tag> to track; repeat for"
        " more",
    )
    add.add_argument(
        "--schedule",
        type=parse_schedule,
        metavar="DURATION",
        help="sync this often, 1m to 366d, e.g. 1h; default: only when triggered",
    )
    add.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="index only paths that match, e.g. 'src/**'; repeat for more; default: every file",
    )
    add.add_argument(
        "--exclude", action="append", metavar="GLOB", help="skip paths that match; repeat for more"
    )
    add.add_argument(
        "--no-builtin-excludes",
        action="store_true",
        help="index lock files, minified bundles, source maps and node_modules too",
    )
    add.add_argument(
        "--retention-days",
        type=_retention_days,
        metavar="DAYS",
        help="delete records older than this; default: keep them",
    )
    _settings_options(add)
    _filter_options(add, edit=False)
    add.set_defaults(client=add_source_command)

    list_sources = source_commands.add_parser(
        "list", help="list the sources of a workspace", description="List a workspace's sources."
    )
    _workspace_argument(list_sources)
    _json_option(list_sources)
    list_sources.set_defaults(client=list_sources_command)

    show = source_commands.add_parser(
        "show", help="show a source", description="Show a source's settings."
    )
    _source_arguments(show)
    _json_option(show)
    show.set_defaults(client=show_source_command)

    edit = source_commands.add_parser(
        "edit",
        help="change a source's settings (admin)",
        description="Change a source's settings; what is not given stays. --ref, --include and"
        " --exclude replace the whole list; streams of refs left out are deleted with their runs."
        " A new schedule starts now. The location cannot change: add another source instead. A"
        " filter option of a push source replaces that rule; the new rules apply to the batches"
        " after the change, and `memini source prune` applies them to the records stored before.",
    )
    _source_arguments(edit)
    edit.add_argument("--name", type=parse_source_name, help="rename the source")
    edit.add_argument(
        "--ref", action="append", metavar="REF", help="the refs to track; repeat for more"
    )
    schedule = edit.add_mutually_exclusive_group()
    schedule.add_argument(
        "--schedule", type=parse_schedule, metavar="DURATION", help="sync this often, 1m to 366d"
    )
    schedule.add_argument("--no-schedule", action="store_true", help="sync only when triggered")
    for option, does in (("include", "index only paths that match"), ("exclude", "skip paths")):
        group = edit.add_mutually_exclusive_group()
        group.add_argument(
            f"--{option}", action="append", metavar="GLOB", help=f"{does}; repeat for more"
        )
        group.add_argument(f"--no-{option}", action="store_true", help=f"no {option} patterns")
    builtin = edit.add_mutually_exclusive_group()
    builtin.add_argument(
        "--builtin-excludes",
        dest="builtin_excludes",
        action="store_const",
        const=True,
        help="leave out lock files, minified bundles, source maps and node_modules (the default)",
    )
    builtin.add_argument(
        "--no-builtin-excludes",
        dest="builtin_excludes",
        action="store_const",
        const=False,
        help="index them too",
    )
    retention = edit.add_mutually_exclusive_group()
    retention.add_argument(
        "--retention-days",
        type=_retention_days,
        metavar="DAYS",
        help="delete records older than this",
    )
    retention.add_argument("--no-retention", action="store_true", help="keep records")
    _settings_options(edit)
    _filter_options(edit, edit=True)
    edit.set_defaults(client=edit_source_command)

    prune = source_commands.add_parser(
        "prune",
        help="apply a push source's filters to its stored records (admin)",
        description="Apply the current filters of a push source to the records it stored before"
        " they changed: a record they exclude is deleted with its chunks, a thread loses the"
        " messages they leave out and is indexed again, the rest stays. Running it again changes"
        " nothing.",
    )
    _source_arguments(prune)
    _json_option(prune)
    prune.set_defaults(client=prune_source_command)

    remove = source_commands.add_parser(
        "remove",
        help="remove a source with its streams, runs and records (admin)",
        description="Remove a source with its streams, runs, jobs and records, and its mirror."
        " Refused while a job of the source runs; try again when it is done.",
    )
    _source_arguments(remove)
    remove.set_defaults(client=remove_source_command)

    streams = commands.add_client(
        "streams",
        help="list a source's streams with lag, last run and queued sync",
        description="List the streams of a source (for git, its tracked refs) with how far they"
        " are synced, their newest run and their queued or running sync.",
    )
    _source_arguments(streams)
    _json_option(streams)
    streams.set_defaults(client=streams_command)

    sync = commands.add_client(
        "sync",
        help="sync a source now (admin)",
        description="Enqueue a sync of every stream of a source, or of the streams given. A"
        " stream whose sync is queued or running already gets no second one. With --wait, wait"
        " for the syncs to finish and print each run; the exit code is 1 if one did not succeed.",
    )
    _source_arguments(sync)
    sync.add_argument(
        "--stream",
        action="append",
        metavar="KEY",
        help="only this stream, by key (refs/heads/main); repeat for more",
    )
    sync.add_argument("--wait", action="store_true", help="wait for the syncs and report the runs")
    default_wait = format_duration(DEFAULT_WAIT.total_seconds())
    sync.add_argument(
        "--wait-timeout",
        type=parse_duration,
        default=DEFAULT_WAIT,
        metavar="DURATION",
        help=f"give up waiting after this long (default {default_wait})",
    )
    sync.set_defaults(client=sync_command)
