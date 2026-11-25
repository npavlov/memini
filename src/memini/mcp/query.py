"""The query tools: ``search``, ``search_code``, ``get_symbol``, ``read_source`` and
``list_commits``.

Each tool authorizes the read (:func:`~memini.auth.authorization.authorize_read`), borrows a
connection from the pool and calls its function in :mod:`memini.query.service`, which the HTTP
routes call too. A :class:`~memini.query.QueryError` becomes an error result with its message, so
the model can correct its call; the messages never repeat an argument or stored content.

The structured result is the shape of :mod:`memini.query.model` or
:class:`~memini.query.SourceRead`, with its ``data_notice`` (:mod:`memini.provenance`). The text
content says the same for clients that read only text (:mod:`memini.mcp.text`): the locators, kinds
and times, the link and evidence reference (record id and anchor) of a hit inside a thread, a
meeting or a document, each on its own line with what a source chose escaped, and the stored text
in fenced blocks, as it is: data, never instructions.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import BaseModel

from memini.auth import ReadFilter, authorization
from memini.chunking import LANGUAGES
from memini.mcp.decisions import summarize_item
from memini.mcp.text import TEXT_NOTICE, fenced, inline
from memini.mcp.tools import Tool, ToolCall, ToolError
from memini.query import (
    CodeContext,
    CodeSearchArguments,
    CommitInfo,
    CommitList,
    ListCommitsArguments,
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
    run_list_commits,
    run_read_source,
    run_search,
    run_search_code,
)
from memini.query.model import GeneralSearchResult
from memini.query.reader import DEFAULT_READ_LIMITS

__all__ = [
    "GET_SYMBOL_TOOL",
    "LIST_COMMITS_TOOL",
    "QUERY_TOOLS",
    "READ_SOURCE_TOOL",
    "SEARCH_CODE_TOOL",
    "SEARCH_TOOL",
    "summarize_commits",
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


def _time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _count(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _head(count: int, one: str, many: str) -> str:
    """The first line: how many, and when there are any, :data:`TEXT_NOTICE`."""
    return f"{_count(count, one, many)}.{f' {TEXT_NOTICE}' if count else ''}"


def _belongs(context: CodeContext | None) -> list[str]:
    """What a file belongs to by the catalog, as one line. Nothing for a result without a
    context, and nothing for a file in no service: without a catalog every file is one, and the
    line would say nothing on every hit. The names are the catalog's handles."""
    if context is None or context.service is None:
        return []
    where = f"project {inline(context.project or '')}, service {inline(context.service)}"
    domains = ", ".join(
        inline(ref.domain if ref.project == context.project else f"{ref.project}:{ref.domain}")
        for ref in context.domains
    )
    return [f"belongs to: {where}; domains: {domains or 'none'}"]


def _hit(number: int, hit: SearchHit) -> str:
    details = [
        f"{hit.kind} in {hit.workspace}/{hit.source} ({hit.source_kind})",
        _time(hit.occurred_at),
    ]
    if hit.at != hit.occurred_at:
        details.append(f"chunk at {_time(hit.at)}")
    if hit.definition is not None:
        details.append(f"defines {hit.definition.kind} {inline(hit.definition.qualified_name)}")
    elif hit.symbol:
        details.append(f"in {inline(hit.symbol)}")
    if hit.match_line is not None:
        details.append(f"first match on line {hit.match_line}")
    if hit.score is not None:
        details.append(f"score {hit.score:.4g}")
    if hit.truncated:
        details.append("snippet cut short")
    lines = [f"{number}. {inline(hit.locator)}", "; ".join(details)]
    if hit.link is not None:
        lines.append(f"link: {inline(hit.link)}")
    if hit.anchor is not None:
        lines.append(f"evidence: record_id {hit.record_id}, anchor {inline(hit.anchor)}")
    lines += _belongs(hit.context)
    lines.append(fenced(hit.snippet, hit.language or ""))
    return "\n".join(lines)


def summarize_search(result: SearchResult) -> str:
    """The text content of a ``search`` or ``search_code`` result: every hit with its snippet."""
    lines = [_head(len(result.hits), "hit", "hits")]
    lines += [_hit(number, hit) for number, hit in enumerate(result.hits, start=1)]
    if isinstance(result, GeneralSearchResult) and result.decisions:
        lines.append(f"{len(result.decisions)} matching journal items.")
        lines.extend(summarize_item(item) for item in result.decisions)
    return "\n\n".join(lines)


def summarize_symbols(result: SymbolResult) -> str:
    """The text content of a ``get_symbol`` result: every definition with its code."""
    parts = [_head(len(result.definitions), "definition", "definitions")]
    for number, found in enumerate(result.definitions, start=1):
        cut = "; snippet cut short" if found.truncated else ""
        start = (
            f"; snippet from line {found.snippet_start_line}" if found.snippet_start_line else ""
        )
        belongs = "".join(f"{line}\n" for line in _belongs(found.context))
        parts.append(
            f"{number}. {found.kind} {inline(found.qualified_name)} at {inline(found.locator)}\n"
            f"in {found.workspace}/{found.repository}, {_time(found.occurred_at)}{start}{cut}\n"
            f"{belongs}{fenced(found.snippet, found.language or '')}"
        )
    return "\n\n".join(parts)


def _symbol_spans(read: SourceRead) -> str:
    spans = ", ".join(
        f"{inline(span.qualified_name)} ({span.kind}, lines {span.start_line}-{span.end_line})"
        for span in read.symbols
    )
    return f"\nDefinitions: {spans}." if spans else ""


def summarize_read(read: SourceRead) -> str:
    """The text content of a ``read_source`` result: what was read, then the file's lines, a
    directory's entries (one a line: name, kind, size) or a symlink's target in a fenced
    block."""
    at = f"{'pinned commit' if read.pinned else 'commit'} of {_time(read.occurred_at)}"
    ref = f", {inline(read.ref)}" if read.ref else ""
    belongs = "".join(f"; {line}" for line in _belongs(read.context))
    where = f"{inline(read.locator)} ({at}{ref}{belongs})"
    if read.kind == "directory":
        listed = read.entries or []
        shown = f"; the first {len(listed)} shown" if read.truncated else ""
        entries = "\n".join(
            f"{entry.name}{'/' if entry.kind == 'directory' else ''} ({entry.kind}"
            f"{f', {entry.size} bytes' if entry.size is not None else ''})"
            for entry in listed
        )
        return f"{where}: a directory{shown}. {TEXT_NOTICE}\n{fenced(entries)}"
    if read.kind == "submodule":
        target = inline(read.target or "")
        return f"{where}: a submodule at commit {target}; its files are not in the mirror."
    if read.kind == "symlink":
        return (
            f"{where}: a symlink, not followed. {TEXT_NOTICE} Its target:\n"
            f"{fenced(read.target or '')}"
        )
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
        f" {TEXT_NOTICE}{_symbol_spans(read)}\n{fenced(read.content or '', read.language or '')}"
    )


def _commit(number: int, commit: CommitInfo) -> str:
    details = [
        f"commit in {commit.workspace}/{commit.repository} ({commit.source_kind})",
        f"committed {_time(commit.occurred_at)}",
    ]
    if commit.authored_at != commit.occurred_at:
        details.append(f"written {_time(commit.authored_at)}")
    if commit.author.person_id is not None:
        details.append(f"author is person {commit.author.person_id}")
    if len(commit.parents) > 1:
        details.append(f"a merge of {len(commit.parents)} commits")
    shown = len(commit.changes)
    paths = _count(commit.changes_total, "path", "paths")
    details.append(
        f"{paths} changed" + (f", {shown} shown" if shown < commit.changes_total else "")
    )
    if commit.truncated:
        details.append("message cut short")
    # Everything the repository chose goes into the block: the author, the message, the paths.
    block = [f"Author: {commit.author.name} <{commit.author.email}>", "", commit.subject]
    if commit.body:
        block += ["", commit.body]
    if commit.changes:
        block += ["", *(f"{change.status} {change.path}" for change in commit.changes)]
    return "\n".join(
        [f"{number}. {inline(commit.locator)}", "; ".join(details), fenced("\n".join(block))]
    )


def summarize_commits(result: CommitList) -> str:
    """The text content of a ``list_commits`` result: what cut the list and how to continue, the
    repositories nothing was read of, then every commit with its author, message and paths."""
    parts = [_head(len(result.commits), "commit", "commits")]
    if result.next_until is not None:
        cause = (
            "The service read as many commits of a repository as it reads in one call"
            if result.cut_by == "walk_limit"
            else "The list is cut at its limit"
        )
        parts.append(
            f"{cause}: older commits may match too. To continue, call again with until"
            f" {_time(result.next_until)} and the other arguments as they were."
        )
    unread = [
        f"{walk.workspace}/{walk.repository}" for walk in result.repositories if not walk.heads
    ]
    if unread:
        parts.append(f"No synced commit yet, so nothing was read of: {', '.join(unread)}.")
    parts += [_commit(number, commit) for number, commit in enumerate(result.commits, start=1)]
    return "\n\n".join(parts)


# --- The tools -----------------------------------------------------------------------------------

_RESULT_NOTE = (
    "Each hit carries its source kind, workspace and source, locator (repo@sha:path#Lstart-Lend"
    " for code; source:external_id#anchor otherwise), occurred_at (the record's time, which since"
    " and until filter on), and the chunk's text as snippet."
)

_CATALOG_NOTE = (
    "projects, domains (a domain includes its subdomains; one is domain/subdomain) and services "
    "narrow to code the catalog names, by its path masks as they are now; a name it does not "
    "hold is an error, get_catalog lists them. A hit of a file of a repository carries context: "
    "the project and service it is in and the domains that name it."
)

_SEARCH_CATALOG_NOTE = (
    "projects and domains (a domain includes its subdomains; one is domain/subdomain) narrow to "
    "the code the catalog names, by its path masks as they are now, and to the threads, meetings "
    "and documents linked to them: pushed with them, by a default of their source or stream, or "
    "else by the domains the model gave the decisions taken from them. services narrows to code. "
    "A name the catalog does not hold is an error, get_catalog lists them. A hit of a file of a "
    "repository carries context: the project and service it is in and the domains that name it."
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
        "thread, meeting, document, decision, open_question), since and until (ISO 8601), "
        "persons (records that name them: 'me', a person id, or an alias such as a mail address, "
        "a chat user id or a display name; merged persons count as one) and roles (with persons: "
        "author, recipient, mention, participant). Active items matching statement words or "
        "citing retrieved records are in decisions. Use get_decision for their full history. "
        f"{_SEARCH_CATALOG_NOTE} "
        "With projects or domains, decisions holds only items under those projects and in those "
        "domains, as list_decisions reads them; with services, only items citing retrieved "
        "records. "
        f"{_RESULT_NOTE} {_ANCHOR_NOTE} For exact, regex or symbol search in code use search_code."
        " For the meetings or the messages of a person, one result per meeting or message, use"
        " list_meetings and list_messages."
    ),
    arguments=SearchArguments,
    result=GeneralSearchResult,
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
        "paths (globs with .gitignore rules, e.g. src/**/*.py), "
        f"languages ({', '.join(LANGUAGES)}). case_sensitive applies to exact, regex and symbol. "
        f"{_CATALOG_NOTE} "
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
        "commit, occurred_at (the commit's time), the code that defines it as snippet, and "
        "context: the project, service and domains its file belongs to by the catalog. "
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
        "carries the commit, its time, a locator repo@sha:path#Lstart-Lend, the definitions "
        "in the lines read, and context: the project, service and domains the path belongs to "
        "by the catalog."
    ),
    arguments=ReadSourceArguments,
    result=SourceRead,
    run=_tool_run(run_read_source),
    summarize=summarize_read,
)

LIST_COMMITS_TOOL = Tool(
    name="list_commits",
    title="List commits",
    description=(
        "Who changed what, and when: the commit history of the git repositories the token may "
        "read, newest first, read from the service's mirror when asked and never stored. Each "
        "repository is walked from its pinned commit, the one search results are at. Filters: "
        "repositories, workspaces, paths (globs with .gitignore rules, e.g. src/fees/** or "
        "src/fees/calc.py: commits that changed such a path, listed with those paths), author "
        "(text in the author's name or mail address), since and until (ISO 8601 with a time "
        "zone, on the committer time), text (words in the message, as written; there is no "
        "search by meaning over messages). Each commit carries its workspace and repository, "
        "sha and short_sha, a locator repo@sha, occurred_at (the committer time) and "
        "authored_at, the author as git shows it and the person the author resolves to "
        "(person_id), subject, body, parents, and the changed paths with their status. A list "
        "cut by a bound says so (truncated, cut_by) and gives next_until: pass it as until to "
        "continue. A walk that runs past the service's time limit is an error: narrow it with "
        "repositories, paths, since or until. Read a file as a commit left it with read_source "
        "and the sha as ref."
    ),
    arguments=ListCommitsArguments,
    result=CommitList,
    run=_tool_run(run_list_commits),
    summarize=summarize_commits,
)

QUERY_TOOLS: Sequence[Tool[Any, Any]] = (
    SEARCH_TOOL,
    SEARCH_CODE_TOOL,
    READ_SOURCE_TOOL,
    GET_SYMBOL_TOOL,
    LIST_COMMITS_TOOL,
)
"""The query tools, in the order ``tools/list`` shows them."""
