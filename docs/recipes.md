# MCP recipes: format reference

A recipe is a declarative file that says how to sync one MCP server: which tool lists a stream's
items in a time window, how its pages follow each other, which tool fetches each item's details,
where the id, the time and the reply count are in a response, which checks prove a window
complete, how fast the server may be called, and which parser turns the raw responses into
records. Why recipes exist, and the two executors that run them, are in
[requirements.md](requirements.md), section 4. This page is the reference of the format; a guide
to writing a recipe and its parser comes later.

A recipe holds no endpoint, credential or stream list: those belong to the source that uses the
recipe. It holds no model prompt either: every value the executors read is found by a selector,
so the sync loop is deterministic (principle 5).

- Format: TOML, one file per server, `format = 1`.
- Schema: [`schemas/recipe.schema.json`](../schemas/recipe.schema.json) (JSON Schema 2020-12), for
  editors; `memini recipe schema` prints it.
- Check: `memini recipe validate PATH...`.
- Code: `memini.recipes` (model, selectors, placeholders, times, validation; reading a window
  and its checks; parsers).

## A full example

Hearth is an invented chat server. A stream is one channel; its root messages are listed by time
with a cursor, and each message with replies gets a second call for its thread. The file is also
`tests/fixtures/recipes/hearth.toml`, and a test keeps the two equal.

```toml
# Hearth is an invented chat server, used as the example of docs/recipes.md.
format = 1
name = "hearth"
description = "Channel messages and their threads from the Hearth chat server."

[server]
name = "hearth-mcp"            # serverInfo.name at initialize
versions = ["2.*"]             # serverInfo.version this recipe was checked against

[stream]
kind = "thread"                # a root message and its replies become one thread record
key_description = "a channel id, such as C0001"
key_pattern = "C[0-9]{4,8}"

[window]
overlap = "2d"                 # replies to a thread arrive for days
initial = "14d"                # the first sync of a channel loads two weeks
backfill = "30d"               # each later run loads 30 more days of history
max_span = "7d"                # one listing covers at most a week

[list]
tool = "channel_history"
decode = "json"                # the text content is a JSON document
arguments = { channel = "{stream}", oldest = "{from:unix}", latest = "{to:unix}", limit = 100 }
items = "$.messages[*]"
order = "newest_first"

[list.item]
id = "@.ts"
timestamp = "@.ts"
timestamp_format = "unix"      # "1731900000.000100": seconds, read exactly
reply_count = "@.reply_count"

[list.paging]
mode = "cursor"
argument = "cursor"            # sent back on the next call, left out of the first
next = "$.next_cursor"         # missing, null or "" on the last page
has_more = "$.has_more"
max_pages = 200

[detail]
tool = "thread_replies"
when = "has_replies"           # only items whose reply_count is above zero
arguments = { channel = "{stream}", thread = "{item.id}" }
items = "$.messages[*]"
includes_root = true           # the first message is the root itself

[detail.paging]
mode = "cursor"
argument = "cursor"
next = "$.next_cursor"

[checks]
reply_counts = true            # replies fetched equal the reply_count listed

[rate]
calls_per_minute = 50
concurrency = 2
max_calls = 3000
timeout = "30s"

[parser]
name = "hearth.thread"
options = { message_url = "https://hearth.example/c/{stream}/m/{id}" }
```

For channel `C0042` and the window `[2026-11-04T00:00Z, 2026-11-11T00:00Z)` the first call is
`channel_history` with `{"channel": "C0042", "oldest": 1793750400, "latest": 1794355200,
"limit": 100}`. A response such as

```json
{"messages": [{"ts": "1794300000.000200", "text": "...", "reply_count": 2}, ...],
 "has_more": true, "next_cursor": "cur-0002"}
```

gives the items, the id and time of each, a reply count, and the next call: the same arguments
with `"cursor": "cur-0002"`. The message with two replies then gets `thread_replies` with
`{"channel": "C0042", "thread": "1794300000.000200"}`, whose `messages` must number three (the
root and two replies) for the window to count as complete.

Two more examples, in `tests/fixtures/recipes/`, show the other choices: `millnotes.toml`, a
meeting note-taker with numbered pages, `structuredContent` and a list tool without date
arguments; `quillpost.toml`, a mail server that answers in text, read with regular expressions,
with the window inside a search query and offset paging.

An editor with a TOML language server (Taplo, Even Better TOML) completes and checks keys when
the file names the schema in its first line: `#:schema ../schemas/recipe.schema.json`, relative
to the recipe, or the schema's URL.

## Top level

| Key | Type | Meaning |
|---|---|---|
| `format` | integer, required | The recipe format: `1`. A file of another format is not read further. |
| `name` | string, required | The recipe's name: 1 to 63 lowercase letters, digits, `-` or `_`, starting with a letter or digit. |
| `description` | string | For people. |
| `[server]` | table, required | The server and its drift check. |
| `[stream]` | table, required | What a stream is and what its items become. |
| `[window]` | table, required | Defaults for the planner. |
| `[list]` | table, required | The list call. |
| `[detail]` | table | The call per listed item. |
| `[checks]` | table | Optional completeness checks. |
| `[rate]` | table, required | The call budget. |
| `[parser]` | table, required | The parser. |

Unknown keys are errors everywhere, so a misspelt key is not silently ignored. Values are not
coerced: `concurrency = "2"` is an error, as is a TOML date or time (`timeout =
2026-11-18T10:00:00Z`): times are strings. Durations are numbers with units `s`, `m`, `h`, `d`
and `w`: `90d`, `12h`, `1w2d`.

## `[server]`: identity and drift

| Key | Type | Meaning |
|---|---|---|
| `name` | string, required | The name the server reports when a session starts (`serverInfo.name`). |
| `versions` | array of strings | Versions (`serverInfo.version`) the recipe was checked against; `*` matches any characters (`1.4.*`). Empty or left out: any version. |

MCP servers change the shape of their output (plan.md, risks), so an executor checks before it
calls anything: another server name stops the run; a version that matches no pattern is a warning
in the run report; a tool the recipe calls that the server does not list stops the run; an
argument the recipe sends that the tool's input schema does not declare (when it declares its
properties) stops the run. The tools are those of `list.tool` and `detail.tool`; they need not be
listed again.

## `[stream]`: what a stream is

| Key | Type | Meaning |
|---|---|---|
| `kind` | `thread`, `meeting` or `document`, required | The kind of record the parser makes of the stream's items. |
| `key_description` | string, required | What a stream key is, for the person who configures a source: "a channel id", "a mailbox search query". |
| `key_pattern` | regular expression | A stream key must match it in full. A server with one list uses a fixed key, such as `meetings`. |
| `snapshot` | boolean, default false | A complete listing of a window holds every live item of the window. The executor then claims an ingest snapshot, and a stored record of the window that the listing left out becomes a tombstone. |

A stream is the unit of incremental sync: it has its own checkpoint (requirements, section 3).
The source names its streams by key, and `{stream}` puts the key into the arguments.

## `[window]`: defaults for the planner

| Key | Type | Meaning |
|---|---|---|
| `overlap` | duration, required | How far behind the checkpoint a window starts, to catch late replies and edits. |
| `initial` | duration, required | How much recent history the first sync of a stream loads. |
| `backfill` | duration | How much older history each later run loads, in steps, until the source's retention start. Left out: no history beyond the initial window. |
| `max_span` | duration | The longest window one listing covers; the planner splits a longer one. For servers that cap a search's results. |

A source may override each value in its own `window` object, with the same durations, and null
for `backfill` or `max_span` to turn them off; it may also set `history`, how far back its history
loads at most (a source with neither a retention nor `history` loads 365 days back, or `initial`
if longer). The planner (`memini.intake`, not the recipe) chooses each run's windows: from the
checkpoint less `overlap` to a time pinned for the run, or `initial` back for a new stream; and,
with `backfill`, one step of older history ending at the stream's frontier, never before the
retention start. Windows longer than `max_span` are split: forward windows run oldest first,
history windows newest first, and each chain stops at its first incomplete window. Requirements,
section 4, has the rules.

## `[list]`: the list call

| Key | Type | Meaning |
|---|---|---|
| `tool` | string, required | The MCP tool name. |
| `arguments` | table | The tool's arguments, with placeholders (below). |
| `decode` | `json`, `structured` or `text`, default `json` | How the result is read; it sets the language of every selector of the call. |
| `items` | selector, required | Where the items are: a path with `*`, or a pattern that matches each item. |
| `order` | `newest_first`, `oldest_first` or `any`, default `any` | The order of the items. Needs `item.timestamp` unless `any`. |
| `[list.item]` | table, required | Where each item's values are. |
| `[list.paging]` | table, required | The paging rule. |

`[list.item]`, relative to the item (`@` paths, or patterns over the item's text):

| Key | Type | Meaning |
|---|---|---|
| `id` | selector, required | The item's id at the server; a detail call asks for it. |
| `timestamp` | selector | The item's time. Needed for an ordered listing. |
| `timestamp_format` | string, default `rfc3339` | `rfc3339`, `unix` (seconds; a number or a string of digits with a fraction, read exactly), `unix_ms`, or a `strptime` pattern of `%Y %m %d %H %M %S %f %z %%`. |
| `timezone` | IANA zone name | The zone of a timestamp that has none. UTC when left out. |
| `reply_count` | selector | The number of replies the listing announces. |
| `fields` | table of selectors | Other values a detail call needs, by name: `fields = { mailbox = "@.box" }` is `{item.mailbox}`. |

### Decoding

An MCP tool result is a list of content blocks, optional `structuredContent`, and `isError`. A
result with `isError` is a failed call: it is retried within the budget and otherwise leaves the
window incomplete. Otherwise `decode` reads it:

- `json`: the text of the text blocks, joined by newlines, parsed as JSON. For servers that put a
  JSON document in a text block, the common case.
- `structured`: the `structuredContent` object.
- `text`: the text of the text blocks, joined by newlines. For servers that answer in prose or
  Markdown meant for a model.

The raw result is stored verbatim either way (below); decoding is how the executor reads it, not
what is kept.

### Selectors

With `json` and `structured` a selector is a **path**, a small subset of JSONPath without filters,
recursive descent or functions:

| Path | Selects |
|---|---|
| `$` | the decoded response; `@` the current item |
| `$.messages` | the key `messages` (names: letters, digits, `_`, `-`) |
| `$['started at']`, `$["a.b"]` | any key, quoted; `\` escapes |
| `$.messages[0]`, `$.messages[-1]` | an element of an array; negative counts from the end |
| `$.messages[*]`, `$.channels.*` | every element of an array, or every value of an object |

A path selects a list of values in document order. A missing key, an index out of range or a step
into a value of another type selects nothing, which is not an error: a missing `next_cursor` is
how the last page says so. Paths from the response start with `$`, the values of an item (in
`[list.item]`) with `@`. `items` must select many values (it has `*`); every other selector one
(it has none).

With `text` a selector is a **pattern**, a Python regular expression:

- `items`: each match is one item, and the item's text is the whole match; the item's values are
  then read from that text.
- a value (`id`, `timestamp`, `reply_count`, `next`, `number`, `total`, ...): exactly one
  capturing group, whose text in the first match is the value. Write `(?:...)` for a group that
  captures nothing.
- `has_more`: more pages follow when it matches.

Inline flags set the mode: `(?m)` for `^` and `$` at each line, `(?s)` for a `.` that matches a
newline. In TOML, write patterns in single quotes (`'(\d+) messages'`) so that backslashes are
not escapes.

### Placeholders

A string in `arguments`, at any depth, may hold placeholders:

| Placeholder | Value |
|---|---|
| `{stream}` | the stream key |
| `{from}`, `{to}` | the window `[from, to)`, as RFC 3339 in UTC (`2026-11-18T14:02:11Z`) |
| `{from:FORMAT}`, `{to:FORMAT}` | the bound in `unix` (integer seconds), `unix_ms`, `date` (`2026-11-18`), `rfc3339`, or a `strftime` pattern of `%Y %m %d %H %M %S %%` |
| `{item.id}`, `{item.timestamp}`, `{item.reply_count}`, `{item.NAME}` | in `[detail]` only: the listed item's values; `NAME` from `list.item.fields` |

A string that is one placeholder and nothing else becomes the value itself: `"{from:unix}"` is an
integer, `"{item.id}"` the id as the response held it. Inside a longer string a value is written
as text: `query = "{stream} after:{from:%Y/%m/%d}"`. `{{` and `}}` are literal braces.

A bound is rendered so that the range a call asks for covers the window: `from` is rounded down
and `to` up to the unit of its format. The window `[2026-11-17T09:30Z, 2026-11-18T14:02Z)` with
`date` asks for `2026-11-17` to `2026-11-19`. The call returns more than the window, which is
harmless: records are upserted by id. A list call whose arguments have no `{from}` and no `{to}`
lists everything; with `order = "newest_first"` it stops after the first page that reaches past
the window's start, with `oldest_first` after the first that reaches past its end.

### Paging

`[list.paging]` (and `[detail.paging]`) has a `mode`, and only the keys of that mode:

| Mode | Keys | Next call | Last page |
|---|---|---|---|
| `cursor` | `argument`, `next`, `has_more`, `max_pages` | `argument` = the value at `next` | `next` missing, null or empty; or `has_more` false |
| `page` | `argument`, `first` (1 or 0), `size_argument`, `size`, `number`, `total_pages`, `total`, `has_more`, `max_pages` | `argument` = previous + 1 | `total_pages` reached, `has_more` false, fewer than `size` items, or no items |
| `offset` | `argument`, `start` (0 or 1), `size_argument`, `size`, `total`, `has_more`, `max_pages` | `argument` = items seen so far (from `start`) | `total` reached, `has_more` false, fewer than `size` items, or no items |
| `none` | | | the first page |

The paging argument is left out of the first call of a cursor listing and set on every call of
the other modes; `size_argument` (which needs `size`) is set on every call. Neither may appear in
`arguments`: the paging rule owns them. `has_more` that says yes on a page without a next cursor
fails completeness rather than ending the listing. `max_pages` (default 1000) caps one listing; a
listing that reaches it is incomplete, never silently cut.

## `[detail]`: the call per item

| Key | Type | Meaning |
|---|---|---|
| `tool` | string, required | The MCP tool name. |
| `arguments` | table | Must name the item: `{item.id}` or another `{item.NAME}`. |
| `decode` | as in `[list]` | |
| `when` | `always` or `has_replies`, default `always` | For which items. `has_replies` needs `list.item.reply_count`, and skips items whose count is zero. |
| `items` | selector | Where the replies are, to count them. |
| `includes_root` | boolean, default false | `items` holds the item itself as well; it is not counted as a reply. Needs `items`. |
| `[detail.paging]` | table | As in `[list]`; `mode = "none"` when left out. |

Without `[detail]` the listing alone holds what the parser needs.

## `[checks]`: completeness

A window counts as complete, and its checkpoint moves (principle 7), only if every check passes.
Every recipe gets these:

- every call succeeded (after retries within the budget);
- every listing reached its last page by its paging rule, not by `max_pages` or `rate.max_calls`;
- every cursor a page named was followed;
- every listed item that `when` selects got its detail call, with all its pages.

`[checks]` adds the ones that need numbers the server may or may not give:

| Key | Needs | Passes when |
|---|---|---|
| `page_numbers` | page paging with `number` | the page numbers the responses state run from `first` without a gap or a repeat, and end at `total_pages` when it is stated |
| `reply_counts` | `list.item.reply_count`, `detail.items` | for each item, the detail's `items` over all its pages, less the item itself with `includes_root`, are as many as the listed `reply_count` |
| `totals` | page or offset paging with `total` | a listing's items are as many as its stated `total` |

A server whose count includes the item (`3 messages` for a root and two replies) leaves
`includes_root` out, so both sides count it. A check declared without what it needs is an error
of the recipe, not a check that silently passes. An incomplete window stores what it got, leaves
the checkpoint where it was and lists the calls that would complete it.

### How the checks read a window

The checks run on the stored responses of a window (`memini.recipes.reading.read_window`), with
the recipe's selectors and nothing else:

- A call is known by its identity, as the store keeps it: list or detail, the tool and the
  arguments. The listing is walked from its first call (the arguments rendered for the stream and
  the window) by its paging rule: the next call carries the cursor a page named, the next page
  number or the items seen so far. Each item `when` selects is walked the same way from its
  detail call's arguments.
- Every list page is read for items, the ones a walk did not reach too. An item listed twice
  (pages that overlap) is one item. An item without an id, or with a time or a reply count that
  cannot be read, fails the window.
- A list call without `{from}` and `{to}` lists everything: with an `order` it ends after the
  first page that reaches past the window, and only the items inside the window are the
  window's, so a history window does not fetch the details of newer items. A windowed listing
  keeps every item it returns.
- `page_numbers` also reads on to the stated `total_pages` when a short page would end the
  listing; `totals` compares a walk's items with the last total a page stated.

Each failed check names the call that would make it pass, a `MissingCall`: the call (list or
detail), the tool, the arguments, the page and the item it is stored with, the check, a reason
for the run report (the call by its position in the window, never a value of a response), and
`repeat` when the window holds that call already, so that making it again is a retry:

| Check | Fails when | Missing call |
|---|---|---|
| `failed` | a call has `isError`, or `decode` cannot read its result | the same call |
| `page` | a page the paging rule asks for is not in the window: the listing's first call, the page a cursor names, the next page number or offset, an item's next detail page | that page |
| `truncated` | a page says more follow (`has_more`) but names no cursor, or names a page read before | the same page |
| `max_pages` | the listing reached `max_pages` before its last page | its next page |
| `detail` | an item `when` selects has no detail call | its first detail call |
| `item` | an item cannot be read, or the parser found it incomplete | the call that holds it |
| `page_numbers` | a page states another number than it was asked for, or the pages end before `total_pages` | that page, or the next |
| `reply_counts` | an item's replies differ from its `reply_count` | its detail call |
| `totals` | a walk's items differ from its `total` | the next page when short, the first when over |

A window with no calls yet misses the first call of its listing; once that is stored, the next
page and the detail calls of the listed items; and so on until nothing is missing. That is how
both executors find their calls (below).

## `[rate]`: the budget

| Key | Type | Meaning |
|---|---|---|
| `calls_per_minute` | integer 1 to 6000, required | At most this many calls in any minute. |
| `concurrency` | integer 1 to 16, default 1 | Calls in flight at once. |
| `max_calls` | integer, default 5000 | Calls one run may make; a run that reaches it ends incomplete. |
| `timeout` | duration, default `60s` | How long one call may take. |

## `[parser]`

| Key | Type | Meaning |
|---|---|---|
| `name` | string, required | A deterministic parser registered in the service (`hearth.thread`): dotted lowercase names. |
| `options` | table | Settings the parser reads and checks, such as a URL pattern for `external_url`. |

The parser reads the raw responses of a window (every list page and every detail response, with
the arguments of each call) and returns records of the stream's `kind`: an `external_id`,
`occurred_at` and a payload with the fields the ingest contract requires (requirements, section
4). It is a Python function, not a model, so the same responses give the same records.

The protocol is `memini.recipes.parsing`. A parser takes a `WindowInput` (the recipe, the stream
key, the window, and the window's responses in the order the calls were made, each a
`RawResponse` with its call, tool, arguments, page, listed item and stored result;
`RawResponse.decode` reads a result by `decode`) and returns a `ParsedWindow`: the records, each
with the positions of every response it was built from (the list page that holds its item, each
page of its detail call: forget removes those responses with the record), whether the window is
complete, and the calls that would complete it. A parser is registered under its name with
`register_parser`; the executors and replay run it through `parse_window`, which checks its
output and adds the checks of the recipe, whatever the parser said.

Most parsers need not read responses at all. An item parser is the part that knows the payload:
the recipe's selectors read the window, and the item parser gets one `Item` at a time (its
listing, with the id, time, reply count and fields the list call read, and the decoded pages of
its detail call, with the replies `detail.items` selects) and returns one record of the stream's
`kind`, or none. `item_parser(parse)` makes it a parser to register; it names the item's
responses for each record, so none is left out of forget. An item its responses do not hold all
of yet (a meeting whose transcript is still being made) is an `IncompleteItem`, with what it
gave so far: the window stays incomplete and the call that holds the item is named again.

```python
def hearth_thread(item: Item, window: WindowInput) -> RecordIn | IncompleteItem | None:
    root = item.listed.value if item.listed else item.detail_items[0]
    messages = [root, *item.replies]
    return RecordIn(kind="thread", external_id=f"{window.stream}/{item.key}", ...)

register_parser("hearth.thread", item_parser(hearth_thread))
```

## How the executors use a recipe

Both executors find their calls the same way: `memini.recipes.missing_calls` over the window's
stored responses. With none stored it names the list call's first page; once a page is stored,
its next page and the detail calls of its items; and so on until nothing is missing, so the
calls follow from the recipe and what the server answered, never from an executor's own loop. A
missing call with `repeat` is a retry, made while the budget allows; one past `max_pages` is not
made.

**Service executor.** The built-in MCP client opens a session with the source's endpoint and
credential, checks the server for drift, and for each due stream asks the planner for windows. For
each window it makes the missing calls, up to `rate.concurrency` at once and within `[rate]`,
storing each raw result as it came (after redaction) before anything is parsed, until none is
missing or the budget is spent. The parser then turns the stored responses into records, the
checks run on them, and one ingest batch stores the records and claims the window, complete or
not. A call that times out, fails on the network, gets HTTP 429 or a server error, or answers
`isError` is tried again after a backoff, each try a call of the budget, up to
`mcp_call_attempts` tries; a 429 pauses every call for its `Retry-After`. It connects only where
the service's `mcp_*` settings allow: an endpoint in `mcp_allowed_endpoints` whose host resolves
to no internal address unless `mcp_allowed_networks` lists it, or an executable in
`mcp_allowed_commands` (requirements, section 4, Service executor and its egress).

The source holds its recipe: `memini source add --kind mcp --recipe PATH` sends the file's text,
and the service keeps it with the source; `--recipe NAME` names a recipe that ships with Memini.

**Agent executor.** For servers reachable only inside an interactive agent session. The service
hands out the missing calls one at a time ("next call": a tool and its arguments). The agent
makes the call; a hook posts the raw result verbatim to the service, which stores it and hands
out the next missing call. The agent never rewrites a response. When nothing is missing, or the
session ends, the same checks and the same parser run on the server side.

Its routes are under `/v1/workspaces/{workspace}/sources/{source}/agent-runs`, with the `ingest`
scope (requirements, section 4, Agent executor): `POST` starts a run of a stream, `GET
/{id}/next` answers `call` (a tool, its arguments and an id), `window_done` or `run_done`, `POST
/{id}/responses` takes the raw result of a call handed out under its id, and `POST /{id}/finish`
parses and ingests the windows and writes the run report. A response for a call that was not
handed out, or with other arguments, is refused. The source's recipe is in its configuration,
`config.recipe` (the TOML text), and its parser is registered under `parser.name`.

`integrations/claude-code` is a Claude Code plugin that runs it: the skill `memini-intake` drives
the loop, and a hook after each MCP tool call posts the result of the call handed out and shows
the agent a short receipt in its place. To use it:

1. An mcp source in Memini with the recipe and its streams, and a token with the `ingest` scope
   (`memini token create --scope ingest --workspace W`), saved in a file only you can read.
2. `~/.config/memini/capture.toml` (or the file `MEMINI_CAPTURE_CONFIG` names):

   ```toml
   url = "http://127.0.0.1:6364"
   token_file = "~/.config/memini/capture.token"
   servers = ["hearth"]          # MCP servers whose responses may be captured: mcp__hearth__*
   ```

   A response of a server not listed is never sent.
3. The plugin: `claude --plugin-dir integrations/claude-code`, or copy
   `integrations/claude-code/skills/memini-intake` to `~/.claude/skills/` and add the two hooks of
   `integrations/claude-code/hooks/hooks.json` to `~/.claude/settings.json`, with the path of
   `memini-agent` in place of `${CLAUDE_PLUGIN_ROOT}/skills/memini-intake/scripts/memini-agent`.
   The client needs Python 3.11 or later: `uv` runs it when it is installed, else `python3`, or
   the interpreter `MEMINI_CAPTURE_PYTHON` names.
4. In a session: ask to take a stream of the source into Memini ("take channel C0042 of hearth
   into Memini"); the skill starts the run, makes the calls and finishes it.

Claude Code hands a hook an MCP tool's content blocks, not its `structuredContent`: a recipe that
decodes `structured` cannot be run through it yet.

An incomplete window keeps the stream's checkpoint, and the run report lists, for each such
window, its bounds, why it is incomplete and the calls that would complete it
(`details.incomplete_windows`, at most 50 calls a window); the next run plans the window again.

Both executors therefore make the same calls with the same arguments and store the same raw
responses, and the records are a function of those responses and the parser: one recipe run by
both executors yields identical records (the check of milestone M3). Because raw responses are
stored, a parser fix is replayed over them without calling the server again, and
`recipe_digest` in each run records which recipe the calls were planned from.

Both store through `memini.responses`: `begin_window` before a window's first call,
`store_response` for each result (redacted, otherwise as it came; a body is kept once per source
by its hash), and `ingest_window` to parse the window and ingest its records with its claim.
`memini replay --workspace W --source S --recipe PATH` parses a source's stored windows again
with the parser of the build and ingests what changed (requirements, section 4, Raw responses and
replay).

## Validation

`memini recipe validate PATH...` reads each file and checks it; it needs no database or
service. It prints `PATH: valid recipe NAME`, or one line per problem with the field and its line:

```
recipes/hearth.toml:41: list.paging.next_token: unknown key for mode = cursor
recipes/hearth.toml:30: list.item.id: a value of a listed item is a path from the item: it starts with @
recipes/hearth.toml:54: checks.reply_counts: needs list.item.reply_count
```

Exit codes: 0 every file is valid, 1 a recipe is not valid, 2 a file cannot be read as TOML (its
reason goes to stderr). The checks run in three steps: `format` first; then the model (required
keys, types without coercion, ranges, unknown keys, durations, time formats, zones); then the
rules that span keys (selectors in their call's language, placeholders the call can fill, paging
arguments, and what each check, `order` and `when` needs). The JSON Schema covers the first two
steps; only `validate` covers the third.

The schema is generated from the model. After a change of the model, regenerate it:

```
uv run python -m memini.recipes.schema > schemas/recipe.schema.json
```

A test fails while the two differ. A change of a key's meaning takes a new `format`.
