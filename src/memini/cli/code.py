"""``memini get-file`` and ``memini list-commits``: thin clients of
``GET /v1/workspaces/{workspace}/sources/{source}/files/{path}`` and ``GET /v1/commits``, the MCP
tools ``get_file`` and ``list_commits``.

The options are the tools' arguments (:mod:`memini.cli.reading`); the service checks them.
``get-file`` prints a file's lines with their numbers, or a directory's entries; ``list-commits``
one commit per paragraph: its locator, time, author, subject and changed paths. File contents,
commit messages and names are data, printed with control characters escaped. ``--json`` prints the
API's answer as it is. Needs the ``read`` scope.
"""

import argparse
from typing import Final
from urllib.parse import quote

from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api, segment
from memini.cli.common import EXIT_OK, message, printable
from memini.cli.output import as_list, as_mapping, print_json, text, when
from memini.cli.reading import (
    add_filter,
    add_include_sensitive,
    add_json,
    add_limit,
    add_time_window,
    add_workspace,
    resolve_workspace,
    time_params,
)

__all__ = ["add_commands", "get_file_command", "list_commits_command"]

COMMITS_DEFAULT_LIMIT: Final = 20
COMMITS_MAX_LIMIT: Final = 1000


def _line_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("a line number is 1 or more")
    return number


def get_file_command(args: argparse.Namespace, api: Api) -> int:
    workspace = resolve_workspace(api, args.workspace)
    path: str = args.path.strip("/")
    route = (
        f"v1/workspaces/{segment(workspace)}/sources/{segment(args.source)}/files/"
        f"{quote(path, safe='/')}"
    )
    params: dict[str, str | int | None] = {
        "ref": args.ref,
        "start_line": args.start_line,
        "end_line": args.end_line,
        "include_sensitive": "true" if args.include_sensitive else None,
    }
    body = api.get(route, params)
    if args.json:
        print_json(body)
        return EXIT_OK
    message(f"{text(body.get('locator'))} ({text(body.get('kind'))})")
    if body.get("entries") is not None:
        for entry in map(as_mapping, as_list(body.get("entries"))):
            suffix = "/" if entry.get("kind") == "directory" else ""
            print(f"{text(entry.get('name'))}{suffix}")
        return EXIT_OK
    content = body.get("content")
    if not isinstance(content, str):
        message("no text to show: a binary file or a link; --json has what the service answered")
        return EXIT_OK
    start = body.get("start_line")
    first = start if isinstance(start, int) and not isinstance(start, bool) else 1
    for number, line in enumerate(content.splitlines(), start=first):
        print(f"{number:>6}  {printable(line.expandtabs(4))}".rstrip())
    if body.get("truncated") is True:
        message("cut at the service's limit; --start-line and --end-line read the rest")
    return EXIT_OK


def list_commits_command(args: argparse.Namespace, api: Api) -> int:
    params: dict[str, str | int | list[str] | None] = {
        "workspaces": args.workspaces,
        "sources": args.sources,
        "paths": args.paths,
        "persons": args.persons,
        "query": args.query,
        **time_params(args),
        "limit": args.limit,
        "cursor": args.cursor,
    }
    body = api.get("v1/commits", params)
    if args.json:
        print_json(body)
        return EXIT_OK
    commits = [as_mapping(commit) for commit in as_list(body.get("items"))]
    if not commits:
        message("no commits")
    for number, commit in enumerate(commits, start=1):
        if number > 1:
            print()
        author = as_mapping(commit.get("author"))
        print(
            f"{text(commit.get('locator'))}  ({when(commit.get('occurred_at'))},"
            f" {text(author.get('name'))} <{text(author.get('email'))}>)"
        )
        print(f"  {text(commit.get('subject'))}")
        for change in map(as_mapping, as_list(commit.get("changes"))):
            print(f"    {text(change.get('status'))}  {text(change.get('path'))}")
        total = commit.get("changes_total")
        shown = len(as_list(commit.get("changes")))
        if isinstance(total, int) and total > shown:
            print(f"    … {total - shown} more paths")
    cursor = body.get("next_cursor")
    if isinstance(cursor, str):
        message(f"more commits: --cursor {cursor}")
    return EXIT_OK


def add_commands(commands: Commands) -> None:
    """Add ``get-file`` and ``list-commits``."""
    get_file = commands.add_client(
        "get-file",
        help="a file, a line range or a directory of a repository",
        description="Read a file, a line range of it or a directory listing from the mirror of a"
        " source of kind repository: the MCP tool get_file. By default at the pinned commit of the"
        " default tracked branch.",
    )
    get_file.add_argument("source", type=parse_source_name, help="the repository's source name")
    get_file.add_argument(
        "path", nargs="?", default="", help="relative to the repository root; default: the root"
    )
    add_workspace(get_file, "repository")
    get_file.add_argument(
        "--ref", metavar="REF", help="a branch or tag name, a full ref or a full commit id"
    )
    get_file.add_argument("--start-line", type=_line_number, metavar="N", help="the first line")
    get_file.add_argument("--end-line", type=_line_number, metavar="N", help="the last line")
    add_include_sensitive(get_file)
    add_json(get_file)
    get_file.set_defaults(client=get_file_command)

    commits = commands.add_client(
        "list-commits",
        help="who changed what: the commits of repositories, newest first",
        description="List the commits of the repositories the token may read, newest first,"
        " read from their mirrors: the MCP tool list_commits.",
    )
    add_filter(commits, "workspaces", "only this workspace", type=parse_workspace_name)
    add_filter(commits, "sources", "only this source of kind repository", type=parse_source_name)
    add_filter(commits, "paths", "only commits that changed a matching path", metavar="GLOB")
    add_filter(
        commits,
        "persons",
        "only commits by this person: me, a person id, or text the author's name or address holds",
        metavar="PERSON",
    )
    commits.add_argument(
        "--query", metavar="TEXT", help="only commits whose message holds this text"
    )
    add_time_window(commits, "commits")
    add_limit(commits, COMMITS_DEFAULT_LIMIT, COMMITS_MAX_LIMIT)
    commits.add_argument(
        "--cursor", metavar="CURSOR", help="continue a list: the next_cursor of the last answer"
    )
    add_json(commits)
    commits.set_defaults(client=list_commits_command)
