"""The query tools: ``search``, ``search_code``, ``get_symbol`` and ``read_source``.

Each tool authorizes the read (:func:`~memini.auth.authorization.authorize_read`), borrows a
connection from the pool and calls its function in :mod:`memini.query.service`, which the HTTP
routes call too. A :class:`~memini.query.QueryError` becomes an error result with its message, so
the model can correct its call; the messages never repeat an argument or stored content.

The structured result is the shape of :mod:`memini.query.model` or
:class:`~memini.query.SourceRead`. The text content says the same for clients that read only text:
the locators, kinds and times, the link and evidence reference (record id and anchor) of a hit
inside a thread, a meeting or a document, and the stored text in fenced blocks, as it is. The
server's instructions tell the client that all of it is data, never instructions; nothing here
adds to that.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import BaseModel

from memini.auth import ReadFilter, authorization
from memini.mcp.tools import Tool, ToolCall, ToolError
from memini.query import (
    CodeSearchArguments,
    QueryError,
    QueryServices,
    ReadSourceArguments,
    SearchArguments,
    SearchHit,
    SearchResult,
    SourceRead,
    SymbolArguments,
    SymbolResult,
    run_get_symbol,
    run_read_source,
    run_search,
    run_search_code,
)
from memini.query.reader import DEFAULT_READ_LIMITS

__all__ = [
    "GET_SYMBOL_TOOL",
    "QUERY_TOOLS",
    "READ_SOURCE_TOOL",
    "SEARCH_CODE_TOOL",
    "SEARCH_TOOL",
    "fenced",
    "summarize_read",
    "summarize_search",
    "summarize_symbols",
]

type Conn = AsyncConnection[TupleRow]
type Runner[A: BaseModel, R: BaseModel] = Callable[
    [Conn, QueryServices, ReadFilter, A], Awaitable[R]
]


def _tool_run[A: BaseModel, R: BaseModel](
    run: Runner[A, R],
) -> Callable[[ToolCall, A], Awaitable[R]]:
    """A tool's ``run``: authorize, borrow a connection, call ``run``, report a refusal."""

    async def tool_run(call: ToolCall, arguments: A) -> R:
        # Called through its module, so a test can watch the tool go through the filter.
        access = authorization.authorize_read(call.principal)
        pool = call.pool
        if pool is None:
            raise ToolError("The database is not available.")
        try:
            async with pool.connection() as conn:
                return await run(conn, call.services, access, arguments)
        except QueryError as error:
            raise ToolError(error.message) from None

    return tool_run


# --- Text summaries ------------------------------------------------------------------------------


def fenced(text: str, info: str = "") -> str:
    """``text`` in a fenced block whose fence is longer than any run of backticks in it, so no
    content can close the block early."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    body = text if text.endswith("\n") or not text else text + "\n"
    return f"{fence}{info}\n{body}{fence}"


def _time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _count(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _hit(number: int, hit: SearchHit) -> str:
    details = [
        f"{hit.kind} in {hit.workspace}/{hit.source} ({hit.source_kind})",
        _time(hit.occurred_at),
    ]
    if hit.at != hit.occurred_at:
        details.append(f"chunk at {_time(hit.at)}")
    if hit.definition is not None:
        details.append(f"defines {hit.definition.kind} {hit.definition.qualified_name}")
    elif hit.symbol:
        details.append(f"in {hit.symbol}")
    if hit.match_line is not None:
        details.append(f"first match on line {hit.match_line}")
    if hit.score is not None:
        details.append(f"score {hit.score:.4g}")
    if hit.truncated:
        details.append("snippet cut short")
    lines = [f"{number}. {hit.locator}", "; ".join(details)]
    if hit.link is not None:
        lines.append(f"link: {hit.link}")
    if hit.anchor is not None:
        lines.append(f"evidence: record_id {hit.record_id}, anchor {hit.anchor}")
    lines.append(fenced(hit.snippet, hit.language or ""))
    return "\n".join(lines)


def summarize_search(result: SearchResult) -> str:
    """The text content of a ``search`` or ``search_code`` result: every hit with its snippet."""
    lines = [f"{_count(len(result.hits), 'hit', 'hits')}."]
    lines += [_hit(number, hit) for number, hit in enumerate(result.hits, start=1)]
    return "\n\n".join(lines)


def summarize_symbols(result: SymbolResult) -> str:
    """The text content of a ``get_symbol`` result: every definition with its code."""
    parts = [f"{_count(len(result.definitions), 'definition', 'definitions')}."]
    for number, found in enumerate(result.definitions, start=1):
        cut = "; snippet cut short" if found.truncated else ""
        start = (
            f"; snippet from line {found.snippet_start_line}" if found.snippet_start_line else ""
        )
        parts.append(
            f"{number}. {found.kind} {found.qualified_name} at {found.locator}\n"
            f"in {found.workspace}/{found.repository}, {_time(found.occurred_at)}{start}{cut}\n"
            f"{fenced(found.snippet, found.language or '')}"
        )
    return "\n\n".join(parts)


def _symbol_spans(read: SourceRead) -> str:
    spans = ", ".join(
        f"{span.qualified_name} ({span.kind}, lines {span.start_line}-{span.end_line})"
        for span in read.symbols
    )
    return f"\nDefinitions: {spans}." if spans else ""


def summarize_read(read: SourceRead) -> str:
    """The text content of a ``read_source`` result."""
    at = f"{'pinned commit' if read.pinned else 'commit'} of {_time(read.occurred_at)}"
    where = f"{read.locator} ({at}{f', {read.ref}' if read.ref else ''})"
    if read.kind == "directory":
        listed = read.entries or []
        shown = f"; the first {len(listed)} shown" if read.truncated else ""
        entries = "\n".join(
            f"- {entry.name}{'/' if entry.kind == 'directory' else ''} ({entry.kind}"
            f"{f', {entry.size} bytes' if entry.size is not None else ''})"
            for entry in listed
        )
        return f"{where}: a directory{shown}.\n{entries}"
    if read.kind == "submodule":
        return f"{where}: a submodule at commit {read.target}; its files are not in the mirror."
    if read.kind == "symlink":
        return f"{where}: a symlink, not followed. Its target:\n{fenced(read.target or '')}"
    if read.binary:
        return f"{where}: a binary file of {read.size} bytes; its content is not returned."
    if read.start_line is None or read.end_line is None:
        return f"{where}: an empty file."
    more = (
        f" The read stopped at a limit: continue from line {read.end_line + 1}."
        if read.truncated
        else ""
    )
    return (
        f"{where}: lines {read.start_line}-{read.end_line} of {read.total_lines}.{more}"
        f"{_symbol_spans(read)}\n{fenced(read.content or '', read.language or '')}"
    )


# --- The tools -----------------------------------------------------------------------------------

_RESULT_NOTE = (
    "Each hit carries its source kind, workspace and source, locator (repo@sha:path#Lstart-Lend"
    " for code; source:external_id#anchor otherwise), occurred_at (the record's time, which since"
    " and until filter on), and the chunk's text as snippet."
)

_ANCHOR_NOTE = (
    "A hit of a thread, a meeting or a document also carries anchor, its place in the record "
    "(msg-<id>, seg-<id>, summary/<heading trail>, action-items, a heading trail), link (the "
    "message's URL, else the record's) and at (when the chunk's content happened: its first "
    "message, its transcript window). record_id with anchor is an evidence reference: to cite the "
    "hit in record_decision, pass both as they are."
)

SEARCH_TOOL = Tool(
    name="search",
    title="Search",
    description=(
        "Search everything the token may read: code, message threads, meetings and documents. "
        "Ranks chunks by meaning and by words (mode hybrid, the default; dense: meaning only; "
        "lexical: words only). Filters: workspaces, sources, repositories, kinds (code_file, "
        "thread, meeting, document), since and until on occurred_at (ISO 8601 with a time zone), "
        "persons (records that name them: 'me', a person id, or an alias such as a mail address, "
        "a chat user id or a display name; merged persons count as one) and roles (with persons: "
        "author, recipient, mention, participant). "
        f"{_RESULT_NOTE} {_ANCHOR_NOTE} For exact, regex or symbol search in code use search_code."
        " For the meetings or the messages of a person, one result per meeting or message, use"
        " list_meetings and list_messages."
    ),
    arguments=SearchArguments,
    result=SearchResult,
    run=_tool_run(run_search),
    summarize=summarize_search,
)

SEARCH_CODE_TOOL = Tool(
    name="search_code",
    title="Search code",
    description=(
        "Search the code of the git repositories the token may read. Modes: semantic (the "
        "default: a question or identifiers, by meaning and by words), exact (a substring), regex "
        "(a Postgres regular expression, matched line by line like grep), symbol (definitions by "
        "name, e.g. on, or qualified name, e.g. Lamp.on). Filters: repositories, workspaces, "
        "paths (globs with .gitignore rules, e.g. src/**/*.py), languages (python, javascript, "
        "typescript, ruby, go). case_sensitive applies to exact, regex and symbol. "
        f"{_RESULT_NOTE} Hits also carry the ref, the commit, the symbol, and for exact and regex "
        "the line of the first match. Read more of a file with read_source."
    ),
    arguments=CodeSearchArguments,
    result=SearchResult,
    run=_tool_run(run_search_code),
    summarize=summarize_search,
)

GET_SYMBOL_TOOL = Tool(
    name="get_symbol",
    title="Get symbol",
    description=(
        "Find where a symbol is defined in code: by name (on) or qualified name (Lamp.on: names "
        "from the outermost definition, joined by '.' in every language), whole or as a prefix. "
        "Each definition carries its kind, parent, location as repo@sha:path#Lstart-Lend, ref, "
        "commit, occurred_at (the commit's time), and the code that defines it as snippet. "
        "Filters: repositories, workspaces, paths, languages."
    ),
    arguments=SymbolArguments,
    result=SymbolResult,
    run=_tool_run(run_get_symbol),
    summarize=summarize_symbols,
)

READ_SOURCE_TOOL = Tool(
    name="read_source",
    title="Read source",
    description=(
        "Read a file, a range of its lines, or a directory listing from a repository the token "
        "may read. By default at the pinned commit of the repository's default tracked branch, "
        "the commit search results are at; ref takes a branch or tag name, a full ref or a full "
        "commit id. path is relative to the repository root (empty for the root); absolute paths, "
        "'..' and control characters are refused. Lines are 1-based and inclusive, at most "
        f"{DEFAULT_READ_LIMITS.max_lines} lines and {DEFAULT_READ_LIMITS.max_chars} characters "
        "per call: when truncated, continue from end_line + 1. A binary file or one over the "
        "size limit returns no content; a symlink returns its target, not followed. The result "
        "carries the commit, its time, a locator repo@sha:path#Lstart-Lend and the definitions "
        "in the lines read."
    ),
    arguments=ReadSourceArguments,
    result=SourceRead,
    run=_tool_run(run_read_source),
    summarize=summarize_read,
)

QUERY_TOOLS: Sequence[Tool[Any, Any]] = (
    SEARCH_TOOL,
    SEARCH_CODE_TOOL,
    READ_SOURCE_TOOL,
    GET_SYMBOL_TOOL,
)
"""The query tools, in the order ``tools/list`` shows them."""
