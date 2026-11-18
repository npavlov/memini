"""Raw responses of MCP recipe runs, and replay (requirements, section 4: "Raw responses are stored
verbatim in both cases, so a parser fix can be replayed without calling the source again").

Both executors store every tool result here before anything parses it, and turn a window's stored
results into records with the same function; replay runs that function again over what is stored,
with zero calls to the source.

**What is stored** (migration 0015). A *window* (``raw_window``) is one window ``[from, to)`` of a
stream that an executor ran: its run, the planner's kind, the recipe digest the calls were planned
from, and the server's name and version from the session. A *response* (``raw_response``) is one
call of the window: list or detail, the tool, the arguments as sent, the page, the listed item of
a detail call, when it came, and the hash of its result. The *body* (``raw_body``) is the result:
the MCP result object as the server returned it (``content`` blocks, ``structuredContent``,
``isError`` and anything else it holds), with secrets and card numbers redacted
(:func:`memini.redaction.redact_json`) and nothing else changed, as canonical JSON. "Verbatim"
means no model rewrites it (principle 5), not that secrets are kept (principle 11): redaction runs
before anything is stored, as at ingest, and the hash is of the redacted bytes.

**Dedup.** A body is stored once per source by its hash: a page fetched again unchanged (a later
run's detail call of a thread that did not change, a retry) adds a response row of a few hundred
bytes and no body. In a window a call is known by its identity (call kind, tool and arguments): the
same call stored again with the same result writes nothing (a capture hook that posts twice), with
another result replaces it (a retry). When a window is first parsed, the stream's earlier attempts
at the same window (same kind and bounds: a retried job with the same pinned ``as_of``) and its
earlier windows never parsed (a run that died) are deleted. Response rows of later runs are not
merged: each run's window keeps its own calls until retention removes them.

**API for the executors** (:mod:`memini.responses.store`, :mod:`memini.responses.replay`)::

    raw = await begin_window(conn, workspace_id=…, owner_id=…, source_id=…, stream_id=…,
                             window=planned, recipe_digest=recipe_digest(recipe),
                             server=ServerInfo(name, version), run_id=run.id)
    for each call:                                   # list pages, then detail calls
        await store_response(conn, raw.id, call="list", tool=…, arguments=…, page=n,
                             result=result.model_dump(mode="json", by_alias=True))
    done = await ingest_window(conn, access, raw.id, recipe=recipe, parser=parser,
                               runs=runs, indexer=indexer)

:func:`ingest_window` parses the window (:func:`memini.recipes.parsing.parse_window`), records
which records each response was built into (:func:`record_parse`), and ingests the records in
batches, the last one claiming the window: complete when every check passed, a snapshot when the
recipe says so. An incomplete window stores what it got and leaves the checkpoint where it was;
its result names the calls that would complete it (``missing``), and the current run's report
lists them under ``details.incomplete_windows``. Before that, an executor finds the next calls of
a window from what is stored: :func:`memini.recipes.missing_calls` over the window's responses
(:func:`load_window`) names the first call of the listing, then each next page and each detail
call, until nothing is missing.

**Replay** (:func:`replay`, ``memini replay``) takes a recipe and its parser and goes through the
parsed windows of a source, of one stream or of one run, in the order they were started, with the
source's job lock held so no sync runs meanwhile:

1. Before it writes anything, it parses every chosen window and every window started after the
   first of them, and finds for each record the last window that holds it. A window that fails to
   parse, comes from another server, or builds a record ingest refuses stops the replay.
2. Each chosen window is parsed again, its attribution recorded, and its records ingested with its
   claim. A record goes only from the last window that holds it: an older window's version of a
   thread that a later window updated is not written over it, so replaying with an unchanged
   parser writes nothing. A record stored as a tombstone is left so (a deletion seen at the source
   is not undone from older responses); a record of a window forget pruned that is not stored is
   not inserted; a record older than the source's retention is left out.
3. The claim is complete when the parse is and forget pruned nothing of the window, never a
   snapshot: a snapshot of an older window would delete what a later window found, and the
   deletions snapshots found are kept as tombstones (point 2). A claim that moves the checkpoint
   moves it (a parser fix that makes an incomplete window complete); one behind it moves nothing.
   A complete window that starts after the checkpoint, while an earlier one is still incomplete,
   is claimed incomplete.

**Retention and forget.** :func:`delete_raw_responses_before` deletes a source's windows that
ended before the retention cutoff, their responses and the bodies no response needs any more.
:func:`delete_raw_responses_for_records` removes the bodies of every response whose attribution
names a forgotten record (the list page that held its item, each page of its detail call), marks
their windows pruned, and deletes the source's windows that were never parsed, which have no
attribution yet.
"""

from memini.responses.replay import (
    INCOMPLETE_DETAIL,
    MAX_REPORTED_MISSING,
    RUN_KIND,
    ReplayError,
    ReplayResult,
    WindowIngest,
    ingest_window,
    replay,
)
from memini.responses.store import (
    CALL_KEY_DOMAIN,
    MAX_RESPONSE_SIZE,
    InvalidResponseError,
    NotAnMcpSourceError,
    RawDeletion,
    RawWindow,
    ResponseTooLargeError,
    ServerInfo,
    StoredResponse,
    WindowClosedError,
    WindowNotFoundError,
    begin_window,
    call_key,
    delete_raw_responses_before,
    delete_raw_responses_for_records,
    get_window,
    load_window,
    record_parse,
    store_response,
    stored_record_ids,
)

__all__ = [
    "CALL_KEY_DOMAIN",
    "INCOMPLETE_DETAIL",
    "MAX_REPORTED_MISSING",
    "MAX_RESPONSE_SIZE",
    "RUN_KIND",
    "InvalidResponseError",
    "NotAnMcpSourceError",
    "RawDeletion",
    "RawWindow",
    "ReplayError",
    "ReplayResult",
    "ResponseTooLargeError",
    "ServerInfo",
    "StoredResponse",
    "WindowClosedError",
    "WindowIngest",
    "WindowNotFoundError",
    "begin_window",
    "call_key",
    "delete_raw_responses_before",
    "delete_raw_responses_for_records",
    "get_window",
    "ingest_window",
    "load_window",
    "record_parse",
    "replay",
    "store_response",
    "stored_record_ids",
]
