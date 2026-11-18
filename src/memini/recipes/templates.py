"""Argument templates: the arguments of a call, with placeholders the executor fills.

The ``arguments`` of a call are a TOML table sent as the tool's JSON arguments. A string in it,
at any depth, may hold placeholders in braces:

- ``{stream}``: the stream key (a channel id, a mailbox query);
- ``{from}``, ``{to}``: the bounds of the window, ``[from, to)``, optionally with a format:
  ``{from:unix}``, ``{to:%Y/%m/%d}`` (:mod:`memini.recipes.times`; ``rfc3339`` by default);
- in a detail call only, ``{item.id}``, ``{item.timestamp}``, ``{item.reply_count}`` and
  ``{item.NAME}`` for a name of the listed item's ``fields``: values the list call read for the
  item the detail call is about.

``{{`` and ``}}`` are literal braces. A string that is one placeholder and nothing else becomes
the value itself: ``"{from:unix}"`` an integer, ``"{item.id}"`` the id as the response held it
(a number stays a number). Inside a longer string a value is written as text, a string as it is
and anything else as JSON. Paging arguments (a cursor, a page number, an offset, a page size)
are not placeholders: the paging rule names them and the executor sets them.
"""

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from memini.recipes.times import check_bound_format, format_bound

__all__ = [
    "ITEM_PREFIX",
    "Placeholder",
    "Template",
    "TemplateError",
    "parse_template",
    "placeholders",
    "render_arguments",
]

ITEM_PREFIX: Final = "item."
_NAME: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?")
_WINDOW: Final = frozenset({"from", "to"})


class TemplateError(ValueError):
    """A string with a placeholder that cannot be read. The message says where in the string and
    why; ``path`` is where the string is in the arguments, when :func:`placeholders` raised it."""

    def __init__(self, message: str, path: tuple[str | int, ...] = ()) -> None:
        super().__init__(message)
        self.path = path


@dataclass(frozen=True, slots=True)
class Placeholder:
    name: str
    """``stream``, ``from``, ``to`` or ``item.NAME``."""
    format: str | None = None
    """For ``from`` and ``to``: the format of the bound; ``None`` is ``rfc3339``."""


@dataclass(frozen=True, slots=True)
class Template:
    """A parsed string: literal text and placeholders, in order."""

    parts: tuple[str | Placeholder, ...]

    @property
    def placeholders(self) -> tuple[Placeholder, ...]:
        return tuple(part for part in self.parts if isinstance(part, Placeholder))

    @property
    def whole(self) -> Placeholder | None:
        """The placeholder when the string is that and nothing else."""
        if len(self.parts) == 1 and isinstance(self.parts[0], Placeholder):
            return self.parts[0]
        return None


def parse_template(text: str) -> Template:
    """Parse a string of the arguments; raises :class:`TemplateError`.

    Checks the syntax and the format of a bound, not whether a name is available to the call:
    :func:`memini.recipes.validate.check_recipe` does that.
    """
    parts: list[str | Placeholder] = []
    literal: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if text.startswith("{{", index) or text.startswith("}}", index):
            literal.append(char)
            index += 2
        elif char == "}":
            raise TemplateError(f"a lone '}}' at character {index + 1}; write '}}}}' for a brace")
        elif char == "{":
            end = text.find("}", index)
            if end < 0:
                raise TemplateError(f"the placeholder at character {index + 1} is not closed")
            body = text[index + 1 : end]
            name, colon, fmt = body.partition(":")
            if _NAME.fullmatch(name) is None:
                raise TemplateError(
                    f"the placeholder at character {index + 1} has no valid name; names are"
                    " stream, from, to and item.NAME"
                )
            if colon:
                if name not in _WINDOW:
                    raise TemplateError(f"{{{name}:...}}: only {{from}} and {{to}} take a format")
                try:
                    check_bound_format(fmt)
                except ValueError as error:
                    raise TemplateError(f"{{{name}:...}}: {error}") from None
            if literal:
                parts.append("".join(literal))
                literal = []
            parts.append(Placeholder(name=name, format=fmt if colon else None))
            index = end + 1
        else:
            literal.append(char)
            index += 1
    if literal:
        parts.append("".join(literal))
    return Template(parts=tuple(parts))


def placeholders(arguments: Mapping[str, Any]) -> list[tuple[tuple[str | int, ...], Placeholder]]:
    """Every placeholder in ``arguments``, with the path of the string that holds it (keys and
    array positions, ``("query", "text")``), in order.

    Raises :class:`TemplateError`, with its ``path``, for the first string that cannot be read.
    """
    found: list[tuple[tuple[str | int, ...], Placeholder]] = []

    def visit(value: Any, path: tuple[str | int, ...]) -> None:
        if isinstance(value, str):
            try:
                template = parse_template(value)
            except TemplateError as error:
                raise TemplateError(str(error), path) from None
            found.extend((path, placeholder) for placeholder in template.placeholders)
        elif isinstance(value, Mapping):
            for key, item in cast(Mapping[str, Any], value).items():
                visit(item, (*path, key))
        elif isinstance(value, list):
            for position, item in enumerate(cast(list[Any], value)):
                visit(item, (*path, position))

    for key, value in arguments.items():
        visit(value, (key,))
    return found


def render_arguments(
    arguments: Mapping[str, Any],
    *,
    stream: str,
    window: tuple[datetime, datetime] | None = None,
    item: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The arguments with every placeholder filled.

    ``window`` is ``(from, to)``, both with a zone; ``item`` maps the names of the listed item's
    values (``id``, ``timestamp``, ``reply_count`` and its ``fields``) to what the list call read.
    Raises ``KeyError`` naming a placeholder that has no value here, and ``ValueError`` for a
    non-finite number. The result is a new object; ``arguments`` is not changed.
    """

    def value_of(placeholder: Placeholder) -> Any:
        name = placeholder.name
        if name == "stream":
            return stream
        if name in _WINDOW:
            if window is None:
                raise KeyError(f"{{{name}}} needs a window")
            moment = window[0] if name == "from" else window[1]
            bound = "from" if name == "from" else "to"
            return format_bound(moment, placeholder.format or "rfc3339", bound)
        if name.startswith(ITEM_PREFIX) and item is not None:
            key = name.removeprefix(ITEM_PREFIX)
            if key in item:
                return item[key]
        raise KeyError(f"{{{name}}} has no value in this call")

    return cast(dict[str, Any], _render(arguments, value_of))


def _render(value: Any, value_of: Callable[[Placeholder], Any]) -> Any:
    if isinstance(value, str):
        template = parse_template(value)
        whole = template.whole
        if whole is not None:
            return value_of(whole)
        return "".join(
            part if isinstance(part, str) else _text(value_of(part)) for part in template.parts
        )
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, Any], value)
        return {key: _render(item, value_of) for key, item in mapping.items()}
    if isinstance(value, list):
        return [_render(item, value_of) for item in cast(list[Any], value)]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("an argument is not a finite number")
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
