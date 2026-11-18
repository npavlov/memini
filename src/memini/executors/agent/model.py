"""What the agent executor works with: its runs, windows, calls and answers, its errors, the recipe
of a source, and the MCP result a capture hook posts. No database here; :mod:`.session` stores
them."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast
from uuid import UUID

from pydantic import JsonValue

from memini.executors.source import McpConfigError, load_recipe_ref, recipe_ref
from memini.intake import PlannedWindow, WindowKind
from memini.recipes import (
    CallKind,
    Check,
    Recipe,
    Server,
)
from memini.responses import ServerInfo, WindowIngest

__all__ = [
    "MAX_ATTEMPTS",
    "RECIPE_KEY",
    "RUN_KIND",
    "WINDOW_KEY",
    "AgentRun",
    "AgentRunClosedError",
    "AgentRunOpenError",
    "AgentWindow",
    "CallMismatchError",
    "CallNotHandedOutError",
    "FinishResult",
    "HandedOutCall",
    "InvalidResultError",
    "NextStatus",
    "NextStep",
    "Receipt",
    "RecipeChangedError",
    "ServerMismatchError",
    "SourceNotReadyError",
    "WindowOutcome",
    "WindowStatus",
    "mcp_result",
    "source_recipe",
    "version_matches",
    "window_overrides",
]

RUN_KIND: Final = "agent_sync"
"""The kind of the run report a finished agent run writes."""

MAX_ATTEMPTS: Final = 3
"""How often one call of a window is handed out at most: the first time, then again while it gets
no response (the agent's call failed before the hook saw it) or its response fails the window's
checks (``isError``, a truncated page). A call past it is not handed out, and the window ends
incomplete."""

RECIPE_KEY: Final = "recipe"
"""The key of ``source.config`` that holds an mcp source's recipe, in a form
:func:`memini.executors.source.recipe_ref` reads: the text the source holds with its name and
digest, a bundled recipe by name, the TOML text, or the recipe as a JSON object."""

WINDOW_KEY: Final = "window"
"""The key of ``source.config`` that holds an mcp source's window overrides
(:func:`memini.intake.resolve_settings`)."""

WindowStatus = Literal["pending", "open", "called", "skipped", "ingested"]
"""Where a window of an agent run is: ``pending``, not opened yet; ``open``, its calls are being
handed out; ``called``, nothing more to hand out; ``skipped``, an earlier window of its chain ended
incomplete; ``ingested``, parsed and ingested by finish."""

NextStatus = Literal["call", "window_done", "run_done"]
"""What ``next`` answers: a call to make; the current window has nothing more to hand out (the
next ``next`` opens the following one); no window is left, so the run is ready to finish."""


# --- Errors --------------------------------------------------------------------------------------


class SourceNotReadyError(Exception):
    """The source cannot be run by the agent executor: no recipe, a recipe that is not valid, no
    parser of that name in this build, window settings that are not valid. The message names the
    setting, never a value of a record."""


class ServerMismatchError(Exception):
    """The session's server reports another name than the recipe's: the run stops before a call
    (docs/recipes.md, ``[server]``)."""


class AgentRunOpenError(Exception):
    """The stream has an agent run that is not finished: finish it first. ``run_id`` is ``None``
    when it finished in the meantime, and starting again works."""

    def __init__(self, run_id: UUID | None) -> None:
        named = "an agent run" if run_id is None else f"agent run {run_id}"
        super().__init__(f"{named} of the stream is not finished; finish it first")
        self.run_id = run_id


class AgentRunClosedError(Exception):
    """The run is finishing or finished: no call is handed out or answered any more."""

    def __init__(self, finished: bool) -> None:
        super().__init__("the agent run is finished" if finished else "the agent run is finishing")
        self.finished = finished


class RecipeChangedError(Exception):
    """The source's recipe is not the one the run was planned from: finish the run and start
    another."""


class CallNotHandedOutError(Exception):
    """The response names a call this run did not hand out."""


class CallMismatchError(Exception):
    """The response is for another call than the one handed out under its id. ``field`` says
    which part differs: ``tool`` or ``arguments``; never their values."""

    def __init__(self, field: str) -> None:
        super().__init__(
            f"the {field} of the call differ from the call handed out: make the call exactly as"
            " it was handed out"
        )
        self.field = field


class InvalidResultError(ValueError):
    """What the hook posted is not an MCP tool result. The message never holds a value of it."""


# --- The recipe of a source ----------------------------------------------------------------------


def source_recipe(config: Mapping[str, Any]) -> Recipe:
    """The recipe of an mcp source, from its configuration (:data:`RECIPE_KEY`), read as the
    service executor reads it (:func:`memini.executors.source.recipe_ref`): the recipe text the
    source holds with its name and digest, a bundled recipe by name, the TOML text itself, or the
    recipe as a table.

    Raises:
        SourceNotReadyError: no recipe, or one that is not TOML or not a valid recipe.
    """
    ref = recipe_ref(config.get(RECIPE_KEY))
    if ref is None:
        raise SourceNotReadyError(f"the source has no recipe (source.config.{RECIPE_KEY})")
    try:
        return load_recipe_ref(ref)
    except McpConfigError as error:
        raise SourceNotReadyError(f"the source's recipe is not valid: {error.reason}") from None


def window_overrides(config: Mapping[str, Any]) -> Mapping[str, object] | None:
    """The source's overrides of the recipe's ``[window]`` (:data:`WINDOW_KEY`), if any.

    Raises:
        SourceNotReadyError: they are not a table.
    """
    value = config.get(WINDOW_KEY)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SourceNotReadyError(f"source.config.{WINDOW_KEY} is not a table")
    return cast(Mapping[str, object], value)


def version_matches(version: str, server: Server) -> bool:
    """Whether ``version`` is one the recipe was checked against: ``server.versions``, where ``*``
    matches any characters; any version when the list is empty."""
    if not server.versions:
        return True
    return any(
        re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), version)
        for pattern in server.versions
    )


# --- The result a hook posts ---------------------------------------------------------------------


def _is_block(value: object) -> bool:
    return isinstance(value, Mapping) and isinstance(
        cast(Mapping[str, Any], value).get("type"), str
    )


def mcp_result(result: object = None, error: str | None = None) -> dict[str, Any]:
    """The MCP tool result (``content`` blocks, ``structuredContent``, ``isError``) of what a
    capture hook posts, as the raw response store keeps it.

    - ``result`` an object with a ``content`` list: an MCP result, taken as it is.
    - ``result`` a list of content blocks (objects with a ``type``): the content of a result that
      succeeded, as Claude Code hands an MCP tool's output to a ``PostToolUse`` hook;
      ``{"content": result, "isError": false}``.
    - ``result`` a string: the text of a result that succeeded; one text block.
    - ``error`` instead: the call failed (a ``PostToolUseFailure`` hook); its message as one text
      block with ``isError`` true, so the window's checks hand the call out again.

    Nothing of the content is changed: the blocks are the server's, only the envelope is added.

    Raises:
        InvalidResultError: neither or both given, or a result of another shape.
    """
    if (result is None) == (error is None):
        raise InvalidResultError("send either the result of the call or its error")
    if error is not None:
        return {"content": [{"type": "text", "text": error}], "isError": True}
    if isinstance(result, str):
        return {"content": [{"type": "text", "text": result}], "isError": False}
    if isinstance(result, Mapping):
        found = cast(Mapping[str, Any], result)
        if not isinstance(found.get("content"), list):
            raise InvalidResultError("an MCP result object has a content list")
        return dict(found)
    if isinstance(result, list):
        blocks = cast(list[Any], result)
        if not all(_is_block(block) for block in blocks):
            raise InvalidResultError("a list of content blocks holds objects with a type")
        return {"content": blocks, "isError": False}
    raise InvalidResultError("a result is an MCP result object, a list of content blocks or a text")


# --- Runs, windows and calls ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentWindow:
    """One window of an agent run's plan."""

    position: int
    """Its place in the plan, from 0."""
    kind: WindowKind
    start: datetime
    end: datetime
    status: WindowStatus
    window_id: UUID | None
    """The raw window (:mod:`memini.responses`), once opened."""
    complete: bool | None
    """Its claim at finish; ``None`` before."""

    @property
    def planned(self) -> PlannedWindow:
        return PlannedWindow(kind=self.kind, start=self.start, end=self.end)

    @property
    def chain(self) -> str:
        """The chain it belongs to: the windows that move the checkpoint, or the frontier."""
        return self.planned.moves


@dataclass(frozen=True, slots=True)
class AgentRun:
    """An agent run as stored."""

    id: UUID
    workspace_id: UUID
    owner_id: UUID
    source_id: UUID
    stream_id: UUID
    workspace: str
    source: str
    stream: str
    """The stream key."""
    as_of: datetime
    recipe_digest: str
    server: ServerInfo
    plan: Mapping[str, JsonValue]
    counts: Mapping[str, int]
    started_at: datetime
    closed_at: datetime | None
    finished_at: datetime | None
    run_id: UUID | None
    """The run report, once finished."""
    windows: tuple[AgentWindow, ...]

    @property
    def state(self) -> Literal["open", "finishing", "finished"]:
        if self.finished_at is not None:
            return "finished"
        return "open" if self.closed_at is None else "finishing"


@dataclass(frozen=True, slots=True)
class HandedOutCall:
    """A call handed out: make it exactly so, and post its result with ``id``."""

    id: UUID
    call: CallKind
    tool: str
    """The tool's name at the server, as the recipe names it."""
    arguments: Mapping[str, Any]
    page: int
    item_id: str | None
    check: Check
    """The check the call makes pass (:class:`memini.recipes.MissingCall`)."""
    reason: str
    """Why it is needed: positions, never values."""
    attempt: int
    """How often this call of the window was handed out, this time included."""


@dataclass(frozen=True, slots=True)
class NextStep:
    """What ``next`` answers."""

    status: NextStatus
    window: AgentWindow | None = None
    """The window of the call, or the window just done; ``None`` with ``run_done``."""
    call: HandedOutCall | None = None
    complete: bool | None = None
    """``window_done``: whether every check of the window passes, as far as the stored responses
    tell before the parser runs."""
    left: int = 0
    """``window_done``: calls the window still misses that are not handed out (past
    ``max_pages``, or tried :data:`MAX_ATTEMPTS` times)."""
    reason: str | None = None
    """``run_done``: why no window is left."""


@dataclass(frozen=True, slots=True)
class Receipt:
    """What storing a posted response did: counts and positions, never a value of the response."""

    call_id: UUID
    window: AgentWindow
    position: int
    """The response's place in its window, from 1."""
    stored: bool
    """False when the window held the same result for the call already."""
    is_error: bool
    responses: int
    """Responses the window holds now."""
    redacted: int
    """Values redacted from the result and the arguments."""


@dataclass(frozen=True, slots=True)
class WindowOutcome:
    """What finish did with one window."""

    window: AgentWindow
    ingest: WindowIngest | None
    """``None`` for a window that was not ingested: never opened, skipped, or gone."""


@dataclass(frozen=True, slots=True)
class FinishResult:
    """What finish did."""

    agent_run: AgentRun
    run_id: UUID
    """The run report (``GET /v1/runs/{id}``)."""
    windows: Sequence[WindowOutcome]
    checked_until: datetime | None
    """The stream's checkpoint after the run."""
