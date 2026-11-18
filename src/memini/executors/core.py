"""What both executors of a recipe share (docs/recipes.md, How the executors use a recipe).

The service executor (:mod:`memini.executors.service`) and the agent executor (memini-2wl) differ in
who makes a call: the built-in MCP client, or an agent whose hook posts the result. Everything
around the call is the same, and lives here or in the libraries it names, so that one recipe run by
both executors makes the same calls and yields the same records:

1. **The recipe and its settings.** :func:`source_recipe` reads the recipe an mcp source holds
   (:class:`~memini.executors.source.McpConfig`) and finds its parser in this build;
   :func:`window_settings` applies the source's window overrides.
2. **The plan.** :func:`memini.intake.pin_as_of` (a job) or the session's pinned time,
   :func:`memini.intake.plan_stream` with those settings, :func:`memini.intake.record_plan`. Each
   chain of windows (``plan.forward``, ``plan.backfill``) runs oldest or newest first and stops at
   its first incomplete window.
3. **Drift.** :func:`check_drift` compares the server's name, version and tools with the recipe
   before any call; an error stops the run (:class:`DriftError`), a warning goes to the report.
4. **A window.** :func:`memini.responses.begin_window`; then, until nothing is left to make,
   :func:`memini.responses.load_window` and :func:`calls_to_make` name the calls, each made and its
   result stored with :func:`memini.responses.store_response` (redacted, before anything parses
   it); then :func:`memini.responses.ingest_window` parses, checks and ingests the window with its
   claim, and reports an incomplete one in the run report.

:func:`calls_to_make` is where the executors' loops meet the recipe: it never names a call past
``max_pages``, and it stops naming a call once it was tried ``attempts`` times in the window, so a
loop ends even when a server keeps failing. The agent executor can hand out its result as "next
calls" one at a time; the service executor makes them, up to the recipe's concurrency.
"""

import fnmatch
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import JsonValue

from memini.executors.source import McpConfig, McpConfigError, load_recipe_ref
from memini.intake import WindowSettings, resolve_settings
from memini.recipes import (
    CursorPaging,
    MissingCall,
    NoPaging,
    OffsetPaging,
    PagePaging,
    Paging,
    Recipe,
    WindowInput,
    WindowParser,
    get_parser,
    missing_calls,
)
from memini.responses import ServerInfo

__all__ = [
    "Drift",
    "DriftError",
    "ToolInfo",
    "call_identity",
    "calls_to_make",
    "check_drift",
    "recipe_arguments",
    "source_recipe",
    "window_settings",
]


# --- The recipe of a source ----------------------------------------------------------------------


def source_recipe(config: McpConfig) -> tuple[Recipe, WindowParser]:
    """The recipe the source holds, read and checked now, and its parser in this build.

    Raises:
        McpConfigError: the source has no recipe, or it is not valid any more (``recipe``).
        ParserNotFoundError: this build has no parser of the recipe's name.
    """
    if config.recipe is None:
        raise McpConfigError("recipe", "the source holds no recipe")
    recipe = load_recipe_ref(config.recipe)
    return recipe, get_parser(recipe.parser.name)


def window_settings(recipe: Recipe, config: McpConfig) -> WindowSettings:
    """The recipe's ``[window]`` with the source's overrides (:func:`memini.intake.
    resolve_settings`).

    Raises:
        McpConfigError: an override is not valid with this recipe (``window.<key>``).
    """
    try:
        return resolve_settings(recipe.window, config.window)
    except ValueError as error:
        setting, _, reason = str(error).partition(": ")
        raise McpConfigError(setting or "window", reason or "not valid") from None


# --- Drift ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """A tool the server lists: its name and its input schema."""

    name: str
    input_schema: Mapping[str, Any] = field(default_factory=dict[str, Any])


class DriftError(Exception):
    """The server is not the one the recipe was written for, or no longer offers what it calls:
    the run stops before any call. The message names the problems by the recipe's names (server,
    tools, arguments) and the server's version; nothing else the server answered."""

    def __init__(self, drift: "Drift") -> None:
        super().__init__("the server drifted from the recipe: " + "; ".join(drift.errors))
        self.drift = drift


@dataclass(frozen=True, slots=True)
class Drift:
    """What :func:`check_drift` found."""

    server: ServerInfo | None
    errors: tuple[str, ...] = ()
    """Each stops the run."""
    warnings: tuple[str, ...] = ()
    """Each goes into the run report."""

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> dict[str, JsonValue]:
        """For the run report (``details.drift``)."""
        return {
            "server_name": None if self.server is None else self.server.name,
            "server_version": None if self.server is None else self.server.version,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _paging_arguments(paging: Paging) -> set[str]:
    match paging:
        case CursorPaging():
            return {paging.argument}
        case PagePaging() | OffsetPaging():
            return {paging.argument} | ({paging.size_argument} if paging.size_argument else set())
        case NoPaging():
            return set()


def recipe_arguments(recipe: Recipe) -> dict[str, set[str]]:
    """The arguments the recipe sends to each tool it calls: its own and its paging rule's."""
    sent: dict[str, set[str]] = {
        recipe.listing.tool: set(recipe.listing.arguments)
        | _paging_arguments(recipe.listing.paging)
    }
    if recipe.detail is not None:
        sent.setdefault(recipe.detail.tool, set()).update(
            set(recipe.detail.arguments) | _paging_arguments(recipe.detail.paging)
        )
    return sent


def _version_matches(version: str, patterns: Sequence[str]) -> bool:
    # fnmatchcase: `*` any characters; recipe versions use no other wildcard in practice, and a
    # `?` or `[` in a version string is matched as fnmatch reads it.
    return any(fnmatch.fnmatchcase(version, pattern) for pattern in patterns)


def check_drift(recipe: Recipe, server: ServerInfo | None, tools: Sequence[ToolInfo]) -> Drift:
    """Compare what the server reported with the recipe (docs/recipes.md, ``[server]``).

    Errors: the server reports another name (or none); a tool the recipe calls is not listed; an
    argument the recipe sends is not among the properties of the tool's input schema, when the
    schema declares its properties. Warning: the version matches none of ``server.versions``.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if server is None or not server.name:
        errors.append(f"the server did not report its name; the recipe is for {recipe.server.name}")
    elif server.name != recipe.server.name:
        errors.append(
            f"the server reports another name than {recipe.server.name}, the recipe's server"
        )
    versions = recipe.server.versions
    if server is not None and server.name == recipe.server.name and versions:
        if not server.version:
            warnings.append("the server did not report its version")
        elif not _version_matches(server.version, versions):
            warnings.append(
                f"the server's version {server.version} matches none of the recipe's"
                f" server.versions ({', '.join(versions)})"
            )
    listed = {tool.name: tool for tool in tools}
    for tool, arguments in recipe_arguments(recipe).items():
        found = listed.get(tool)
        if found is None:
            errors.append(f"the server does not list the tool {tool}")
            continue
        properties = found.input_schema.get("properties")
        if not isinstance(properties, Mapping):
            continue
        declared = set(cast(Mapping[str, Any], properties))
        unknown = sorted(arguments - declared)
        if unknown:
            errors.append(
                f"the input schema of {tool} does not declare the arguments the recipe sends:"
                f" {', '.join(unknown)}"
            )
    return Drift(server=server, errors=tuple(errors), warnings=tuple(warnings))


# --- The calls of a window -----------------------------------------------------------------------


def call_identity(call: MissingCall) -> str:
    """A call's identity in its window, as the store knows it: its kind, tool and arguments."""
    return json.dumps(
        [call.call, call.tool, call.arguments],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def calls_to_make(
    window: WindowInput, attempts: Mapping[str, int], max_attempts: int
) -> list[MissingCall]:
    """The calls of ``window`` to make now: :func:`memini.recipes.missing_calls`, less a call past
    ``max_pages`` (never made: the listing is incomplete) and a call tried ``max_attempts`` times
    in this window already (``attempts`` by :func:`call_identity`), each once."""
    todo: list[MissingCall] = []
    seen: set[str] = set()
    for call in missing_calls(window):
        if call.check == "max_pages":
            continue
        identity = call_identity(call)
        if identity in seen or attempts.get(identity, 0) >= max_attempts:
            continue
        seen.add(identity)
        todo.append(call)
    return todo
