"""Reading and checking a recipe file.

:func:`load_recipe` reads a TOML file and returns a :class:`~memini.recipes.model.Recipe`, or
raises :class:`RecipeFileError` (the file cannot be read as TOML) or :class:`RecipeError` (it can,
but is not a valid recipe). :func:`check_recipe` returns every problem instead of raising.

A check runs in three steps, and each problem is a :class:`RecipeIssue` that names the field by
its path in the file (``list.paging.next``, ``list.arguments.channel``) and, when the file's text
is at hand, its line:

1. ``format`` first: a file of another format is not read further, as its keys may mean other
   things. TOML dates and times are refused wherever they are: a recipe writes times as strings.
2. The model (:mod:`memini.recipes.model`): required keys, types (no coercion: ``"2"`` is not a
   number), ranges, unknown keys, durations, time formats and zones.
3. The rules that span keys, on a recipe that passed step 2: selectors in the language of their
   call's ``decode``, placeholders the call can fill, paging arguments that do not collide with
   fixed ones, and the values each declared check, order and detail rule needs.

No message quotes a value of the file other than the format number.
"""

import json
import math
import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Final, Literal, cast

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from memini.ids import hash_fields
from memini.recipes.model import (
    PAGING_MODES,
    RECIPE_FORMAT,
    CursorPaging,
    DetailCall,
    ListCall,
    NoPaging,
    OffsetPaging,
    PagePaging,
    Recipe,
)
from memini.recipes.selectors import SelectorSyntaxError, parse_path, parse_pattern
from memini.recipes.templates import ITEM_PREFIX, TemplateError, placeholders

__all__ = [
    "MAX_RECIPE_SIZE",
    "RECIPE_DIGEST_DOMAIN",
    "Location",
    "RecipeError",
    "RecipeFileError",
    "RecipeIssue",
    "TomlLocator",
    "check_recipe",
    "load_recipe",
    "parse_recipe",
    "read_recipe",
    "recipe_digest",
]

MAX_RECIPE_SIZE: Final = 1024**2
"""Bytes of a recipe file. A recipe is a page of TOML; anything near this is not one."""

RECIPE_DIGEST_DOMAIN: Final = "memini.recipe.v1"

type Location = tuple[str | int, ...]
"""A place in the file: keys and array positions, ``("list", "paging", "next")``."""

type _Role = Literal["value", "items", "flag"]


@dataclass(frozen=True, slots=True)
class RecipeIssue:
    """One problem of a recipe: where (``location``, ``line``) and what (``message``)."""

    location: Location
    message: str
    line: int | None = None
    """1-based, when the file's text was at hand and the field was found in it."""

    @property
    def field(self) -> str:
        """The location as text: ``list.paging.next``, ``server.versions[0]``; empty for the
        whole file."""
        return format_location(self.location)

    def __str__(self) -> str:
        return f"{self.field}: {self.message}" if self.location else self.message


class RecipeError(ValueError):
    """The recipe is not valid. ``issues`` holds every problem found, in order."""

    def __init__(self, issues: Sequence[RecipeIssue], path: Path | None = None) -> None:
        self.issues = tuple(issues)
        self.path = path
        prefix = f"{path}: " if path is not None else ""
        super().__init__(prefix + "; ".join(str(issue) for issue in self.issues))


class RecipeFileError(Exception):
    """The file cannot be read as TOML: missing, unreadable, too large, not UTF-8, not TOML."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def format_location(location: Location) -> str:
    text = ""
    for part in location:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else part
    return text


# --- Entry points --------------------------------------------------------------------------------


def read_recipe(path: Path) -> tuple[str, dict[str, Any]]:
    """The text of a recipe file and its TOML, unchecked. Raises :class:`RecipeFileError`."""
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_RECIPE_SIZE + 1)
    except OSError as error:
        raise RecipeFileError(path, f"cannot read the file: {error.strerror or error}") from None
    if len(raw) > MAX_RECIPE_SIZE:
        raise RecipeFileError(path, f"larger than {MAX_RECIPE_SIZE} bytes; not a recipe")
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        raise RecipeFileError(path, "not UTF-8 text") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise RecipeFileError(path, f"not valid TOML: {error}") from None
    return text, data


def load_recipe(path: Path) -> Recipe:
    """Read and check the recipe at ``path``.

    Raises :class:`RecipeFileError` or :class:`RecipeError`.
    """
    text, data = read_recipe(path)
    recipe, issues = check_recipe(data, text)
    if recipe is None or issues:
        raise RecipeError(issues, path)
    return recipe


def parse_recipe(data: Mapping[str, Any], text: str | None = None) -> Recipe:
    """Check a recipe already read as TOML (or built as a dict); raises :class:`RecipeError`."""
    recipe, issues = check_recipe(data, text)
    if recipe is None or issues:
        raise RecipeError(issues)
    return recipe


def check_recipe(
    data: Mapping[str, Any], text: str | None = None
) -> tuple[Recipe | None, list[RecipeIssue]]:
    """Every problem of a recipe, and the recipe when there are none.

    ``text``, the TOML the data was read from, gives each issue its line.
    """
    issues = _check_format(data)
    if not issues:
        timeless = list(_times(data, ()))
        issues = [
            RecipeIssue(location, "a TOML date or time; write the time as a string in quotes")
            for location in timeless
        ]
        recipe: Recipe | None = None
        try:
            recipe = Recipe.model_validate(data)
        except ValidationError as error:
            for details in error.errors(include_url=False, include_input=False):
                issue = _from_pydantic(details, data)
                if not any(issue.location[: len(t)] == t for t in timeless):
                    issues.append(issue)
        if recipe is not None and not issues:
            issues = _Rules(recipe).check()
    else:
        recipe = None
    if text is not None:
        locator = TomlLocator(text)
        issues = [
            RecipeIssue(issue.location, issue.message, locator.line(issue.location))
            for issue in issues
        ]
    return (recipe if not issues else None), issues


def recipe_digest(recipe: Recipe) -> str:
    """A digest of what the recipe means: the same for two files that differ only in comments,
    layout, key order or defaults written out. 64 lowercase hex characters.

    The executors record it with each run, so a record can be traced to the recipe that read it.
    """
    data = recipe.model_dump(mode="json", by_alias=True)
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hash_fields(RECIPE_DIGEST_DOMAIN, text.encode())


# --- Step 1: format and TOML times ---------------------------------------------------------------


def _check_format(data: Mapping[str, Any]) -> list[RecipeIssue]:
    if "format" not in data:
        return [RecipeIssue(("format",), f"required: the recipe format, {RECIPE_FORMAT}")]
    value = data["format"]
    if not isinstance(value, int) or isinstance(value, bool):
        return [RecipeIssue(("format",), f"an integer: the recipe format, {RECIPE_FORMAT}")]
    if value != RECIPE_FORMAT:
        return [
            RecipeIssue(
                ("format",),
                f"format {value} is not supported; this build reads format {RECIPE_FORMAT}",
            )
        ]
    return []


def _times(value: Any, location: Location) -> Iterator[Location]:
    if isinstance(value, datetime | date | time):
        yield location
    elif isinstance(value, Mapping):
        for key, item in cast(Mapping[str, Any], value).items():
            yield from _times(item, (*location, key))
    elif isinstance(value, list):
        for index, item in enumerate(cast(list[Any], value)):
            yield from _times(item, (*location, index))


# --- Step 2: the model ---------------------------------------------------------------------------

_TYPE_WORDS: Final = {
    "string_type": "must be a string",
    "int_type": "must be an integer",
    "float_type": "must be a number",
    "bool_type": "must be true or false",
    "dict_type": "must be a table",
    "model_type": "must be a table",
    "model_attributes_type": "must be a table",
    "list_type": "must be an array",
}


def _from_pydantic(details: ErrorDetails, data: Mapping[str, Any]) -> RecipeIssue:
    """An issue from one pydantic error: its location as keys of the file, a short message."""
    location, branch = _file_location(details["loc"], data)
    kind = details["type"]
    ctx: dict[str, Any] = dict(details.get("ctx") or {})
    if kind == "missing":
        message = "required" + (f" for mode = {branch}" if branch else "")
    elif kind == "extra_forbidden":
        message = "unknown key" + (f" for mode = {branch}" if branch else "")
    elif kind in ("union_tag_not_found", "union_tag_invalid"):
        # The one union of the model is the paging rule, told apart by its mode.
        location = (*location, "mode")
        modes = ", ".join(PAGING_MODES)
        message = (
            f"required: one of {modes}"
            if kind == "union_tag_not_found"
            else (f"must be one of {modes}")
        )
    elif kind == "value_error" and "error" in ctx:
        message = str(ctx["error"])
    elif kind in _TYPE_WORDS:
        message = _TYPE_WORDS[kind]
    else:
        message = details["msg"]
        for prefix in ("Input should be ", "Value should be "):
            if message.startswith(prefix):
                message = "must be " + message.removeprefix(prefix)
        message = message[:1].lower() + message[1:]
    return RecipeIssue(location, message)


def _file_location(loc: Sequence[int | str], data: Mapping[str, Any]) -> tuple[Location, str]:
    """Pydantic's location with what is not a key of the file left out, and the union branch
    it passed through, if any.

    Pydantic puts the tag of a union's branch into the location (``list.paging.cursor.next``)
    and ``[key]`` for an error in a key; the file has neither. Walking the data tells them apart:
    a step that is not in the data is kept only as the last one (a missing key).
    """
    parts: list[str | int] = []
    branch = ""
    current: Any = data
    for position, element in enumerate(loc):
        last = position == len(loc) - 1
        if isinstance(element, int):
            parts.append(element)
            items = cast(list[Any], current) if isinstance(current, list) else []
            current = items[element] if -len(items) <= element < len(items) else None
        elif element == "[key]":
            continue
        elif isinstance(current, Mapping) and element in current:
            parts.append(element)
            current = cast(Mapping[str, Any], current)[element]
        elif last:
            parts.append(element)
        else:
            branch = element
    return tuple(parts), branch


# --- Step 3: rules that span keys ----------------------------------------------------------------


class _Rules:
    """The checks of a recipe the model passed. :meth:`check` returns their issues in order."""

    def __init__(self, recipe: Recipe) -> None:
        self.recipe = recipe
        self.issues: list[RecipeIssue] = []

    def add(self, location: Location, message: str) -> None:
        self.issues.append(RecipeIssue(location, message))

    def check(self) -> list[RecipeIssue]:
        recipe = self.recipe
        if recipe.stream.key_pattern is not None:
            try:
                re.compile(recipe.stream.key_pattern)
            except re.error as error:
                self.add(("stream", "key_pattern"), f"not a valid regular expression: {error.msg}")
        self._list(recipe.listing)
        if recipe.detail is not None:
            self._detail(recipe.detail, recipe.listing)
        self._checks()
        self._finite(recipe.parser.options, ("parser", "options"))
        return self.issues

    # -- The list call

    def _list(self, call: ListCall) -> None:
        where: Location = ("list",)
        decode = call.decode
        self._selector((*where, "items"), call.items, decode, "items", "$")
        item = call.item
        at: Location = (*where, "item")
        self._selector((*at, "id"), item.id, decode, "value", "@")
        if item.timestamp is not None:
            self._selector((*at, "timestamp"), item.timestamp, decode, "value", "@")
        else:
            if "timestamp_format" in item.model_fields_set:
                self.add((*at, "timestamp_format"), "needs list.item.timestamp")
            if item.timezone is not None:
                self.add((*at, "timezone"), "needs list.item.timestamp")
        if item.timezone is not None and item.timestamp_format in ("unix", "unix_ms"):
            self.add((*at, "timezone"), "a unix timestamp has no zone to supply")
        if item.reply_count is not None:
            self._selector((*at, "reply_count"), item.reply_count, decode, "value", "@")
        for name, selector in item.fields.items():
            self._selector((*at, "fields", name), selector, decode, "value", "@")
        if call.order != "any" and item.timestamp is None:
            self.add((*where, "order"), f"{call.order} needs list.item.timestamp")
        self._paging((*where, "paging"), call.paging, call.arguments, decode)
        self._arguments(where, call.arguments, frozenset({"stream", "from", "to"}))

    # -- The detail call

    def _detail(self, call: DetailCall, listing: ListCall) -> None:
        where: Location = ("detail",)
        decode = call.decode
        if call.items is not None:
            self._selector((*where, "items"), call.items, decode, "items", "$")
        elif call.includes_root:
            self.add((*where, "includes_root"), "needs detail.items")
        item = listing.item
        if call.when == "has_replies" and item.reply_count is None:
            self.add((*where, "when"), "has_replies needs list.item.reply_count")
        self._paging((*where, "paging"), call.paging, call.arguments, decode)
        names = {"id"} | set(item.fields)
        if item.timestamp is not None:
            names.add("timestamp")
        if item.reply_count is not None:
            names.add("reply_count")
        available = frozenset({"stream", "from", "to"} | {ITEM_PREFIX + n for n in names})
        used = self._arguments(where, call.arguments, available)
        if not any(name.startswith(ITEM_PREFIX) for name in used) and not any(
            issue.location[:2] == (*where, "arguments") for issue in self.issues
        ):
            self.add(
                (*where, "arguments"),
                "names no listed item: a detail call is made per item, with {item.id} or"
                " another {item.NAME} in its arguments",
            )

    # -- Shared parts

    def _selector(
        self, location: Location, text: str, decode: str, role: _Role, root: Literal["$", "@"]
    ) -> None:
        message = _selector_problem(text, decode, role, root)
        if message is not None:
            self.add(location, message)

    def _paging(
        self,
        location: Location,
        paging: CursorPaging | PagePaging | OffsetPaging | NoPaging,
        arguments: Mapping[str, Any],
        decode: str,
    ) -> None:
        if isinstance(paging, NoPaging):
            return
        call = location[0]
        for key in ("argument", "size_argument"):
            name = getattr(paging, key, None)
            if name is not None and name in arguments:
                self.add(
                    (*location, key),
                    f"{call}.arguments sets this argument too; the paging rule sets it on each"
                    " call, so leave it out of the arguments",
                )
        if isinstance(paging, CursorPaging):
            self._selector((*location, "next"), paging.next, decode, "value", "$")
        else:
            if paging.size_argument is not None:
                if paging.size_argument == paging.argument:
                    self.add((*location, "size_argument"), "the same argument as argument")
                if paging.size is None:
                    self.add((*location, "size"), "required with size_argument")
            if isinstance(paging, PagePaging):
                for key in ("number", "total_pages"):
                    selector = cast(str | None, getattr(paging, key))
                    if selector is not None:
                        self._selector((*location, key), selector, decode, "value", "$")
            if paging.total is not None:
                self._selector((*location, "total"), paging.total, decode, "value", "$")
        if paging.has_more is not None:
            self._selector((*location, "has_more"), paging.has_more, decode, "flag", "$")

    def _arguments(
        self, where: Location, arguments: Mapping[str, Any], available: frozenset[str]
    ) -> set[str]:
        """Check the placeholders of a call's arguments; return the names they use."""
        location: Location = (*where, "arguments")
        self._finite(arguments, location)
        try:
            found = placeholders(arguments)
        except TemplateError as error:
            self.add((*location, *error.path), str(error))
            return set()
        used: set[str] = set()
        for path, placeholder in found:
            used.add(placeholder.name)
            if placeholder.name not in available:
                self.add((*location, *path), _unavailable(placeholder.name, where[0]))
        return used

    def _finite(self, value: Any, location: Location) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            self.add(location, "not a finite number: nan and inf are not JSON")
        elif isinstance(value, Mapping):
            for key, item in cast(Mapping[str, Any], value).items():
                self._finite(item, (*location, key))
        elif isinstance(value, list):
            for index, item in enumerate(cast(list[Any], value)):
                self._finite(item, (*location, index))

    # -- Checks

    def _checks(self) -> None:
        recipe = self.recipe
        checks = recipe.checks
        pagings: list[CursorPaging | PagePaging | OffsetPaging | NoPaging] = [recipe.listing.paging]
        if recipe.detail is not None:
            pagings.append(recipe.detail.paging)
        if checks.page_numbers and not any(
            isinstance(p, PagePaging) and p.number is not None for p in pagings
        ):
            self.add(("checks", "page_numbers"), "needs page paging with number")
        if checks.totals and not any(
            isinstance(p, PagePaging | OffsetPaging) and p.total is not None for p in pagings
        ):
            self.add(("checks", "totals"), "needs page or offset paging with total")
        if checks.reply_counts:
            missing: list[str] = []
            if recipe.listing.item.reply_count is None:
                missing.append("list.item.reply_count")
            if recipe.detail is None or recipe.detail.items is None:
                missing.append("detail.items")
            if missing:
                self.add(("checks", "reply_counts"), f"needs {' and '.join(missing)}")


def _selector_problem(text: str, decode: str, role: _Role, root: Literal["$", "@"]) -> str | None:
    """Why ``text`` is not a selector of ``role`` in a call that decodes as ``decode``."""
    if decode == "text":
        try:
            pattern = parse_pattern(text)
        except SelectorSyntaxError as error:
            return str(error)
        if role == "value" and pattern.groups != 1:
            return (
                "a pattern that reads a value has exactly one capturing group; write (?:...)"
                " for a group that captures nothing"
            )
        return None
    try:
        path = parse_path(text)
    except SelectorSyntaxError as error:
        return f"{error} (decode = {decode} reads selectors as paths)"
    if path.root != root:
        return (
            "a value of a listed item is a path from the item: it starts with @"
            if root == "@"
            else "a path from the response starts with $"
        )
    if role == "items" and not path.many:
        return "selects the items: a path with [*] or .*, such as $.messages[*]"
    if role != "items" and path.many:
        return "selects one value: a path without [*] or .*"
    return None


def _unavailable(name: str, call: str | int) -> str:
    if name.startswith(ITEM_PREFIX):
        if call == "list":
            return f"{{{name}}} is for detail calls; the list call has no item yet"
        return (
            f"{{{name}}} is not a value of the listed item; declare it in list.item"
            " (id, timestamp, reply_count, or a name in fields)"
        )
    return f"{{{name}}} is not a placeholder; use stream, from, to or item.NAME"


# --- Lines ---------------------------------------------------------------------------------------

_KEY_PART: Final = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')"""
_KEY_LINE: Final = re.compile(rf"\s*({_KEY_PART}(?:\s*\.\s*{_KEY_PART})*)\s*=")
_KEY_PARTS: Final = re.compile(_KEY_PART)
_TABLE: Final = re.compile(rf"\s*\[\s*({_KEY_PART}(?:\s*\.\s*{_KEY_PART})*)\s*\]\s*(?:#.*)?")
_ARRAY_TABLE: Final = re.compile(
    rf"\s*\[\[\s*({_KEY_PART}(?:\s*\.\s*{_KEY_PART})*)\s*\]\]\s*(?:#.*)?"
)


class TomlLocator:
    """The line of each key of a TOML text, found by a scan of its lines.

    Tables (``[list.paging]``), arrays of tables and ``key = value`` lines, dotted keys included,
    are found. A key inside an inline table or a multi-line array is not: :meth:`line` then gives
    the line of the closest enclosing key, which for an inline table is the same line.
    """

    def __init__(self, text: str) -> None:
        self._lines: dict[Location, int] = {}
        self._arrays: dict[Location, int] = {}
        self._open_string: str | None = None
        self._depth = 0
        prefix: Location = ()
        for number, line in enumerate(text.splitlines(), start=1):
            if self._open_string is not None or self._depth > 0:
                self._scan_value(line, 0)
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("["):
                prefix = self._header(line, number, prefix)
                continue
            match = _KEY_LINE.match(line)
            if match is None:
                continue
            keys = _key_parts(match.group(1))
            for end in range(1, len(keys) + 1):
                self._lines.setdefault((*prefix, *keys[:end]), number)
            self._scan_value(line, match.end())

    def line(self, location: Location) -> int | None:
        """The line of ``location``, or of its closest enclosing key; ``None`` if neither."""
        for end in range(len(location), 0, -1):
            found = self._lines.get(location[:end])
            if found is not None:
                return found
        return None

    def _header(self, line: str, number: int, prefix: Location) -> Location:
        array = _ARRAY_TABLE.fullmatch(line)
        if array is not None:
            keys = _key_parts(array.group(1))
            position = self._arrays.get(keys, 0)
            self._arrays[keys] = position + 1
            self._lines.setdefault(keys, number)
            table = (*keys, position)
            self._lines.setdefault(table, number)
            return table
        match = _TABLE.fullmatch(line)
        if match is None:
            return prefix
        keys = _key_parts(match.group(1))
        for end in range(1, len(keys) + 1):
            self._lines.setdefault(keys[:end], number)
        return keys

    def _scan_value(self, line: str, start: int) -> None:
        """Follow strings, comments and brackets so that lines inside a multi-line string or
        array are not read as keys."""
        index = start
        while index < len(line):
            if self._open_string is not None:
                end = line.find(self._open_string, index)
                if end < 0:
                    return
                index = end + 3
                self._open_string = None
                continue
            char = line[index]
            if char == "#":
                return
            if line.startswith('"""', index) or line.startswith("'''", index):
                self._open_string = line[index : index + 3]
                index += 3
            elif char == '"':
                index += 1
                while index < len(line) and line[index] != '"':
                    index += 2 if line[index] == "\\" else 1
                index += 1
            elif char == "'":
                end = line.find("'", index + 1)
                index = end + 1 if end >= 0 else len(line)
            else:
                if char in "[{":
                    self._depth += 1
                elif char in "]}":
                    self._depth = max(0, self._depth - 1)
                index += 1


def _key_parts(dotted: str) -> tuple[str, ...]:
    parts: list[str] = []
    for part in _KEY_PARTS.findall(dotted):
        if part.startswith('"'):
            try:
                parts.append(str(json.loads(part)))
            except ValueError:
                parts.append(part[1:-1])
        elif part.startswith("'"):
            parts.append(part[1:-1])
        else:
            parts.append(part)
    return tuple(parts)
