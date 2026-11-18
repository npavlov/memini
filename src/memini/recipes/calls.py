"""The calls of one window of a recipe: those made, as stored (:class:`RawResponse`), and those
that would complete the window (:class:`MissingCall`).

A window of a stream is read from its stored calls only (:class:`WindowInput`), so both executors
and replay see the same input: the recipe, the stream key, the window ``[start, end)`` and the
calls in the order they were made. :mod:`memini.recipes.reading` reads them with the recipe's
selectors and checks them; :mod:`memini.recipes.parsing` turns them into records.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from pydantic import JsonValue

from memini.recipes.model import Decode, Recipe

__all__ = [
    "CHECKS",
    "CallKind",
    "Check",
    "MissingCall",
    "RawResponse",
    "ResponseDecodeError",
    "WindowInput",
]

CallKind = Literal["list", "detail"]
"""Which call of the recipe a response answers: the list call or the detail call of an item."""

TEXT_BLOCK: Final = "text"


class ResponseDecodeError(ValueError):
    """A stored result cannot be read as the recipe's ``decode`` asks. The message never holds a
    value of the result."""


@dataclass(frozen=True, slots=True)
class RawResponse:
    """One call of a window and its result, as stored (:mod:`memini.responses`)."""

    position: int
    """The call's place in the window, from 1, in the order the calls were made."""
    call: CallKind
    tool: str
    arguments: Mapping[str, Any]
    """The arguments as sent, with secrets redacted."""
    page: int
    """The page's place in its listing (``list``) or in its item's detail call, from 0."""
    item_id: str | None
    """For a detail call, the id of the listed item it was made for
    (:func:`memini.recipes.reading.item_key`)."""
    result: Mapping[str, Any]
    """The MCP tool result: ``content`` blocks, ``structuredContent`` and ``isError``, with
    secrets and card numbers redacted, otherwise as the server returned it."""

    @property
    def is_error(self) -> bool:
        """Whether the server reported the call as failed (``isError``)."""
        return self.result.get("isError") is True

    def text(self) -> str:
        """The text of the result's text content blocks, joined by newlines."""
        blocks = self.result.get("content")
        if not isinstance(blocks, list):
            return ""
        texts: list[str] = []
        for block in cast(list[Any], blocks):
            if isinstance(block, Mapping):
                entry = cast(Mapping[str, Any], block)
                text = entry.get("text")
                if entry.get("type") == TEXT_BLOCK and isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)

    def decode(self, decode: Decode) -> Any:
        """The result read as a recipe's call reads it (docs/recipes.md, Decoding): ``json``, the
        text parsed as JSON; ``structured``, the ``structuredContent`` object; ``text``, the text.

        Raises:
            ResponseDecodeError: the text is not JSON, or there is no ``structuredContent``.
        """
        match decode:
            case "text":
                return self.text()
            case "structured":
                structured = self.result.get("structuredContent")
                if not isinstance(structured, Mapping):
                    raise ResponseDecodeError(
                        f"response {self.position}: the result has no structuredContent object"
                    )
                return cast(Mapping[str, Any], structured)
            case "json":
                try:
                    return json.loads(self.text())
                except (ValueError, RecursionError):
                    raise ResponseDecodeError(
                        f"response {self.position}: the text of the result is not JSON"
                    ) from None


@dataclass(frozen=True, slots=True)
class WindowInput:
    """What a parser reads: the stored responses of one window of one stream."""

    recipe: Recipe
    stream: str
    """The stream key: ``{stream}`` in the recipe's arguments and URL patterns."""
    start: datetime
    end: datetime
    responses: tuple[RawResponse, ...]
    """In the order the calls were made. Parsers find a call by its identity (tool, arguments,
    item), not by its position alone: with ``rate.concurrency`` above 1 the detail calls of a
    window may come back in any order."""


Check = Literal[
    "failed",
    "page",
    "truncated",
    "max_pages",
    "detail",
    "item",
    "page_numbers",
    "reply_counts",
    "totals",
]
"""The check a :class:`MissingCall` would make pass.

- ``failed``: the call failed (``isError``) or its result cannot be read by ``decode``; the
  same call again.
- ``page``: a page the paging rule asks for is not in the window: the first call of a listing,
  the page a cursor names, the next page number or offset, or an item's detail call after its
  first page.
- ``truncated``: a page says more pages follow (``has_more``) but names no cursor; the same call
  again.
- ``max_pages``: the listing reached ``max_pages`` before its last page; its next page.
- ``detail``: a listed item that ``detail.when`` selects has no detail call.
- ``item``: a listed item cannot be read (no id; a time or a reply count that is not one), or
  the parser found it incomplete; the call that holds it again.
- ``page_numbers``, ``reply_counts``, ``totals``: the checks a recipe declares in ``[checks]``.
"""

CHECKS: Final[tuple[Check, ...]] = (
    "failed",
    "page",
    "truncated",
    "max_pages",
    "detail",
    "item",
    "page_numbers",
    "reply_counts",
    "totals",
)


@dataclass(frozen=True, slots=True)
class MissingCall:
    """A call that would complete an incomplete window: a check failed, and this call is the one
    that would make it pass. The executors make it (the service executor) or hand it out as the
    next call (the agent executor); the run report lists it."""

    call: CallKind
    tool: str
    arguments: Mapping[str, Any]
    """The arguments to send: the recipe's, rendered for the stream, the window and the item,
    with the paging arguments of the page. Values read from stored responses (a cursor, an item
    id) are as stored, so redacted where redaction found a secret."""
    page: int
    """The page's place in its listing or detail call, from 0: the ``page`` to store it with."""
    item_id: str | None
    """For a detail call, the key of the listed item: the ``item_id`` to store it with."""
    check: Check
    reason: str
    """What failed, for the run report: the call, the page or the item by its position in the
    window, never a value of a response."""
    repeat: bool = False
    """The window holds this call already (a failed or truncated page, a detail call that holds
    fewer replies than listed): making it again replaces the stored result, as a retry does."""

    def to_json(self) -> dict[str, JsonValue]:
        """The call as the run report lists it."""
        return {
            "call": self.call,
            "tool": self.tool,
            "arguments": cast(JsonValue, dict(self.arguments)),
            "page": self.page,
            "item_id": self.item_id,
            "check": self.check,
            "reason": self.reason,
            "repeat": self.repeat,
        }
