"""MCP recipes (requirements, section 4): a declarative file per MCP server that says how to
sync it.

A recipe is a TOML file (format 1, docs/recipes.md is the reference). It names the server and how
to check it has not drifted, what a stream key is and what kind of record a stream's items
become, how the planner cuts windows (overlap, initial load, backfill), the list call (tool,
window arguments, paging rule, where each item's id, time and reply count are), the optional
detail call per item, completeness checks, the rate budget and the name of a registered
deterministic parser. No model reads anything in the sync loop (principle 5): every value the
executors need is found by a selector, a path into JSON or a pattern over text.

This package reads and checks recipes, and gives the executors the parts of the format they
evaluate:

- :mod:`memini.recipes.model`: the recipe as pydantic models, and its JSON Schema
  (``schemas/recipe.schema.json``).
- :mod:`memini.recipes.validate`: :func:`load_recipe` and :func:`check_recipe`, with issues that
  name the field and its line; :func:`recipe_digest`.
- :mod:`memini.recipes.selectors`: paths and patterns.
- :mod:`memini.recipes.templates`: argument placeholders and their rendering.
- :mod:`memini.recipes.times`: durations, window bound formats, timestamp formats.
- :mod:`memini.recipes.calls`: a window's stored calls (:class:`RawResponse`,
  :class:`WindowInput`) and the calls that would complete it (:class:`MissingCall`).
- :mod:`memini.recipes.reading`: a window read with its recipe's selectors, its items and the
  completeness checks (:func:`read_window`, :func:`missing_calls`).
- :mod:`memini.recipes.parsing`: the parser protocol (a window's raw responses in, records and
  completeness out), item parsers, the registry of parsers by name, and :func:`parse_window`.

``memini recipe validate`` and ``memini recipe schema`` (:mod:`memini.cli.recipes`) are the
commands; both read files only.
"""

from memini.recipes.calls import (
    CHECKS,
    CallKind,
    Check,
    MissingCall,
    RawResponse,
    ResponseDecodeError,
    WindowInput,
)
from memini.recipes.model import (
    RECIPE_FORMAT,
    Checks,
    CursorPaging,
    Decode,
    DetailCall,
    ListCall,
    ListItem,
    NoPaging,
    OffsetPaging,
    Order,
    PagePaging,
    Paging,
    Parser,
    Rate,
    Recipe,
    Server,
    Stream,
    Window,
    recipe_json_schema,
)
from memini.recipes.parsing import (
    IncompleteItem,
    ItemParser,
    ParsedRecord,
    ParsedWindow,
    ParserError,
    ParserNotFoundError,
    RecipeParser,
    WindowParser,
    get_parser,
    item_parser,
    parse_window,
    register_parser,
    registered_parsers,
    unregister_parser,
)
from memini.recipes.reading import (
    DetailPage,
    Item,
    ListedItem,
    WindowReading,
    item_key,
    missing_calls,
    read_window,
    repeat_call,
)
from memini.recipes.selectors import (
    JsonPath,
    SelectorSyntaxError,
    TextPattern,
    parse_path,
    parse_pattern,
)
from memini.recipes.templates import (
    Placeholder,
    Template,
    TemplateError,
    parse_template,
    placeholders,
    render_arguments,
)
from memini.recipes.times import format_bound, parse_duration, parse_timestamp
from memini.recipes.validate import (
    RecipeError,
    RecipeFileError,
    RecipeIssue,
    check_recipe,
    load_recipe,
    parse_recipe,
    read_recipe,
    recipe_digest,
)

__all__ = [
    "CHECKS",
    "RECIPE_FORMAT",
    "CallKind",
    "Check",
    "Checks",
    "CursorPaging",
    "Decode",
    "DetailCall",
    "DetailPage",
    "IncompleteItem",
    "Item",
    "ItemParser",
    "JsonPath",
    "ListCall",
    "ListItem",
    "ListedItem",
    "MissingCall",
    "NoPaging",
    "OffsetPaging",
    "Order",
    "PagePaging",
    "Paging",
    "ParsedRecord",
    "ParsedWindow",
    "Parser",
    "ParserError",
    "ParserNotFoundError",
    "Placeholder",
    "Rate",
    "RawResponse",
    "Recipe",
    "RecipeError",
    "RecipeFileError",
    "RecipeIssue",
    "RecipeParser",
    "ResponseDecodeError",
    "SelectorSyntaxError",
    "Server",
    "Stream",
    "Template",
    "TemplateError",
    "TextPattern",
    "Window",
    "WindowInput",
    "WindowParser",
    "WindowReading",
    "check_recipe",
    "format_bound",
    "get_parser",
    "item_key",
    "item_parser",
    "load_recipe",
    "missing_calls",
    "parse_duration",
    "parse_path",
    "parse_pattern",
    "parse_recipe",
    "parse_template",
    "parse_timestamp",
    "parse_window",
    "placeholders",
    "read_recipe",
    "read_window",
    "recipe_digest",
    "recipe_json_schema",
    "register_parser",
    "registered_parsers",
    "render_arguments",
    "repeat_call",
    "unregister_parser",
]
