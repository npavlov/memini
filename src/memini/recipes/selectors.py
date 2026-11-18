"""Selectors: where a value lives in a tool's response.

A call of a recipe decodes its result (``decode``) and then reads values from it with selectors.
The language of every selector of a call follows from its ``decode``:

**Paths**, for ``json`` and ``structured``. A small subset of JSONPath, with no filters, no
recursive descent and no functions, so a path always selects the same values from the same
document::

    path     = root segment*
    root     = "$"                      the decoded response
             | "@"                      the current item (fields of a listed item)
    segment  = "." name                 a key of an object: [A-Za-z_][A-Za-z0-9_-]*
             | "[" quoted "]"           any key, in double or single quotes; \\ escapes
             | "[" integer "]"          an element of an array; negative counts from the end
             | "[*]" | ".*"             every element of an array, or every value of an object

A path selects a list of values: ``$.messages[*].ts`` every ``ts`` of every message, in document
order. A key that is not there, an index out of range or a step into a value of another type
selects nothing; it is not an error. A path without ``*`` selects at most one value.

**Patterns**, for ``text``. A Python regular expression (``re``), matched against the text of the
response or of an item. A pattern that reads a value has exactly one capturing group, and the
value is the group's text in the first match. A pattern that finds items matches each item once;
the item is the whole match. A pattern that answers yes or no (``has_more``) answers yes when it
matches. Inline flags set the mode: ``(?m)`` for ``^`` and ``$`` at each line, ``(?s)`` for a
``.`` that matches a newline.
"""

import re
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

__all__ = [
    "WILDCARD",
    "JsonPath",
    "Root",
    "SelectorSyntaxError",
    "TextPattern",
    "Wildcard",
    "parse_path",
    "parse_pattern",
]

type Root = Literal["$", "@"]


class Wildcard:
    """The ``[*]`` segment of a path."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "WILDCARD"


WILDCARD: Final = Wildcard()

type Segment = str | int | Wildcard

MAX_SELECTOR_LENGTH: Final = 1000
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_INDEX: Final = re.compile(r"-?[0-9]{1,9}")


class SelectorSyntaxError(ValueError):
    """A selector that cannot be read. The message says where and why, never quoting a value."""


@dataclass(frozen=True, slots=True)
class JsonPath:
    """A parsed path. :meth:`select` reads it from a decoded response or an item."""

    text: str
    root: Root
    segments: tuple[Segment, ...]

    @property
    def many(self) -> bool:
        """Whether it can select more than one value: it has a ``*``."""
        return any(isinstance(segment, Wildcard) for segment in self.segments)

    def select(self, document: Any) -> list[Any]:
        """The values the path selects in ``document``, in document order."""
        current: list[Any] = [document]
        for segment in self.segments:
            following: list[Any] = []
            for value in current:
                if isinstance(value, list):
                    elements = cast(list[Any], value)
                    if isinstance(segment, Wildcard):
                        following.extend(elements)
                    elif isinstance(segment, int) and -len(elements) <= segment < len(elements):
                        following.append(elements[segment])
                elif isinstance(value, dict):
                    members = cast(dict[str, Any], value)
                    if isinstance(segment, Wildcard):
                        following.extend(members.values())
                    elif isinstance(segment, str) and segment in members:
                        following.append(members[segment])
            current = following
        return current

    def first(self, document: Any) -> Any | None:
        """The first value the path selects, or ``None``."""
        found = self.select(document)
        return found[0] if found else None


@dataclass(frozen=True, slots=True)
class TextPattern:
    """A compiled pattern over the text of a response or an item."""

    text: str
    pattern: re.Pattern[str]

    @property
    def groups(self) -> int:
        return self.pattern.groups

    def value(self, text: str) -> str | None:
        """The first capturing group of the first match, or ``None`` without a match."""
        match = self.pattern.search(text)
        if match is None:
            return None
        group = match.group(1) if self.pattern.groups else match.group(0)
        return group  # None when the group took no part in the match

    def items(self, text: str) -> list[str]:
        """The whole text of every match, in order."""
        return [match.group(0) for match in self.pattern.finditer(text)]

    def matches(self, text: str) -> bool:
        return self.pattern.search(text) is not None


def parse_path(text: str) -> JsonPath:
    """Parse a path; raises :class:`SelectorSyntaxError` naming the character where it fails."""
    if not text:
        raise SelectorSyntaxError("a path is not empty; it starts with $ or @")
    if len(text) > MAX_SELECTOR_LENGTH:
        raise SelectorSyntaxError(f"a path is at most {MAX_SELECTOR_LENGTH} characters")
    root = text[0]
    if root != "$" and root != "@":
        raise SelectorSyntaxError("a path starts with $ (the response) or @ (the item)")
    segments: list[Segment] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == ".":
            if text.startswith(".*", index):
                segments.append(WILDCARD)
                index += 2
                continue
            name = _NAME.match(text, index + 1)
            if name is None:
                raise _error(index + 1, "expected a key after '.' (or use [\"...\"])")
            segments.append(name.group(0))
            index = name.end()
        elif char == "[":
            index = _bracket(text, index + 1, segments)
        else:
            raise _error(index, "expected '.' or '['")
    return JsonPath(text=text, root=root, segments=tuple(segments))


def _bracket(text: str, index: int, segments: list[Segment]) -> int:
    """Read a bracket segment after its ``[``; return the position after its ``]``."""
    if text.startswith("*]", index):
        segments.append(WILDCARD)
        return index + 2
    if index < len(text) and text[index] in "\"'":
        quote = text[index]
        key: list[str] = []
        position = index + 1
        while position < len(text):
            char = text[position]
            if char == "\\":
                if position + 1 >= len(text):
                    break
                key.append(text[position + 1])
                position += 2
            elif char == quote:
                if not text.startswith("]", position + 1):
                    raise _error(position + 1, "expected ']' after the quoted key")
                segments.append("".join(key))
                return position + 2
            else:
                key.append(char)
                position += 1
        raise _error(index, "the quoted key is not closed")
    number = _INDEX.match(text, index)
    if number is None or not text.startswith("]", number.end()):
        raise _error(index, "expected *, an integer or a quoted key in [...]")
    segments.append(int(number.group(0)))
    return number.end() + 1


def _error(position: int, reason: str) -> SelectorSyntaxError:
    return SelectorSyntaxError(f"not a valid path at character {position + 1}: {reason}")


def parse_pattern(text: str) -> TextPattern:
    """Compile a pattern; raises :class:`SelectorSyntaxError` with the reason ``re`` gives."""
    if not text:
        raise SelectorSyntaxError("a pattern is not empty")
    if len(text) > MAX_SELECTOR_LENGTH:
        raise SelectorSyntaxError(f"a pattern is at most {MAX_SELECTOR_LENGTH} characters")
    try:
        compiled = re.compile(text)
    except re.error as error:
        where = f" at character {error.pos + 1}" if error.pos is not None else ""
        raise SelectorSyntaxError(f"not a valid regular expression{where}: {error.msg}") from None
    return TextPattern(text=text, pattern=compiled)
