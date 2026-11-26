"""``memini search`` and ``memini search-code``: thin clients of ``GET /v1/search`` and
``GET /v1/search/code``, the MCP tools ``search`` and ``search_code``.

The options are the tools' arguments (:mod:`memini.cli.reading`); the service checks them. Each
hit prints as its locator, kind, place and time, its link when it has one, then the first lines of
its snippet. The snippet is the source's text: data, printed with control characters escaped.
``--json`` prints the API's answer as it is. Needs the ``read`` scope; useful to check an install
end to end.
"""

import argparse
from typing import Final, get_args

from memini.chunking import Language
from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api, Json
from memini.cli.common import EXIT_OK, message, printable
from memini.cli.decisions import print_item
from memini.cli.output import as_list, as_mapping, print_json, text, when
from memini.cli.reading import (
    add_filter,
    add_include_sensitive,
    add_json,
    add_limit,
    add_time_window,
    left_out,
    time_params,
)
from memini.ids import RecordKind
from memini.knowledge import ITEM_KINDS
from memini.persons import Role
from memini.query.model import CodeMode, SearchMode
from memini.search.model import DEFAULT_LIMIT, MAX_LIMIT

__all__ = ["SNIPPET_LINES", "add_commands", "search_code_command", "search_command"]

SNIPPET_LINES: Final = 6
"""Lines of a hit's snippet printed; the rest is cut, ``--json`` has all of it."""


def search_command(args: argparse.Namespace, api: Api) -> int:
    params: dict[str, str | int | list[str] | None] = {
        "query": args.query,
        "limit": args.limit,
        "mode": args.mode,
        "workspaces": args.workspaces,
        "sources": args.sources,
        "kinds": args.kinds,
        "persons": args.persons,
        "roles": args.roles,
        "projects": args.projects,
        "domains": args.domains,
        "services": args.services,
        "include_sensitive": "true" if args.include_sensitive else None,
        **time_params(args),
    }
    return _show(args, api.get("v1/search", params))


def search_code_command(args: argparse.Namespace, api: Api) -> int:
    params: dict[str, str | int | list[str] | None] = {
        "query": args.query,
        "limit": args.limit,
        "mode": args.mode,
        "workspaces": args.workspaces,
        "sources": args.sources,
        "paths": args.paths,
        "languages": args.languages,
        "projects": args.projects,
        "domains": args.domains,
        "services": args.services,
        "case_sensitive": None if args.case_sensitive else "false",
        "include_sensitive": "true" if args.include_sensitive else None,
    }
    return _show(args, api.get("v1/search/code", params))


def _show(args: argparse.Namespace, body: Json) -> int:
    if args.json:
        print_json(body)
        return EXIT_OK
    hits = [as_mapping(hit) for hit in as_list(body.get("items"))]
    decisions = as_list(body.get("decisions"))
    left_out(body, "hits of records")
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


def _scope(parser: argparse.ArgumentParser, what: str) -> None:
    add_filter(parser, "workspaces", "only this workspace", type=parse_workspace_name)
    add_filter(parser, "sources", f"only this source{what}", type=parse_source_name)
    add_filter(parser, "projects", "only results of this project of the catalog")
    add_filter(parser, "domains", "only results of this domain (domain or domain/subdomain)")
    add_filter(parser, "services", "only code of this service of the catalog")


def add_commands(commands: Commands) -> None:
    """Add ``search`` and ``search-code``."""
    search = commands.add_client(
        "search",
        help="search everything: code, messages, meetings, documents, decisions",
        description="Search the records the token may read, best match first: the MCP tool"
        " search. Modes: hybrid (default), dense, lexical.",
    )
    search.add_argument("query", help="a question or keywords")
    search.add_argument("--mode", choices=get_args(SearchMode), help="how to search")
    _scope(search, "")
    add_filter(
        search,
        "kinds",
        "only this kind of record or knowledge item",
        choices=(*get_args(RecordKind), *ITEM_KINDS),
    )
    add_filter(
        search,
        "persons",
        "only records that name this person: me, a person id or an alias",
        metavar="PERSON",
    )
    add_filter(
        search, "roles", "with --persons: what the person is in the record", choices=get_args(Role)
    )
    add_time_window(search, "records")
    add_include_sensitive(search)
    add_limit(search, DEFAULT_LIMIT, MAX_LIMIT)
    add_json(search)
    search.set_defaults(client=search_command)

    code = commands.add_client(
        "search-code",
        help="search code: by meaning, text, regex or symbol name",
        description="Search the code of the repositories the token may read: the MCP tool"
        " search_code. Modes: semantic (default), exact, regex, symbol (whole names) and"
        " symbol_prefix (where a symbol is defined).",
    )
    code.add_argument("query", help="a question, a substring, a regular expression or a name")
    code.add_argument("--mode", choices=get_args(CodeMode), help="how to search")
    _scope(code, " of kind repository")
    add_filter(code, "paths", "only paths that match, e.g. 'src/**/*.py'", metavar="GLOB")
    add_filter(code, "languages", "only code in this language", choices=get_args(Language))
    code.add_argument(
        "--case-sensitive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="exact and regex: match case (default); --no-case-sensitive ignores it",
    )
    add_include_sensitive(code)
    add_limit(code, DEFAULT_LIMIT, MAX_LIMIT)
    add_json(code)
    code.set_defaults(client=search_code_command)
