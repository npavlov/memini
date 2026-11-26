"""Client commands for persons: ``memini person …``, through ``/v1/workspaces/{w}/persons``.

::

    memini person list <workspace> [--query TEXT] [--merged] [--limit N] [--cursor CURSOR]
    memini person show <workspace> <person-id>
    memini person lookup <workspace> {email,chat_user_id,display_name,git_author} <value>
    memini person add <workspace> <display-name> [--alias KIND=VALUE ...]
    memini person rename <workspace> <person-id> <display-name>
    memini person alias add <workspace> <person-id> <kind> <value>
    memini person alias remove <workspace> <person-id> <alias-id>
    memini person merge <workspace> <person-id> --into <person-id>
    memini person split <workspace> <person-id>
    memini person me <workspace> [--set <person-id> | --unset]

Reads (``list``, ``show``, ``lookup``, ``me`` without an option) need the ``read`` scope and take
``--json`` for the API's answer as it is; changes need ``admin``. The rules are the service's
(:mod:`memini.persons`): a merge marks the person merged and moves nothing, so ``split`` undoes it;
adding an alias that another person holds merges that person into this one, and the command says
so and how to undo it.
"""

import argparse
from collections.abc import Mapping
from typing import Any, Final
from uuid import UUID

from memini.cli.arguments import parse_workspace_name
from memini.cli.client import Api, Json, segment
from memini.cli.common import EXIT_OK, message, print_table
from memini.cli.output import as_list, as_mapping, print_fields, print_json, text, when
from memini.persons import ALIAS_KINDS, DEFAULT_PERSON_PAGE, MAX_PERSON_PAGE

__all__ = ["DESCRIPTION", "HELP", "configure_parser"]

HELP: Final = "list and find persons, edit their aliases, merge, split, set me"
DESCRIPTION: Final = (
    "Persons of a workspace and their aliases (mail addresses, chat user ids, display names, git"
    " authors). Ingest resolves the people of each record by their mail address and chat user id"
    " first; a name without either stays a person of its own until it is merged. A merge marks"
    " the person merged and changes nothing else, so a split restores it. Reads need the read"
    " scope, changes the admin scope."
)


def _persons(workspace: str, *rest: str) -> str:
    return "/".join(("v1", "workspaces", segment(workspace), "persons", *rest))


def _limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= MAX_PERSON_PAGE:
        raise argparse.ArgumentTypeError(f"a number from 1 to {MAX_PERSON_PAGE}")
    return limit


def _person_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("a person id, as `person list` shows it") from None


def _alias_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("an alias id, as `person show` shows it") from None


def _alias(value: str) -> tuple[str, str]:
    kind, separator, alias = value.partition("=")
    if not separator or kind not in ALIAS_KINDS or not alias.strip():
        raise argparse.ArgumentTypeError(
            f"KIND=VALUE, the kind one of {', '.join(ALIAS_KINDS)}: email=ann@example.test"
        )
    return kind, alias


# --- Output --------------------------------------------------------------------------------------


def _aliases_line(person: Mapping[str, Any]) -> str:
    aliases = [as_mapping(alias) for alias in as_list(person.get("aliases"))]
    return ", ".join(f"{text(a.get('kind'))}:{text(a.get('value'))}" for a in aliases) or "-"


def print_person(person: Mapping[str, Any]) -> None:
    """A person's fields, then its aliases and the persons merged into it."""
    person_id = text(person.get("id"))
    canonical = text(person.get("canonical_id"))
    print_fields(
        [
            ("id", person_id),
            ("name", text(person.get("display_name"))),
            ("workspace", text(person.get("workspace"))),
            ("canonical", "this person" if canonical == person_id else canonical),
            ("merged into", text(person.get("merged_into"))),
            ("merged at", when(person.get("merged_at"))),
            ("members", text(person.get("members"))),
            ("me", text(person.get("is_me"))),
            ("created", when(person.get("created_at"))),
        ]
    )
    aliases = [as_mapping(alias) for alias in as_list(person.get("aliases"))]
    if not aliases:
        return
    print()
    print_table(
        ("ALIAS", "KIND", "VALUE", "HELD BY"),
        (
            (
                text(a.get("id")),
                text(a.get("kind")),
                text(a.get("value")),
                "this person"
                if a.get("person_id") == person.get("id")
                else text(a.get("person_id")),
            )
            for a in aliases
        ),
    )


def _show(args: argparse.Namespace, body: Json) -> int:
    if getattr(args, "json", False):
        print_json(body)
    else:
        print_person(body)
    return EXIT_OK


# --- Commands ------------------------------------------------------------------------------------


def list_command(args: argparse.Namespace, api: Api) -> int:
    body = api.get(
        _persons(args.workspace),
        {
            "query": args.query,
            "merged": "true" if args.merged else None,
            "limit": args.limit,
            "cursor": args.cursor,
        },
    )
    if args.json:
        print_json(body)
        return EXIT_OK
    persons = [as_mapping(p) for p in as_list(body.get("items"))]
    if not persons:
        message("no persons; ingest resolves them from records, or add one with `person add`")
        return EXIT_OK
    print_table(
        ("ID", "NAME", "ME", "MERGED INTO", "ALIASES"),
        (
            (
                text(p.get("id")),
                text(p.get("display_name")),
                "yes" if p.get("is_me") else "",
                text(p.get("merged_into"), ""),
                _aliases_line(p),
            )
            for p in persons
        ),
    )
    following = body.get("next_cursor")
    if isinstance(following, str):
        message(f"more: --cursor {text(following)}")
    return EXIT_OK


def show_command(args: argparse.Namespace, api: Api) -> int:
    return _show(args, api.get(_persons(args.workspace, args.id)))


def lookup_command(args: argparse.Namespace, api: Api) -> int:
    body = api.get(_persons(args.workspace, "lookup"), {"kind": args.kind, "value": args.value})
    return _show(args, body)


def add_command(args: argparse.Namespace, api: Api) -> int:
    aliases: list[tuple[str, str]] = args.alias or []
    body = api.post(
        _persons(args.workspace),
        {
            "display_name": args.name,
            "aliases": [{"kind": kind, "value": value} for kind, value in aliases],
        },
    )
    message(f"added person {text(body.get('id'))}")
    return _show(args, body)


def rename_command(args: argparse.Namespace, api: Api) -> int:
    body = api.patch(_persons(args.workspace, args.id), {"display_name": args.name})
    return _show(args, body)


def alias_add_command(args: argparse.Namespace, api: Api) -> int:
    body = api.post(
        _persons(args.workspace, args.id, "aliases"), {"kind": args.kind, "value": args.value}
    )
    if args.json:
        print_json(body)
        return EXIT_OK
    merged = body.get("merged")
    if merged is not None:
        message(
            f"person {text(merged)} held the alias and is merged into this person now; undo with"
            f" `memini person split {args.workspace} {text(merged)}`"
        )
    elif body.get("created"):
        message("added the alias")
    else:
        message("the person holds the alias already; nothing changed")
    print_person(as_mapping(body.get("person")))
    return EXIT_OK


def alias_remove_command(args: argparse.Namespace, api: Api) -> int:
    path = _persons(args.workspace, args.id, "aliases", args.alias)
    body = api.request("DELETE", path) or {}
    message(f"removed alias {args.alias}")
    return _show(args, body)


def merge_command(args: argparse.Namespace, api: Api) -> int:
    body = api.post(_persons(args.workspace, args.id, "merge"), {"into": args.into})
    message(f"merged person {args.id}; undo with `memini person split {args.workspace} {args.id}`")
    return _show(args, body)


def split_command(args: argparse.Namespace, api: Api) -> int:
    body = api.post(_persons(args.workspace, args.id, "split"))
    message(f"split person {args.id} off; it is a person of its own again")
    return _show(args, body)


def me_command(args: argparse.Namespace, api: Api) -> int:
    path = _persons(args.workspace, "me")
    if args.unset:
        api.request("DELETE", path)
        message("no person is marked as me now")
        return EXIT_OK
    if args.set is not None:
        body = api.request("PUT", path, body={"person_id": args.set}) or {}
        message(f"person {args.set} is me now")
        return _show(args, body)
    return _show(args, api.get(path))


# --- The parser ----------------------------------------------------------------------------------


def _workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")


def _person(parser: argparse.ArgumentParser, help: str = "the person's id") -> None:
    parser.add_argument("id", type=_person_id, help=help)


def _json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the API's JSON answer")


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Add the subcommands of ``memini person`` to its parser."""
    commands = parser.add_subparsers(metavar="<command>", required=True)

    listing = commands.add_parser(
        "list",
        help="list persons, or search them",
        description="List the persons of a workspace by name, each with the aliases of everyone"
        " merged into it; persons merged into others are left out unless --merged.",
    )
    _workspace(listing)
    listing.add_argument("--query", metavar="TEXT", help="only names or aliases containing it")
    listing.add_argument("--merged", action="store_true", help="list merged persons too")
    listing.add_argument(
        "--limit",
        type=_limit,
        default=DEFAULT_PERSON_PAGE,
        help=f"persons per page, 1 to {MAX_PERSON_PAGE} (default: {DEFAULT_PERSON_PAGE})",
    )
    listing.add_argument("--cursor", metavar="CURSOR", help="the page after this cursor")
    _json(listing)
    listing.set_defaults(client=list_command)

    show = commands.add_parser(
        "show", help="show a person", description="Show a person with its aliases."
    )
    _workspace(show)
    _person(show)
    _json(show)
    show.set_defaults(client=show_command)

    lookup = commands.add_parser(
        "lookup",
        help="find the person of an alias",
        description="Show the canonical person an alias resolves to, as ingest resolves it.",
    )
    _workspace(lookup)
    lookup.add_argument("kind", choices=ALIAS_KINDS, help="the kind of alias")
    lookup.add_argument("value", help="the address, user id or name")
    _json(lookup)
    lookup.set_defaults(client=lookup_command)

    add = commands.add_parser(
        "add",
        help="add a person by hand (admin)",
        description="Add a person with aliases no other person holds, e.g. yourself before any"
        " record names you, then mark it with `person me --set`.",
    )
    _workspace(add)
    add.add_argument("name", metavar="display-name", help="the name to show")
    add.add_argument(
        "--alias",
        action="append",
        type=_alias,
        metavar="KIND=VALUE",
        help="an alias, e.g. email=ann@example.test; repeat for more",
    )
    _json(add)
    add.set_defaults(client=add_command)

    rename = commands.add_parser(
        "rename", help="change a person's name (admin)", description="Change a person's name."
    )
    _workspace(rename)
    _person(rename)
    rename.add_argument("name", metavar="display-name", help="the new name")
    _json(rename)
    rename.set_defaults(client=rename_command)

    alias = commands.add_parser(
        "alias", help="add or remove an alias (admin)", description="Add or remove an alias."
    )
    alias_commands = alias.add_subparsers(metavar="<command>", required=True)
    alias_add = alias_commands.add_parser(
        "add",
        help="add an alias to a person",
        description="Add an alias to a person. If another person holds it, the two are one"
        " person: that one is merged into this one, and `person split` undoes it.",
    )
    _workspace(alias_add)
    _person(alias_add)
    alias_add.add_argument("kind", choices=ALIAS_KINDS, help="the kind of alias")
    alias_add.add_argument("value", help="the address, user id or name")
    _json(alias_add)
    alias_add.set_defaults(client=alias_add_command)
    alias_remove = alias_commands.add_parser(
        "remove",
        help="remove an alias of a person",
        description="Remove an alias of a person or of one merged into it.",
    )
    _workspace(alias_remove)
    _person(alias_remove)
    alias_remove.add_argument(
        "alias", metavar="alias-id", type=_alias_id, help="the alias's id, as `person show` shows"
    )
    _json(alias_remove)
    alias_remove.set_defaults(client=alias_remove_command)

    merge = commands.add_parser(
        "merge",
        help="merge a person into another (admin)",
        description="Merge a person into another. Nothing is deleted or moved: the person keeps"
        " its aliases, records and decisions, which now count for the other. `person split`"
        " undoes it.",
    )
    _workspace(merge)
    _person(merge, "the person to merge")
    merge.add_argument(
        "--into", required=True, type=_person_id, metavar="PERSON-ID", help="the person it is"
    )
    _json(merge)
    merge.set_defaults(client=merge_command)

    split = commands.add_parser(
        "split",
        help="undo the merge of a person (admin)",
        description="Undo the merge of a person: it is a person of its own again, with what it"
        " had.",
    )
    _workspace(split)
    _person(split, "the merged person")
    _json(split)
    split.set_defaults(client=split_command)

    me = commands.add_parser(
        "me",
        help="show, set or unset me",
        description="Show the person marked as you in the workspace; --set and --unset change it"
        " (admin).",
    )
    _workspace(me)
    choice = me.add_mutually_exclusive_group()
    choice.add_argument("--set", type=_person_id, metavar="PERSON-ID", help="mark this person")
    choice.add_argument("--unset", action="store_true", help="mark no person")
    _json(me)
    me.set_defaults(client=me_command)
