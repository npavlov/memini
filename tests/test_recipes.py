"""Tests for the recipe format: the model, the checks, selectors, placeholders, times, the JSON
Schema and ``memini recipe``. No database. Every server, tool and value here is invented."""

import argparse
import asyncio
import io
import json
import re
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memini.cli import recipes as recipe_cli
from memini.recipes import (
    CursorPaging,
    PagePaging,
    Recipe,
    RecipeError,
    RecipeFileError,
    SelectorSyntaxError,
    TemplateError,
    check_recipe,
    format_bound,
    load_recipe,
    parse_duration,
    parse_path,
    parse_pattern,
    parse_recipe,
    parse_template,
    parse_timestamp,
    placeholders,
    recipe_digest,
    recipe_json_schema,
    render_arguments,
)
from memini.recipes.schema import schema_text
from memini.recipes.times import check_bound_format, check_timestamp_format, format_duration
from memini.recipes.validate import TomlLocator

ROOT = Path(__file__).resolve().parent.parent
RECIPES = ROOT / "tests" / "fixtures" / "recipes"
EXAMPLES = ("hearth", "millnotes", "quillpost")


def example_text(name: str) -> str:
    return (RECIPES / f"{name}.toml").read_text()


def issues_of(text: str) -> list[tuple[str, str, int | None]]:
    recipe, issues = check_recipe(tomllib.loads(text), text)
    assert (recipe is None) == bool(issues)
    return [(issue.field, issue.message, issue.line) for issue in issues]


def edit(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"the fixture holds {old!r} {text.count(old)} times"
    return text.replace(old, new)


# --- Valid recipes -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXAMPLES)
def test_examples_are_valid(name: str) -> None:
    recipe = load_recipe(RECIPES / f"{name}.toml")
    assert recipe.name == name
    assert recipe.format == 1


def test_hearth_reads_as_written() -> None:
    recipe = load_recipe(RECIPES / "hearth.toml")
    assert recipe.tools == ("channel_history", "thread_replies")
    assert recipe.stream.kind == "thread"
    assert recipe.window.overlap == timedelta(days=2)
    assert recipe.window.max_span == timedelta(weeks=1)
    assert recipe.rate.timeout == timedelta(seconds=30)
    assert isinstance(recipe.listing.paging, CursorPaging)
    assert recipe.listing.paging.max_pages == 200
    assert recipe.detail is not None
    assert recipe.detail.when == "has_replies"
    assert recipe.checks.reply_counts
    assert not recipe.checks.page_numbers


def test_defaults_fill_what_is_left_out() -> None:
    recipe = load_recipe(RECIPES / "millnotes.toml")
    assert recipe.description == ""
    assert recipe.server.versions == []
    assert recipe.rate.concurrency == 1
    assert recipe.rate.max_calls == 5000
    assert recipe.rate.timeout == timedelta(seconds=60)
    assert recipe.window.backfill is None
    assert recipe.detail is not None
    assert recipe.detail.paging.mode == "none"
    assert isinstance(recipe.listing.paging, PagePaging)


def test_digest_ignores_layout_comments_and_defaults_written_out() -> None:
    text = example_text("millnotes")
    same = edit(
        text, "[rate]\ncalls_per_minute = 20", "[rate]\nconcurrency = 1\ncalls_per_minute = 20"
    )
    same = "# a comment\n" + same.replace(" = ", "=")
    other = edit(text, "calls_per_minute = 20", "calls_per_minute = 21")
    digest = recipe_digest(parse_recipe(tomllib.loads(text)))
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert recipe_digest(parse_recipe(tomllib.loads(same))) == digest
    assert recipe_digest(parse_recipe(tomllib.loads(other))) != digest


def test_durations_written_differently_mean_the_same() -> None:
    text = example_text("hearth")
    week = parse_recipe(tomllib.loads(text))
    days = parse_recipe(tomllib.loads(edit(text, 'max_span = "7d"', 'max_span = "1w"')))
    assert recipe_digest(week) == recipe_digest(days)


def test_model_validate_alone_does_not_check_the_rules() -> None:
    """The docs say to build recipes with parse_recipe: the model alone lets a bad path pass."""
    data = tomllib.loads(edit(example_text("hearth"), 'id = "@.ts"', 'id = "@.ts["'))
    Recipe.model_validate(data)
    with pytest.raises(RecipeError) as raised:
        parse_recipe(data)
    assert raised.value.issues[0].field == "list.item.id"


# --- Broken recipes ------------------------------------------------------------------------------

LIST_NEXT = 'next = "$.next_cursor"         # missing, null or "" on the last page'
LIST_MODE = (
    'mode = "cursor"\nargument = "cursor"            # sent back on the next call, left out of the'
    " first"
)

# (example, old text, new text, field, a part of the message)
BROKEN: list[tuple[str, str, str, str, str]] = [
    # required keys and types
    ("hearth", 'tool = "channel_history"\n', "", "list.tool", "required"),
    ("hearth", "[parser]\n", "[parsers]\n", "parsers", "unknown key"),
    ("hearth", "concurrency = 2", 'concurrency = "2"', "rate.concurrency", "must be an integer"),
    ("hearth", "includes_root = true", 'includes_root = "yes"', "detail.includes_root", "true"),
    ("hearth", "calls_per_minute = 50", "calls_per_minute = 0", "rate.calls_per_minute", "1"),
    ("hearth", 'name = "hearth"', 'name = "Hearth Chat"', "name", "pattern"),
    ("hearth", 'kind = "thread"', 'kind = "code_file"', "stream.kind", "thread"),
    ("hearth", 'versions = ["2.*"]', 'versions = "2.*"', "server.versions", "array"),
    ("hearth", 'versions = ["2.*"]', 'versions = [""]', "server.versions[0]", "at least 1"),
    ("hearth", 'name = "hearth.thread"', 'name = "Hearth/Thread"', "parser.name", "pattern"),
    # unknown keys
    (
        "hearth",
        "max_pages = 200",
        'max_pages = 200\nnext_token = "$.x"',
        "list.paging.next_token",
        "unknown key",
    ),
    (
        "hearth",
        'reply_count = "@.reply_count"',
        'reply_count = "@.reply_count"\nreplies = "@.r"',
        "list.item.replies",
        "unknown key",
    ),
    # times
    ("hearth", 'overlap = "2d"', 'overlap = "2 days"', "window.overlap", "a duration"),
    ("hearth", 'overlap = "2d"', 'overlap = "0d"', "window.overlap", "longer than zero"),
    ("hearth", 'overlap = "2d"', "overlap = 2", "window.overlap", "a duration"),
    ("hearth", 'timeout = "30s"', "timeout = 2026-11-18T10:00:00Z", "rate.timeout", "TOML date"),
    (
        "hearth",
        'timestamp_format = "unix"',
        'timestamp_format = "epoch"',
        "list.item.timestamp_format",
        "rfc3339",
    ),
    (
        "hearth",
        'timestamp_format = "unix"',
        'timestamp_format = "%d %b %Y"',
        "list.item.timestamp_format",
        "%b is not allowed",
    ),
    (
        "millnotes",
        'timezone = "Europe/Lisbon"',
        'timezone = "Mars/Olympus"',
        "list.item.timezone",
        "IANA",
    ),
    (
        "hearth",
        'timestamp_format = "unix"',
        'timestamp_format = "unix"\ntimezone = "UTC"',
        "list.item.timezone",
        "no zone",
    ),
    # selectors
    ("hearth", 'id = "@.ts"', 'id = "@.ts[x]"', "list.item.id", "not a valid path at character 6"),
    ("hearth", 'id = "@.ts"', 'id = "ts"', "list.item.id", "starts with $ (the response) or @"),
    ("hearth", 'id = "@.ts"', 'id = "$.ts"', "list.item.id", "it starts with @"),
    (
        "hearth",
        'items = "$.messages[*]"\norder',
        'items = "$.messages"\norder',
        "list.items",
        "[*]",
    ),
    ("hearth", LIST_NEXT, 'next = "$.pages[*].cursor"', "list.paging.next", "one value"),
    ("hearth", LIST_NEXT, 'next = "@.next_cursor"', "list.paging.next", "starts with $"),
    (
        "quillpost",
        "id = 'Thread (\\S+)'",
        "id = 'Thread (\\S+'",
        "list.item.id",
        "not a valid regular expression",
    ),
    (
        "quillpost",
        "id = 'Thread (\\S+)'",
        "id = 'Thread \\S+'",
        "list.item.id",
        "exactly one capturing group",
    ),
    (
        "quillpost",
        "id = 'Thread (\\S+)'",
        "id = '(Thread) (\\S+)'",
        "list.item.id",
        "exactly one capturing group",
    ),
    (
        "hearth",
        'key_pattern = "C[0-9]{4,8}"',
        'key_pattern = "C[0-9"',
        "stream.key_pattern",
        "regular expression",
    ),
    # paging rules
    (
        "hearth",
        LIST_NEXT,
        'total_pages = "$.pages"\nnext = "$.next_cursor"',
        "list.paging.total_pages",
        "unknown key for mode = cursor",
    ),
    ("hearth", LIST_NEXT, "", "list.paging.next", "required for mode = cursor"),
    (
        "hearth",
        LIST_MODE,
        'argument = "cursor"',
        "list.paging.mode",
        "required: one of cursor, page, offset, none",
    ),
    (
        "hearth",
        LIST_MODE,
        'mode = "token"\nargument = "cursor"',
        "list.paging.mode",
        "one of cursor, page, offset",
    ),
    (
        "hearth",
        "limit = 100 }",
        'limit = 100, cursor = "" }',
        "list.paging.argument",
        "list.arguments sets this argument too",
    ),
    ("millnotes", "size = 25\n", "", "list.paging.size", "required with size_argument"),
    (
        "millnotes",
        'size_argument = "per_page"',
        'size_argument = "page"',
        "list.paging.size_argument",
        "the same argument as argument",
    ),
    ("millnotes", "first = 1", "first = 2", "list.paging.first", "less than or equal to 1"),
    (
        "millnotes",
        "arguments = { include_shared = true }",
        "arguments = { include_shared = true, per_page = 10 }",
        "list.paging.size_argument",
        "list.arguments sets this argument too",
    ),
    # placeholders
    (
        "hearth",
        'latest = "{to:unix}"',
        'latest = "{until}"',
        "list.arguments.latest",
        "{until} is not a placeholder",
    ),
    (
        "hearth",
        'latest = "{to:unix}"',
        'latest = "{until:unix}"',
        "list.arguments.latest",
        "only {from} and {to} take a format",
    ),
    (
        "hearth",
        'latest = "{to:unix}"',
        'latest = "{to:%b}"',
        "list.arguments.latest",
        "%b is not allowed",
    ),
    (
        "hearth",
        'latest = "{to:unix}"',
        'latest = "{to:seconds}"',
        "list.arguments.latest",
        "a bound format is one of",
    ),
    (
        "hearth",
        'latest = "{to:unix}"',
        'latest = "{to:unix"',
        "list.arguments.latest",
        "not closed",
    ),
    ("hearth", 'latest = "{to:unix}"', 'latest = "a } b"', "list.arguments.latest", "a lone '}'"),
    (
        "hearth",
        'latest = "{to:unix}"',
        'latest = "{item.id}"',
        "list.arguments.latest",
        "is for detail calls",
    ),
    (
        "hearth",
        'thread = "{item.id}"',
        'thread = "{item.channel}"',
        "detail.arguments.thread",
        "not a value of the listed item",
    ),
    (
        "hearth",
        'thread = "{item.id}"',
        'thread = "first"',
        "detail.arguments",
        "names no listed item",
    ),
    ("hearth", "limit = 100 }", "limit = nan }", "list.arguments.limit", "not a finite number"),
    (
        "hearth",
        "options = { message_url",
        "options = { weight = inf, message_url",
        "parser.options.weight",
        "not a finite number",
    ),
    # rules that need other keys
    (
        "hearth",
        'reply_count = "@.reply_count"\n',
        "",
        "detail.when",
        "has_replies needs list.item.reply_count",
    ),
    (
        "hearth",
        "reply_counts = true",
        "page_numbers = true",
        "checks.page_numbers",
        "needs page paging with number",
    ),
    (
        "hearth",
        "reply_counts = true",
        "totals = true",
        "checks.totals",
        "needs page or offset paging with total",
    ),
    (
        "quillpost",
        "items = '(?m)^--- message \\d+ ---$'\n",
        "",
        "checks.reply_counts",
        "needs detail.items",
    ),
    ("hearth", 'timestamp = "@.ts"\n', "", "list.order", "newest_first needs list.item.timestamp"),
    ("millnotes", 'number = "$.page"\n', "", "checks.page_numbers", "needs page paging"),
    ("quillpost", "total = 'Total: (\\d+) threads'", "", "checks.totals", "with total"),
    # format
    ("hearth", "format = 1", "format = 2", "format", "format 2 is not supported"),
    ("hearth", "format = 1", 'format = "1"', "format", "an integer"),
    ("hearth", "format = 1\n", "", "format", "required"),
]


@pytest.mark.parametrize(
    ("example", "old", "new", "field", "part"),
    BROKEN,
    ids=[f"{case[3]}:{case[4][:24]}" for case in BROKEN],
)
def test_broken_recipe_names_the_field(
    example: str, old: str, new: str, field: str, part: str
) -> None:
    found = issues_of(edit(example_text(example), old, new))
    assert any(f == field and part in message for f, message, _ in found), found


def test_every_problem_is_reported_at_once() -> None:
    text = example_text("hearth")
    text = edit(text, "concurrency = 2", 'concurrency = "2"')
    text = edit(text, 'tool = "channel_history"\n', "")
    text = edit(text, "max_pages = 200", "max_pages = 200\nturbo = true")
    fields = [field for field, _, _ in issues_of(text)]
    assert fields == ["list.tool", "list.paging.turbo", "rate.concurrency"]


def test_issue_lines_point_into_the_file() -> None:
    text = example_text("hearth")
    lines = text.splitlines()
    broken = edit(text, 'id = "@.ts"', 'id = "$.ts"')
    [(field, _, line)] = issues_of(broken)
    assert field == "list.item.id"
    assert line is not None
    assert lines[line - 1].startswith("id = ")

    missing = edit(text, 'tool = "channel_history"\n', "")
    [(_, _, line)] = issues_of(missing)
    assert line is not None
    assert missing.splitlines()[line - 1] == "[list]"  # a missing key: the line of its table

    inline = edit(text, 'latest = "{to:unix}"', 'latest = "{to:%b}"')
    [(_, _, line)] = issues_of(inline)
    assert line is not None
    assert inline.splitlines()[line - 1].startswith("arguments = {")


def test_messages_never_quote_values() -> None:
    marker = "zq-marker-7"
    text = edit(example_text("hearth"), 'id = "@.ts"', f'id = "{marker}"')
    text = edit(text, 'overlap = "2d"', f'overlap = "{marker}"')
    text = edit(text, 'latest = "{to:unix}"', f'latest = "{{to:%b{marker}}}"')
    found = issues_of(text)
    assert found
    assert all(marker not in message for _, message, _ in found)


def test_rules_wait_for_the_model() -> None:
    """A recipe the model refuses gets no rule issues, which would refer to keys it lacks."""
    text = edit(example_text("hearth"), 'tool = "channel_history"\n', "")
    text = edit(text, 'id = "@.ts"', 'id = "$.ts"')
    assert [field for field, _, _ in issues_of(text)] == ["list.tool"]


def test_load_recipe_raises() -> None:
    with pytest.raises(RecipeFileError, match="cannot read"):
        load_recipe(RECIPES / "absent.toml")


# --- Lines ---------------------------------------------------------------------------------------


def test_locator_finds_tables_keys_and_arrays_of_tables() -> None:
    text = "\n".join(
        [
            "top = 1                     # 1",
            "[a]                         # 2",
            "b.c = 2                     # 3",
            "list = [                    # 4",
            '  "x = 3",                  # 5',
            "  { d = 4 },                # 6",
            "]                           # 7",
            'text = """                  # 8',
            "e = 5                       # 9: inside a string",
            '"""                         # 10',
            "[[runs]]                    # 11",
            "f = 6                       # 12",
            "[[runs]]                    # 13",
            "f = 7                       # 14",
            '["quoted key".g]            # 15',
            "h = 8                       # 16",
        ]
    )
    locator = TomlLocator(text)
    assert locator.line(("top",)) == 1
    assert locator.line(("a",)) == 2
    assert locator.line(("a", "b", "c")) == 3
    assert locator.line(("a", "list", 1, "d")) == 4
    assert locator.line(("a", "e")) == 2  # not the line inside the string
    assert locator.line(("runs", 0, "f")) == 12
    assert locator.line(("runs", 1, "f")) == 14
    assert locator.line(("runs", 1)) == 13
    assert locator.line(("quoted key", "g", "h")) == 16
    assert locator.line(("nowhere",)) is None


# --- Selectors -----------------------------------------------------------------------------------

DOCUMENT: dict[str, Any] = {
    "messages": [
        {"ts": "1.1", "reply_count": 2, "user": {"name": "ana"}},
        {"ts": "2.2", "reply_count": 0},
    ],
    "meta": {"next cursor": "c2", "a.b": True},
    "channels": {"C1": {"n": 1}, "C2": {"n": 2}},
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$", [DOCUMENT]),
        ("$.messages[*].ts", ["1.1", "2.2"]),
        ("$.messages[0].user.name", ["ana"]),
        ("$.messages[-1].ts", ["2.2"]),
        ("$.messages[5].ts", []),
        ("$.messages[*].user.name", ["ana"]),
        ("$['meta'][\"next cursor\"]", ["c2"]),
        ("$.meta['a.b']", [True]),
        ("$.channels.*.n", [1, 2]),
        ("$.channels[*].n", [1, 2]),
        ("$.absent.deeper", []),
        ("$.messages.ts", []),
        ("$.meta[0]", []),
    ],
)
def test_path_selects(path: str, expected: list[Any]) -> None:
    assert parse_path(path).select(DOCUMENT) == expected


def test_path_from_the_item_and_many() -> None:
    item = parse_path("@.reply_count")
    assert item.root == "@"
    assert not item.many
    assert parse_path("$.a[*]").many
    assert parse_path("$.a.*.b").many
    assert item.first({"reply_count": 3}) == 3
    assert item.first({}) is None


@pytest.mark.parametrize(
    ("path", "part"),
    [
        ("", "not empty"),
        ("messages", "starts with $"),
        ("$messages", "character 2: expected '.' or '['"),
        ("$.", "expected a key"),
        ("$.1st", "expected a key"),
        ("$[x]", "expected *, an integer or a quoted key"),
        ("$['open", "not closed"),
        ("$['key'x", "expected ']'"),
        ("$[1", "expected *, an integer"),
        ("$" + ".a" * 600, "at most 1000"),
    ],
)
def test_bad_paths(path: str, part: str) -> None:
    with pytest.raises(SelectorSyntaxError, match=re.escape(part)):
        parse_path(path)


def test_patterns() -> None:
    text = "Thread t1 in inbox: 3 messages\nThread t2 in archive: 1 messages\nTotal: 2 threads"
    items = parse_pattern(r"(?m)^Thread \S+ .*$").items(text)
    assert items == ["Thread t1 in inbox: 3 messages", "Thread t2 in archive: 1 messages"]
    assert parse_pattern(r"(\d+) messages").value(items[0]) == "3"
    assert parse_pattern(r"Total: (\d+)").value(text) == "2"
    assert parse_pattern(r"absent (\d+)").value(text) is None
    assert parse_pattern(r"Total").matches(text)
    with pytest.raises(SelectorSyntaxError, match="not a valid regular expression"):
        parse_pattern("(unclosed")


# --- Placeholders --------------------------------------------------------------------------------

WINDOW = (datetime(2026, 11, 17, 9, 30, tzinfo=UTC), datetime(2026, 11, 18, 14, 2, 5, tzinfo=UTC))


def test_render_whole_and_inline_placeholders() -> None:
    arguments: dict[str, Any] = {
        "channel": "{stream}",
        "oldest": "{from:unix}",
        "latest": "{to:unix_ms}",
        "query": "{stream} after:{from:%Y/%m/%d} before:{to:date} {{literal}}",
        "nested": {"since": ["{from}"], "count": 5, "flag": True},
        "thread": "{item.id}",
        "label": "id={item.id}",
    }
    rendered = render_arguments(arguments, stream="C0042", window=WINDOW, item={"id": 17})
    assert rendered == {
        "channel": "C0042",
        "oldest": 1794907800,
        "latest": 1795010525000,
        "query": "C0042 after:2026/11/17 before:2026-11-19 {literal}",
        "nested": {"since": ["2026-11-17T09:30:00Z"], "count": 5, "flag": True},
        "thread": 17,
        "label": "id=17",
    }
    assert arguments["oldest"] == "{from:unix}"  # not changed


def test_render_needs_its_values() -> None:
    with pytest.raises(KeyError, match="needs a window"):
        render_arguments({"a": "{from}"}, stream="s")
    with pytest.raises(KeyError, match=re.escape("{item.channel}")):
        render_arguments({"a": "{item.channel}"}, stream="s", item={"id": 1})


def test_placeholders_lists_paths() -> None:
    found = placeholders({"a": "{stream}", "b": {"c": ["x", "{to:date}"]}})
    assert [(path, p.name, p.format) for path, p in found] == [
        (("a",), "stream", None),
        (("b", "c", 1), "to", "date"),
    ]
    with pytest.raises(TemplateError) as raised:
        placeholders({"b": ["{oops"]})
    assert raised.value.path == ("b", 0)


@pytest.mark.parametrize(
    ("text", "part"),
    [
        ("{", "not closed"),
        ("}", "lone '}'"),
        ("{}", "no valid name"),
        ("{Stream}", "no valid name"),
        ("{item.a.b}", "no valid name"),
        ("{stream:unix}", "only {from} and {to} take a format"),
        ("{from:%b}", "%b is not allowed"),
        ("{from:%Y%}", "lone %"),
    ],
)
def test_bad_templates(text: str, part: str) -> None:
    with pytest.raises(TemplateError, match=re.escape(part)):
        parse_template(text)


# --- Times ---------------------------------------------------------------------------------------


def test_durations() -> None:
    assert parse_duration("90d") == timedelta(days=90)
    assert parse_duration("1w2d3h4m5s") == timedelta(days=9, hours=3, minutes=4, seconds=5)
    assert format_duration(timedelta(days=9, hours=3, minutes=4, seconds=5)) == "1w2d3h4m5s"
    assert format_duration(timedelta(days=7)) == "1w"
    for bad in ("", "5", "d", "1.5d", "1 d", "-1d", "0s", "99999999999999d"):
        with pytest.raises(ValueError, match="duration"):
            parse_duration(bad)


@pytest.mark.parametrize(
    ("fmt", "start", "end"),
    [
        ("rfc3339", "2026-11-17T09:30:00Z", "2026-11-18T14:02:05Z"),
        ("unix", 1794907800, 1795010525),
        ("unix_ms", 1794907800000, 1795010525000),
        ("date", "2026-11-17", "2026-11-19"),
        ("%Y/%m/%d %H", "2026/11/17 09", "2026/11/18 15"),
        ("%Y-%m", "2026-11", "2026-12"),
        ("%Y", "2026", "2027"),
    ],
)
def test_bounds_cover_the_window(fmt: str, start: str | int, end: str | int) -> None:
    window = (WINDOW[0], WINDOW[1].replace(microsecond=0))
    assert format_bound(window[0], fmt, "from") == start
    assert format_bound(window[1], fmt, "to") == end


def test_bounds_round_partial_units() -> None:
    moment = datetime(2026, 12, 31, 23, 59, 59, 500_000, tzinfo=UTC)
    assert format_bound(moment, "rfc3339", "from") == "2026-12-31T23:59:59Z"
    assert format_bound(moment, "rfc3339", "to") == "2027-01-01T00:00:00Z"
    assert format_bound(moment, "%Y-%m", "to") == "2027-01"
    exact = datetime(2026, 11, 18, tzinfo=UTC)
    assert format_bound(exact, "date", "to") == "2026-11-18"
    with pytest.raises(ValueError, match="time zone"):
        format_bound(datetime(2026, 11, 18), "date", "from")  # no zone, on purpose


def test_bound_and_timestamp_formats() -> None:
    assert check_bound_format("%Y/%m/%d") == "%Y/%m/%d"
    assert check_timestamp_format("%Y-%m-%d %H:%M:%S%z") == "%Y-%m-%d %H:%M:%S%z"
    with pytest.raises(ValueError, match="needs at least one"):
        check_bound_format("%%")
    with pytest.raises(ValueError, match="needs %Y, %m and %d"):
        check_timestamp_format("%H:%M")


@pytest.mark.parametrize(
    ("value", "fmt", "zone", "expected"),
    [
        ("1794300000.000200", "unix", None, datetime(2026, 11, 10, 8, 40, 0, 200, tzinfo=UTC)),
        (1794300000, "unix", None, datetime(2026, 11, 10, 8, 40, tzinfo=UTC)),
        (1794300000.5, "unix", None, datetime(2026, 11, 10, 8, 40, 0, 500_000, tzinfo=UTC)),
        ("1794300000123", "unix_ms", None, datetime(2026, 11, 10, 8, 40, 0, 123_000, tzinfo=UTC)),
        ("2026-11-10T08:40:00Z", "rfc3339", None, datetime(2026, 11, 10, 8, 40, tzinfo=UTC)),
        ("2026-11-10T09:40:00+01:00", "rfc3339", None, datetime(2026, 11, 10, 8, 40, tzinfo=UTC)),
        (
            "2026-11-10T08:40:00",
            "rfc3339",
            "Europe/Lisbon",
            datetime(2026, 11, 10, 8, 40, tzinfo=UTC),
        ),
        (
            "2026-07-10 09:40",
            "%Y-%m-%d %H:%M",
            "Europe/Lisbon",
            datetime(2026, 7, 10, 8, 40, tzinfo=UTC),
        ),
        ("2026-07-10 09:40", "%Y-%m-%d %H:%M", None, datetime(2026, 7, 10, 9, 40, tzinfo=UTC)),
    ],
)
def test_timestamps(value: object, fmt: str, zone: str | None, expected: datetime) -> None:
    parsed = parse_timestamp(value, fmt, zone)
    assert parsed.tzinfo is not None
    assert parsed == expected


@pytest.mark.parametrize(
    ("value", "fmt"),
    [
        ("17943x", "unix"),
        (True, "unix"),
        (-5, "unix"),
        (float("nan"), "unix"),
        ("1794300000.5", "unix_ms"),
        ("2026-11-10", "rfc3339"),
        ("yesterday-ish-value", "rfc3339"),
        (17, "%Y-%m-%d"),
        ("10/11/2026", "%Y-%m-%d"),
    ],
)
def test_bad_timestamps_do_not_quote_the_value(value: object, fmt: str) -> None:
    with pytest.raises(ValueError, match=r"timestamp|RFC 3339") as raised:
        parse_timestamp(value, fmt)
    assert str(value) not in str(raised.value)


# --- The JSON Schema -----------------------------------------------------------------------------


def test_committed_schema_matches_the_model() -> None:
    committed = (ROOT / "schemas" / "recipe.schema.json").read_text()
    assert committed == schema_text(), (
        "schemas/recipe.schema.json differs from the model; regenerate it with"
        " `uv run python -m memini.recipes.schema > schemas/recipe.schema.json`"
    )


def test_schema_describes_the_file() -> None:
    schema = recipe_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "format",
        "name",
        "server",
        "stream",
        "window",
        "list",
        "rate",
        "parser",
    }
    paging = schema["$defs"]["ListCall"]["properties"]["paging"]
    assert paging["discriminator"]["propertyName"] == "mode"
    assert "null" not in json.dumps(schema).replace("Missing, null or empty", "")


def test_docs_example_is_the_hearth_fixture() -> None:
    docs = (ROOT / "docs" / "recipes.md").read_text()
    blocks = re.findall(r"```toml\n(.*?)```", docs, flags=re.DOTALL)
    fixture = example_text("hearth")
    assert blocks[0] == fixture.split("\n", 1)[1]  # the fixture adds a #:schema line


# --- memini recipe -------------------------------------------------------------------------------


def run_validate(*paths: Path) -> tuple[int, list[str]]:
    out = io.StringIO()
    code = recipe_cli.validate_files(paths, out)
    return code, out.getvalue().splitlines()


def test_validate_valid_files() -> None:
    code, lines = run_validate(*(RECIPES / f"{name}.toml" for name in EXAMPLES))
    assert code == 0
    assert lines == [f"{RECIPES / name}.toml: valid recipe {name}" for name in EXAMPLES]


def test_validate_invalid_file(tmp_path: Path) -> None:
    text = edit(example_text("hearth"), 'id = "@.ts"', 'id = "$.ts"')
    text = edit(text, "concurrency = 2", 'concurrency = "2"')
    path = tmp_path / "broken.toml"
    path.write_text(text)
    code, lines = run_validate(RECIPES / "hearth.toml", path)
    assert code == 1
    assert lines[0].endswith("valid recipe hearth")
    assert lines[1:] == [f"{path}:59: rate.concurrency: must be an integer"]
    path.write_text(edit(example_text("hearth"), 'id = "@.ts"', 'id = "$.ts"'))
    code, lines = run_validate(path)
    assert lines == [
        f"{path}:30: list.item.id: a value of a listed item is a path from the item: it starts"
        " with @"
    ]


def test_validate_unreadable_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    syntax = tmp_path / "syntax.toml"
    syntax.write_text("format = = 1\n")
    binary = tmp_path / "binary.toml"
    binary.write_bytes(b"\xff\xfe")
    broken = tmp_path / "broken.toml"
    broken.write_text(edit(example_text("hearth"), "format = 1", "format = 2"))
    code, lines = run_validate(tmp_path / "absent.toml", syntax, binary, broken)
    assert code == 2  # wins over the invalid one
    assert lines == [f"{broken}:3: format: format 2 is not supported; this build reads format 1"]
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith(f"memini: {tmp_path / 'absent.toml'}: cannot read the file")
    assert err[1].startswith(f"memini: {syntax}: not valid TOML")
    assert err[2] == f"memini: {binary}: not UTF-8 text"


def test_parser_wiring(capsys: pytest.CaptureFixture[str]) -> None:
    parser = argparse.ArgumentParser(prog="memini recipe")
    recipe_cli.configure_parser(parser)
    args = parser.parse_args(["validate", str(RECIPES / "hearth.toml")])
    assert asyncio.run(args.offline(args)) == 0
    assert capsys.readouterr().out.endswith("valid recipe hearth\n")

    args = parser.parse_args(["schema"])
    assert asyncio.run(args.offline(args)) == 0
    assert json.loads(capsys.readouterr().out) == recipe_json_schema()

    with pytest.raises(SystemExit):
        parser.parse_args(["validate"])  # at least one path
    assert recipe_cli.HELP
    assert recipe_cli.DESCRIPTION
