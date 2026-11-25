"""``memini search``: a thin client of ``GET /v1/search`` and, with ``--code``, ``/v1/search/code``.

The arguments are those of the MCP tools ``search`` and ``search_code``; the service checks them.
Each hit prints as its locator, kind, place and time, its link when it has one, then the first
lines of its snippet. The snippet is the source's text: data, printed with control characters
escaped. ``--json`` prints the API's answer as it is. Needs the ``read`` scope; useful to check an
install end to end.
"""

import argparse
from typing import Final, get_args

from memini.chunking import Language
from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api
from memini.cli.common import EXIT_OK, EXIT_USAGE, CommandError, message, printable
from memini.cli.decisions import print_item
from memini.cli.output import as_list, as_mapping, print_json, text, when
from memini.ids import RecordKind
from memini.knowledge import ITEM_KINDS
from memini.persons import Role
from memini.query.model import CodeMode, SearchMode
from memini.search.model import DEFAULT_LIMIT, MAX_LIMIT

__all__ = ["SNIPPET_LINES", "add_commands", "search_command"]

SNIPPET_LINES: Final = 6
"""Lines of a hit's snippet printed; the rest is cut, ``--json`` has all of it."""

_CODE_ONLY: Final = ("path", "language")
_EVERYTHING_ONLY: Final = ("kind", "person", "role")


def _limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"a number from 1 to {MAX_LIMIT}")
    return limit


def search_command(args: argparse.Namespace, api: Api) -> int:
    code: bool = args.code
    mode: str | None = args.mode
    modes = get_args(CodeMode) if code else get_args(SearchMode)
    other = "search" if code else "search --code"
    if mode is not None and mode not in modes:
        raise CommandError(f"--mode {mode} is for {other}; here: {', '.join(modes)}", EXIT_USAGE)
    for option in _EVERYTHING_ONLY if code else _CODE_ONLY:
        if getattr(args, option):
            raise CommandError(f"--{option} is for {other} only", EXIT_USAGE)
    params: dict[str, str | int | list[str] | None] = {
        "query": args.query,
        "limit": args.limit,
        "mode": mode,
        "workspaces": args.workspace,
        "sources": args.source,
        "projects": args.project,
        "domains": args.domain,
        "services": args.service,
        "include_sensitive": "true" if args.include_sensitive else None,
    }
    if code:
        params |= {"paths": args.path, "languages": args.language}
    else:
        params |= {"kinds": args.kind, "persons": args.person, "roles": args.role}
    body = api.get("v1/search/code" if code else "v1/search", params)
    if args.json:
        print_json(body)
        return EXIT_OK
    hits = [as_mapping(hit) for hit in as_list(body.get("items"))]
    decisions = as_list(body.get("decisions"))
    left_out = body.get("left_out")
    if isinstance(left_out, int) and left_out:
        message(
            f"{left_out} hits of records marked sensitive left out; --include-sensitive reads"
            " them, with a token that has the sensitive scope"
        )
    if not hits and not decisions:
        message("no hits")
        return EXIT_OK
    for number, hit in enumerate(hits, start=1):
        if number > 1:
            print()
        place = f"{text(hit.get('workspace'))}/{text(hit.get('source'))}"
        print(
            f"{text(hit.get('locator'))}  ({text(hit.get('kind'))}, {place},"
            f" {when(hit.get('occurred_at'))})"
        )
        link = hit.get("link")
        if isinstance(link, str):
            print(f"  link: {text(link)}")
        snippet = hit.get("snippet")
        lines = snippet.splitlines() if isinstance(snippet, str) else []
        for line in lines[:SNIPPET_LINES]:
            print(f"    {printable(line.expandtabs(4))}".rstrip())
        if len(lines) > SNIPPET_LINES or hit.get("truncated") is True:
            print("    …")
    for item in decisions:
        print_item(as_mapping(item))
    return EXIT_OK


def add_commands(commands: Commands) -> None:
    """Add ``search``."""
    search = commands.add_client(
        "search",
        help="search everything, or code with --code",
        description="Search the records the token may read, as the MCP tools search and"
        " search_code do. Modes: hybrid (default), dense, lexical; with --code: semantic"
        " (default), exact, regex, symbol, symbol_prefix.",
    )
    search.add_argument("query", help="a question or keywords; with --code also a name or regex")
    search.add_argument("--code", action="store_true", help="search code (search_code)")
    search.add_argument(
        "--include-sensitive",
        action="store_true",
        help="also records marked sensitive; needs a token with the sensitive scope",
    )
    search.add_argument(
        "--mode", choices=(*get_args(SearchMode), *get_args(CodeMode)), help="how to search"
    )
    search.add_argument(
        "--workspace",
        action="append",
        type=parse_workspace_name,
        metavar="NAME",
        help="only this workspace; repeat for more",
    )
    search.add_argument(
        "--source",
        action="append",
        type=parse_source_name,
        metavar="NAME",
        help="only this source; with --code a repository; repeat for more",
    )
    search.add_argument(
        "--project",
        action="append",
        metavar="NAME",
        help="only results of this project of the catalog; repeat for more",
    )
    search.add_argument(
        "--domain",
        action="append",
        metavar="NAME",
        help="only results of this domain (domain or domain/subdomain); repeat for more",
    )
    search.add_argument(
        "--service",
        action="append",
        metavar="NAME",
        help="only code of this service of the catalog; repeat for more",
    )
    search.add_argument(
        "--person",
        action="append",
        metavar="PERSON",
        help="only records that name this person: me, a person id or an alias; repeat for more"
        " (not with --code)",
    )
    search.add_argument(
        "--role",
        action="append",
        choices=get_args(Role),
        help="with --person: what the person is in the record; repeat for more (not with --code)",
    )
    search.add_argument(
        "--kind",
        action="append",
        choices=(*get_args(RecordKind), *ITEM_KINDS),
        help="only this kind of record; repeat for more (not with --code)",
    )
    search.add_argument(
        "--path",
        action="append",
        metavar="GLOB",
        help="only paths that match, e.g. 'src/**/*.py'; repeat for more (--code only)",
    )
    search.add_argument(
        "--language",
        action="append",
        choices=get_args(Language),
        help="only code in this language; repeat for more (--code only)",
    )
    search.add_argument(
        "--limit",
        type=_limit,
        default=DEFAULT_LIMIT,
        help=f"the most hits, 1 to {MAX_LIMIT} (default {DEFAULT_LIMIT})",
    )
    search.add_argument("--json", action="store_true", help="print the API's JSON answer")
    search.set_defaults(client=search_command)
