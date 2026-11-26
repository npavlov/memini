"""One naming convention for every MCP tool, HTTP route and CLI command (docs/mcp/conventions.md).

The checks walk what the service and the CLI offer: the tools' argument and result schemas, the
routes of the OpenAPI document with their parameters, bodies and answers, and the CLI's commands
and options. A name that breaks a rule is a bug; there are no aliases.
"""

import argparse
import re
from collections.abc import Iterator, Mapping
from typing import Any, cast

import pytest

from memini.api.app import create_app
from memini.cli import build_parser
from memini.config import Settings
from memini.mcp.server import TOOLS

VERBS = ("get", "list", "search", "put", "record", "supersede", "close", "retract")
NOUNLESS = {"status", "catalog_guide", "search"}

# Names the convention replaced: none of them may come back as an argument, a parameter or an
# option. Fields of answers are checked against RETIRED_FIELDS: a message's author is data there.
RETIRED = {
    "person",
    "person_ids",
    "author",
    "repository",
    "repositories",
    "item_id",
    "other_project",
    "decided_at",
    "recorded_at",
    "sent_at",
    "sensitive_at",
    "next_until",
    "next_before",
    "before",
    "hits",
    "definitions",
    "chunk_id",
    "at",
    "unassigned_repositories",
}
RETIRED_FIELDS = RETIRED - {"person", "author", "item_id"}
RETIRED_OPTIONS = RETIRED
# Tools that write: the CLI has them as admin commands grouped by noun (memini decision record),
# or not at all (put_document and put_project are for agents; memini push sends records).
WRITE_TOOLS = {
    "put_document",
    "put_project",
    "record_decision",
    "supersede_decision",
    "close_decision",
    "retract_decision",
}
# Options a read command has beyond its tool's arguments: how the answer prints, and the gate.
CLI_ONLY_OPTIONS = {"help", "json", "gate"}
# Tool arguments a read command leaves out, and why: get_domain takes include_sensitive in MCP
# only, where nothing reads it; its HTTP route has no such parameter.
CLI_LEFT_OUT = {("get_domain", "include_sensitive")}
# Commands the CLI had before the read commands took the tools' names; none may come back.
RETIRED_COMMANDS = {"meetings", "messages", "read", "symbol", "decisions", "catalog", "commits"}
# Fields that hold a list of things but are data, not filters or answers: named as they are.
DATA_LISTS = {"evidence", "supersedes", "participants", "paths"}
# Where a word is the data of a document, not a filter: put_document's own text.
DATA_TEXT = {("put_document", "text")}
# Path parameters: the singular noun of a named object, id for an object with an id, a parent's
# id as <noun>_id, and the path of a file or a domain inside its parent.
PATH_PARAMETERS = {"workspace", "source", "project", "domain", "id", "person_id", "path"}
# The list objects of answers.
LIST_KEYS = {"items"}


def _properties(schema: Mapping[str, Any]) -> dict[str, Any]:
    return dict(schema.get("properties", {}))


def _is_list(prop: Mapping[str, Any]) -> bool:
    if prop.get("type") == "array":
        return True
    return any(each.get("type") == "array" for each in prop.get("anyOf", []))


def _all_keys(schema: object) -> Iterator[str]:
    """Every property name anywhere in a JSON schema, its definitions included."""
    if isinstance(schema, dict):
        for key, value in cast("dict[str, object]", schema).items():
            if key == "properties" and isinstance(value, dict):
                yield from cast("dict[str, object]", value)
            yield from _all_keys(cast("object", value))
    elif isinstance(schema, list):
        for value in cast("list[object]", schema):
            yield from _all_keys(value)


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
def test_a_tool_is_a_verb_and_a_noun(tool: Any) -> None:
    name: str = tool.name
    assert name in NOUNLESS or re.fullmatch(rf"({'|'.join(VERBS)})_[a-z]+(_[a-z]+)*", name), name


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
def test_a_tools_arguments_follow_the_convention(tool: Any) -> None:
    arguments = _properties(tool.arguments.model_json_schema())
    for name, prop in arguments.items():
        assert name not in RETIRED, f"{tool.name}: {name}"
        assert name != "text" or (tool.name, name) in DATA_TEXT, f"{tool.name}: text is query"
        if _is_list(prop) and name not in DATA_LISTS:
            assert name.endswith("s"), f"{tool.name}: a filter is a plural list: {name}"
    if tool.name.startswith("get_") and tool.name not in {"get_catalog", "get_file"}:
        catalog = {"project", "domain"} <= set(arguments) or "project" in arguments
        assert "id" in arguments or catalog, f"{tool.name} names its object by id"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
def test_a_tools_result_follows_the_convention(tool: Any) -> None:
    schema = tool.result.model_json_schema()
    retired = sorted(set(_all_keys(schema)) & RETIRED_FIELDS)
    assert not retired, f"{tool.name}: {retired}"
    top = _properties(schema)
    if tool.name.startswith(("list_", "search")):
        assert "items" in top, f"{tool.name} answers items"


def _openapi(settings: Settings) -> dict[str, Any]:
    return create_app(settings).openapi()


def test_every_route_follows_the_convention(settings: Settings) -> None:
    spec = _openapi(settings)
    problems: list[str] = []
    for path, operations in spec["paths"].items():
        for name in re.findall(r"{([a-z_]+)(?::[a-z]+)?}", path):
            if name not in PATH_PARAMETERS:
                problems.append(f"{path}: path parameter {name}")
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                name = parameter["name"]
                if name in RETIRED:
                    problems.append(f"{method.upper()} {path}: parameter {name}")
                schema = parameter.get("schema", {})
                if parameter["in"] == "query" and _is_list(schema) and not name.endswith("s"):
                    problems.append(f"{method.upper()} {path}: list parameter {name}")
    schemas = spec["components"]["schemas"]
    for model, schema in schemas.items():
        retired = sorted(set(_properties(schema)) & RETIRED_FIELDS)
        if retired:
            problems.append(f"schema {model}: {retired}")
    assert not problems, "\n".join(problems)


def test_a_list_route_answers_items(settings: Settings) -> None:
    spec = _openapi(settings)
    schemas = spec["components"]["schemas"]
    for model, schema in schemas.items():
        if model.endswith("List"):
            assert "items" in _properties(schema), f"{model} answers items"


def _commands(parser: argparse.ArgumentParser, trail: str = "") -> Iterator[tuple[str, Any]]:
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        found: object = getattr(action, "choices", None)
        if isinstance(found, dict) and action.nargs == argparse.PARSER:
            choices = cast("dict[str, argparse.ArgumentParser]", found)
            for name, child in choices.items():
                yield from _commands(child, f"{trail} {name}".strip())
        else:
            yield trail, action


def test_the_cli_follows_the_convention() -> None:
    problems: list[str] = []
    for command, action in _commands(build_parser()):
        for option in action.option_strings:
            name = option.lstrip("-").replace("-", "_")
            if name in RETIRED_OPTIONS or name in {"record", "external_id", "decided_at"}:
                problems.append(f"memini {command}: {option}")
        if not action.option_strings and action.dest in RETIRED:
            problems.append(f"memini {command}: <{action.dest}>")
        if not action.option_strings and action.dest.endswith("_id"):
            problems.append(f"memini {command}: <{action.dest}> names its object as id")
    assert not problems, "\n".join(problems)


def _top_level(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        found: object = getattr(action, "choices", None)
        if isinstance(found, dict) and action.nargs == argparse.PARSER:
            return cast("dict[str, argparse.ArgumentParser]", found)
    raise AssertionError("memini has no commands")


@pytest.mark.parametrize(
    "tool", [tool for tool in TOOLS if tool.name not in WRITE_TOOLS], ids=lambda tool: tool.name
)
def test_a_read_tool_is_a_cli_command_with_its_arguments(tool: Any) -> None:
    """``memini search-code`` is ``search_code``: the tool's name and its arguments, with
    dashes. An argument that names the object is positional; every other one is an option."""
    command = tool.name.replace("_", "-")
    commands = _top_level(build_parser())
    assert command in commands, f"no memini {command}"
    arguments = set(_properties(tool.arguments.model_json_schema()))
    taken: set[str] = set()
    for action in commands[command]._actions:  # pyright: ignore[reportPrivateUsage]
        if action.dest in CLI_ONLY_OPTIONS:
            continue
        assert action.dest in arguments, f"memini {command}: {action.dest} is no argument"
        taken.add(action.dest)
        for option in action.option_strings:
            spelled = option.removeprefix("--no-").removeprefix("--").replace("-", "_")
            assert spelled == action.dest, f"memini {command}: {option} for {action.dest}"
    left_out = {name for name in arguments - taken if (tool.name, name) not in CLI_LEFT_OUT}
    assert not left_out, f"memini {command} lacks {sorted(left_out)}"


def test_the_cli_has_no_retired_commands() -> None:
    commands = set(_top_level(build_parser()))
    assert not commands & RETIRED_COMMANDS, sorted(commands & RETIRED_COMMANDS)
    tools = {tool.name.replace("_", "-") for tool in TOOLS}
    # A command named like a tool is that tool; any other command is an admin noun.
    for command in commands:
        verb = command.split("-", 1)[0]
        if verb in VERBS and "-" in command:
            assert command in tools, f"memini {command} is named like a tool but is none"
