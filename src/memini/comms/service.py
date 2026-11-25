"""The comms tools as functions: from validated arguments to a result, or a
:class:`~memini.query.QueryError`.

One function per tool, for the MCP tools (:mod:`memini.mcp.comms`) and the HTTP routes
(:mod:`memini.api.comms`) alike. The reads (:func:`run_list_meetings`, :func:`run_get_meeting`,
:func:`run_list_messages`, :func:`run_get_thread`) take the caller's
:class:`~memini.auth.ReadFilter`; the write (:func:`run_put_document`) its
:class:`~memini.auth.WriteAccess` with the ``ingest`` scope. Each takes a connection with no
transaction open and leaves it idle.

**Whose.** ``person`` is ``"me"``, a person id or an alias (:func:`memini.persons.resolve_persons`)
in the workspaces the token may read (narrowed by ``workspaces``). It stands for its canonical
person and everyone merged into it (:func:`memini.persons.member_ids`): their links
(``record_person``) are read. "Me" is resolved in each workspace on its own.

**Meetings** are the ``meeting`` records where the person is a ``participant`` (listed, or a
speaker of the transcript). **Messages** are the messages of ``thread`` records where the person
has one of the roles asked for: ``recipient`` (to or cc), ``mention`` or ``author``; a link names
the message by its id (its anchor), so a result is one message, not a thread.

**A query** is matched by words, through the lexical retriever of search
(:func:`memini.search.search`, mode ``lexical``: the stemmed ``tsvector`` of the chunks, English
and Russian, identifiers cut into words), over the chunks of the person's meetings or threads
only. A meeting comes back when a chunk of its notes holds a word of the query, ranked by its best
chunk; a message when a chunk that holds the message does (a chunk of a thread is a group of
consecutive messages, located by the rows of the normalized text). The words decide, not a
similarity: a dense retriever ranks every candidate, and with no threshold that means anything
across models it cannot say that a meeting is not about the topic, which the M2 check asks. No
language model takes part. For a ranking by meaning, ``search`` takes the ``persons`` and
``roles`` filters. A query looks at the best :data:`~memini.search.MAX_LIMIT` chunks. Without a
query, the listing is metadata and time only, newest first, and needs no embedder.

Refusals, beyond those of search (:func:`memini.query.query_errors`):

| Error | Status | Code |
|---|---|---|
| "me" and no person is marked as me in the workspaces read | 404 | ``no_me`` |
| no such person, workspace, source or record the caller may read | 404 | ``not_found`` |
| a record named by source and external id in two workspaces | 422 | ``ambiguous_record`` |
| a record of another kind (``get_meeting`` of a thread) | 422 | ``wrong_kind`` |
| a payload the parser of this version does not read | 409 | ``unreadable_record`` |
| ``put_document`` into a source that is not a push source | 409 | ``not_ingestible`` |
| ``put_document``: the errors of the push API (:mod:`memini.api.ingest`) | 413, 422, 409, 503 | |
"""

import logging
from collections.abc import Collection, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, cast
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from memini import ingest
from memini.auth import ReadFilter, WriteAccess
from memini.comms.model import (
    ActionItemInfo,
    GetMeetingArguments,
    GetThreadArguments,
    ListedMeeting,
    ListedMessage,
    ListMeetingsArguments,
    ListMessagesArguments,
    MeetingList,
    MeetingRecord,
    MessageInfo,
    MessageList,
    MessageRole,
    PersonInfo,
    PutDocumentArguments,
    PutDocumentResult,
    RecordArguments,
    RecordPerson,
    SegmentInfo,
    StoredAs,
    ThreadRecord,
)
from memini.embeddings import EmbeddingError
from memini.ids import hash_fields, record_id
from memini.index import IndexMismatchError
from memini.payloads import Meeting, Message, PayloadError, Thread, load_payload, render_thread
from memini.persons import ME, member_ids, parse_person_ref, resolve_persons
from memini.query import QueryError, QueryServices, query_errors
from memini.search import MAX_LIMIT, Hit, SearchFilters, SearchQuery, search
from memini.search.catalog import catalog_groups, load_code_maps
from memini.search.model import source_kind
from memini.search.retrieval import RECORD_ROW, Params, linked_records
from memini.sensitivity import sensitive_message, sensitive_records

__all__ = [
    "DOCUMENT_ID_DOMAIN",
    "NOT_INDEXED_NOTE",
    "document_external_id",
    "run_get_meeting",
    "run_get_thread",
    "run_list_meetings",
    "run_list_messages",
    "run_put_document",
]

type Conn = AsyncConnection[TupleRow]

log = logging.getLogger(__name__)

DOCUMENT_ID_DOMAIN: Final = "memini.document.v1"
"""Domain tag of the external id ``put_document`` derives from a title and a text."""

NOT_INDEXED_NOTE: Final = (
    "No embedding provider is configured, so the document was stored without chunks and search"
    " does not find it yet. Set MEMINI_EMBEDDING_PROVIDER, then run memini reindex."
)


# --- Whose ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Whose:
    """The persons a listing is for, resolved."""

    workspaces: dict[UUID, str]
    """The workspaces read, by id, with their names."""
    persons: list[PersonInfo]
    roots: tuple[UUID, ...]
    """The canonical persons the reference names, at most one per workspace."""
    members: tuple[UUID, ...]
    """They and everyone merged into them: the persons whose links count."""


def _scope(access: ReadFilter | WriteAccess, alias: str, *, source: bool = False) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
        source_id=sql.Identifier(alias, "source_id") if source else None,
    )


def _record_scope(access: ReadFilter, alias: str = "r") -> sql.Composable:
    """:func:`_scope` of a record: a record marked sensitive is read only when asked for."""
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
        source_id=sql.Identifier(alias, "source_id"),
        sensitive=sql.Identifier(alias, "sensitive"),
    )


_RECORD_SOURCES: Final = sql.SQL(
    "SELECT s.id FROM source s WHERE s.workspace_id = ANY(%s) AND s.kind <> 'repository' AND {}"
)


async def _catalog_filter(
    conn: Conn,
    access: ReadFilter,
    workspace_ids: Collection[UUID],
    projects: Sequence[str] | None,
    domains: Sequence[str] | None,
) -> tuple[sql.Composable, dict[str, Any]]:
    """The condition on ``record r`` of the ``projects`` and ``domains`` filters, with its
    parameters: the record is linked to them as search links a record other than code
    (:func:`memini.search.retrieval.linked_records`). ``TRUE`` without them.

    Raises:
        QueryError: a name no catalog of the workspaces read holds (``unknown_project``,
            ``unknown_domain``).
    """
    if projects is None and domains is None:
        return sql.SQL("TRUE"), {}
    spaces = list(workspace_ids)
    scope = access.where(
        workspace_id=sql.Identifier("s", "workspace_id"),
        owner_id=sql.Identifier("s", "owner_id"),
        source_id=sql.Identifier("s", "id"),
    )
    cur = await conn.execute(_RECORD_SOURCES.format(scope), (spaces,))
    sources = [row[0] for row in await cur.fetchall()]
    with query_errors():
        groups = catalog_groups(
            await load_code_maps(conn, access, spaces),
            projects=projects,
            domains=domains,
            sources=sources,
        )
    param = Params()
    parts = [
        sql.SQL("FALSE")
        if group.records is None
        else linked_records(group.records, param, RECORD_ROW)
        for group in groups
    ]
    return sql.SQL("({})").format(sql.SQL(" AND ").join(parts)), param.values


def _wide(access: ReadFilter) -> ReadFilter:
    """The filter a listing reads with: every record, sensitive ones too, which it then takes out
    and counts (``left_out``) unless the caller asked for them."""
    return access if access.include_sensitive else replace(access, include_sensitive=True)


async def _sensitive(conn: Conn, access: ReadFilter, record_ids: Sequence[str]) -> frozenset[str]:
    """The records of ``record_ids`` the caller did not ask for: those marked sensitive."""
    if access.include_sensitive:
        return frozenset()
    return await sensitive_records(conn, record_ids)


async def _workspaces(
    conn: Conn, access: ReadFilter, names: Sequence[str] | None
) -> dict[UUID, str]:
    query = sql.SQL(
        "SELECT w.id, w.name FROM workspace w WHERE {access}"
        " AND (%s::text[] IS NULL OR w.name = ANY(%s)) ORDER BY w.name"
    ).format(
        access=access.where(
            workspace_id=sql.Identifier("w", "id"), owner_id=sql.Identifier("w", "owner_id")
        )
    )
    names_list = None if names is None else list(names)
    cur = await conn.execute(query, (names_list, names_list))
    return {row[0]: row[1] for row in await cur.fetchall()}


_PERSONS: Final = sql.SQL("""
SELECT p.id, p.workspace_id, p.display_name,
       coalesce(p.id = (SELECT person_root(m.id) FROM person m
                        WHERE m.workspace_id = p.workspace_id AND m.principal_id = %s), false)
FROM person p WHERE p.id = ANY(%s) AND {access}
ORDER BY p.workspace_id, p.display_name, p.id
""")


async def _whose(
    conn: Conn, access: ReadFilter, persons: Sequence[str], workspaces: Sequence[str] | None
) -> _Whose:
    """The persons ``persons`` name in the workspaces read; see the module."""
    readable = await _workspaces(conn, access, workspaces)
    refs = [parse_person_ref(person) for person in persons]
    found = await resolve_persons(conn, access, readable, refs)
    roots = tuple(sorted(root for each in found.values() for root in each))
    if not roots:
        if all(ref == ME for ref in refs):
            raise QueryError(
                404,
                "no_me",
                "No person is marked as me in the workspaces read: mark one with"
                " memini person me (PUT /v1/workspaces/{workspace}/persons/me), or name a person.",
            )
        raise QueryError(404, "not_found", "No such person.")
    cur = await conn.execute(
        _PERSONS.format(access=_scope(access, "p")), (access.principal.principal_id, list(roots))
    )
    resolved = [
        PersonInfo(id=row[0], workspace=readable[row[1]], display_name=row[2], is_me=row[3])
        for row in await cur.fetchall()
    ]
    return _Whose(
        workspaces={workspace_id: readable[workspace_id] for workspace_id in found},
        persons=resolved,
        roots=roots,
        members=await member_ids(conn, roots),
    )


# --- Records -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Record:
    """A stored record with what a result says of it."""

    id: str
    workspace_id: UUID
    workspace: str
    source: str
    source_kind: str
    kind: str
    external_id: str
    external_url: str | None
    occurred_at: datetime
    payload: bytes | None

    def locator(self, anchor: str | None = None) -> str:
        """``source:external_id#anchor``, as search hits name a place."""
        place = "" if anchor is None else f"#{anchor}"
        return f"{self.source}:{self.external_id}{place}"

    def source_fields(self) -> dict[str, Any]:
        """Where the record comes from: what a message of it carries with its own id and time."""
        return {
            "workspace": self.workspace,
            "source": self.source,
            "source_kind": source_kind(self.source_kind),
            "external_id": self.external_id,
            "url": self.external_url,
        }

    def fields(self) -> dict[str, Any]:
        """The fields every result of the record carries: its own id and time too."""
        return self.source_fields() | {"id": self.id, "occurred_at": self.occurred_at}


_RECORD_COLUMNS: Final = sql.SQL(
    "r.id, r.workspace_id, w.name, s.name, s.kind, r.kind, r.external_id, r.external_url,"
    " r.occurred_at, r.payload"
)
_RECORD_FROM: Final = sql.SQL(
    "record r JOIN source s ON s.id = r.source_id JOIN workspace w ON w.id = r.workspace_id"
)


def _record(row: Sequence[Any]) -> _Record:
    return _Record(
        id=row[0],
        workspace_id=row[1],
        workspace=row[2],
        source=row[3],
        source_kind=row[4],
        kind=row[5],
        external_id=row[6],
        external_url=row[7],
        occurred_at=row[8],
        payload=None if row[9] is None else bytes(row[9]),
    )


async def _records(conn: Conn, access: ReadFilter, ids: Collection[str]) -> dict[str, _Record]:
    """The live records ``ids`` the caller may read, by id."""
    if not ids:
        return {}
    query = sql.SQL(
        "SELECT {columns} FROM {from_} WHERE r.id = ANY(%s) AND r.deleted_at IS NULL AND {access}"
    ).format(columns=_RECORD_COLUMNS, from_=_RECORD_FROM, access=_record_scope(access))
    cur = await conn.execute(query, (sorted(ids),))
    return {row[0]: _record(row) for row in await cur.fetchall()}


def _parsed(record: _Record) -> Thread | Meeting | None:
    """The record's payload, parsed; ``None`` when it does not parse with this version (it did
    when it was stored), which is logged with the record's id."""
    if record.payload is None:
        return None
    try:
        parsed = load_payload(record.kind, record.payload)
    except (PayloadError, ValueError) as error:
        log.warning("record %s does not parse: %s", record.id, error)
        return None
    return parsed if isinstance(parsed, Thread | Meeting) else None


def _unreadable() -> QueryError:
    return QueryError(
        409,
        "unreadable_record",
        "The record's payload does not parse with this version of the service; the server log"
        " names the field.",
    )


# --- Searching by words --------------------------------------------------------------------------


async def _matches(
    conn: Conn, services: QueryServices, access: ReadFilter, text: str, filters: SearchFilters
) -> list[Hit]:
    """The chunks that hold words of ``text`` under ``filters``, best first: the lexical
    retriever of search, over at most :data:`~memini.search.MAX_LIMIT` chunks."""
    embedder = services.embedder
    if embedder is None:
        raise QueryError(
            503,
            "embeddings_not_configured",
            "A query reads the index, which needs an embedding provider, and none is configured:"
            " set MEMINI_EMBEDDING_PROVIDER. Without a query the listing works.",
        )
    with query_errors():
        query = SearchQuery(text=text, mode="lexical", limit=MAX_LIMIT, filters=filters)
        return await search(conn, embedder, access, query, options=services.search_options)


def _cut(text: str, chars: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= chars else (text[:chars], True)


# --- list_meetings -------------------------------------------------------------------------------

_MEETINGS: Final = sql.SQL("""
SELECT {columns} FROM {from_}
WHERE r.kind = 'meeting' AND r.deleted_at IS NULL AND {access}
  AND r.workspace_id = ANY(%(workspaces)s)
  AND EXISTS (SELECT 1 FROM record_person rp
              WHERE rp.record_id = r.id AND rp.person_id = ANY(%(persons)s)
                AND rp.role = 'participant')
  AND (%(since)s::timestamptz IS NULL OR r.occurred_at >= %(since)s)
  AND (%(until)s::timestamptz IS NULL OR r.occurred_at < %(until)s)
  AND (%(sources)s::text[] IS NULL OR s.name = ANY(%(sources)s))
  AND {catalog}
ORDER BY r.occurred_at DESC, r.id
LIMIT %(limit)s
""")


def _listed_meeting(record: _Record, hit: Hit | None) -> ListedMeeting:
    """A meeting of a listing; ``hit``: the chunk of its notes that matched the query best."""
    meeting = _parsed(record)
    if not isinstance(meeting, Meeting):
        meeting = None
    anchor = None if hit is None else hit.anchor
    return ListedMeeting(
        **record.fields(),
        locator=record.locator(anchor),
        link=record.external_url if hit is None else hit.link,
        title=None if meeting is None else meeting.title,
        started_at=None if meeting is None else meeting.started_at,
        ended_at=None if meeting is None else meeting.ended_at,
        participants=[] if meeting is None else list(meeting.participants),
        anchor=anchor,
        snippet=None if hit is None else hit.snippet,
        truncated=False if hit is None else hit.truncated,
        score=None if hit is None else hit.score,
    )


async def run_list_meetings(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: ListMeetingsArguments
) -> MeetingList:
    """``list_meetings``: the meetings of persons (default: me), by time, topic and catalog."""
    wide = _wide(access)
    async with conn.transaction():
        whose = await _whose(conn, wide, arguments.persons, arguments.workspaces)
    if arguments.query is None:
        async with conn.transaction():
            catalog, catalog_params = await _catalog_filter(
                conn, wide, whose.workspaces, arguments.projects, arguments.domains
            )
            query = _MEETINGS.format(
                columns=_RECORD_COLUMNS,
                from_=_RECORD_FROM,
                access=_record_scope(wide),
                catalog=catalog,
            )
            cur = await conn.execute(
                query,
                {
                    "workspaces": list(whose.workspaces),
                    "persons": list(whose.members),
                    "since": arguments.since,
                    "until": arguments.until,
                    "sources": arguments.sources,
                    "limit": arguments.limit,
                }
                | catalog_params,
            )
            rows = await cur.fetchall()
        listed = [(_record(row), None) for row in rows]
    else:
        hits = await _matches(
            conn,
            services,
            wide,
            arguments.query,
            SearchFilters(
                workspaces=tuple(whose.workspaces),
                sources=None if arguments.sources is None else tuple(arguments.sources),
                kinds=("meeting",),
                persons=whose.roots,
                roles=("participant",),
                since=arguments.since,
                until=arguments.until,
                projects=None if arguments.projects is None else tuple(arguments.projects),
                domains=None if arguments.domains is None else tuple(arguments.domains),
            ),
        )
        best: dict[str, Hit] = {}
        for hit in hits:
            best.setdefault(hit.record_id, hit)
        chosen = list(best.items())[: arguments.limit]
        async with conn.transaction():
            records = await _records(conn, wide, [record_id for record_id, _ in chosen])
        listed = [(records[record_id], hit) for record_id, hit in chosen if record_id in records]
    hidden = await _sensitive(conn, access, [record.id for record, _ in listed])
    return MeetingList(
        persons=whose.persons,
        items=[_listed_meeting(record, hit) for record, hit in listed if record.id not in hidden],
        left_out=sum(record.id in hidden for record, _ in listed),
    )


# --- list_messages -------------------------------------------------------------------------------

_MESSAGE_LINKS: Final = sql.SQL("""
SELECT rp.record_id, rp.anchor, array_agg(DISTINCT rp.role ORDER BY rp.role),
       max(rp.occurred_at) AS at
FROM record_person rp
JOIN record r ON r.id = rp.record_id
JOIN source s ON s.id = r.source_id
WHERE rp.person_id = ANY(%(persons)s) AND rp.role = ANY(%(roles)s)
  AND rp.record_kind = 'thread' AND rp.anchor IS NOT NULL
  AND r.deleted_at IS NULL AND {access} AND r.workspace_id = ANY(%(workspaces)s)
  AND (%(since)s::timestamptz IS NULL OR rp.occurred_at >= %(since)s)
  AND (%(until)s::timestamptz IS NULL OR rp.occurred_at < %(until)s)
  AND (%(sources)s::text[] IS NULL OR s.name = ANY(%(sources)s))
  AND (%(records)s::text[] IS NULL OR rp.record_id = ANY(%(records)s))
  AND {catalog}
GROUP BY rp.record_id, rp.anchor
ORDER BY at DESC, rp.record_id, rp.anchor
LIMIT %(limit)s
""")


@dataclass(frozen=True, slots=True)
class _Link:
    """A message where the person has some of the roles asked for."""

    record_id: str
    message_id: str
    roles: tuple[MessageRole, ...]


async def _message_links(
    conn: Conn,
    access: ReadFilter,
    whose: _Whose,
    roles: Sequence[MessageRole],
    arguments: ListMessagesArguments,
    *,
    records: Sequence[str] | None,
    limit: int | None,
) -> list[_Link]:
    catalog, catalog_params = await _catalog_filter(
        conn, access, whose.workspaces, arguments.projects, arguments.domains
    )
    query = _MESSAGE_LINKS.format(access=_record_scope(access), catalog=catalog)
    cur = await conn.execute(
        query,
        catalog_params
        | {
            "persons": list(whose.members),
            "roles": list(roles),
            "workspaces": list(whose.workspaces),
            "since": arguments.since,
            "until": arguments.until,
            "sources": arguments.sources,
            "records": None if records is None else list(records),
            "limit": limit,
        },
    )
    return [
        _Link(record_id=row[0], message_id=row[1], roles=tuple(cast("list[MessageRole]", row[2])))
        for row in await cur.fetchall()
    ]


def _message_info(record: _Record, message: Message, *, chars: int | None = None) -> dict[str, Any]:
    text, truncated = (message.text, False) if chars is None else _cut(message.text, chars)
    anchor = f"msg-{message.id}"
    return {
        "id": message.id,
        "anchor": anchor,
        "locator": record.locator(anchor),
        "link": message.url or record.external_url,
        "author": message.author,
        "occurred_at": message.sent_at,
        "recipients": list(message.recipients),
        "cc": list(message.cc),
        "mentions": list(message.mentions),
        "text": text,
        "truncated": truncated,
    }


class _Threads:
    """The threads of a listing, parsed once each, with the rows of their messages."""

    def __init__(self, records: Mapping[str, _Record]) -> None:
        self.records = records
        self._parsed: dict[str, tuple[Thread, dict[str, int], list[tuple[int, int]]] | None] = {}

    def _load(self, record_id: str) -> tuple[Thread, dict[str, int], list[tuple[int, int]]] | None:
        if record_id not in self._parsed:
            record = self.records.get(record_id)
            thread = None if record is None else _parsed(record)
            if isinstance(thread, Thread):
                lines = [(rows.first + 1, rows.last + 1) for rows in render_thread(thread).messages]
                order = {message.id: index for index, message in enumerate(thread.messages)}
                self._parsed[record_id] = (thread, order, lines)
            else:
                self._parsed[record_id] = None
        return self._parsed[record_id]

    def message(self, record_id: str, message_id: str) -> tuple[Thread, Message, int, int] | None:
        """The thread, the message and its first and last line in the normalized text
        (1-based, as chunks count them)."""
        loaded = self._load(record_id)
        if loaded is None:
            return None
        thread, order, lines = loaded
        index = order.get(message_id)
        if index is None:
            return None
        first, last = lines[index]
        return thread, thread.messages[index], first, last


def _listed_message(
    record: _Record,
    thread: Thread,
    message: Message,
    roles: Sequence[MessageRole],
    *,
    chars: int,
    score: float | None,
) -> ListedMessage:
    return ListedMessage(
        **record.source_fields(),
        **_message_info(record, message, chars=chars),
        record_id=record.id,
        title=thread.title,
        channel=thread.channel,
        roles=list(roles),
        score=score,
    )


def _overlaps(hit: Hit, first: int, last: int) -> bool:
    return (
        hit.start_line is not None
        and hit.end_line is not None
        and hit.start_line <= last
        and first <= hit.end_line
    )


async def run_list_messages(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: ListMessagesArguments
) -> MessageList:
    """``list_messages``: messages to, mentioning or from persons (default: to or mentioning
    me), by source, time, topic and catalog; one result per message."""
    roles = tuple(dict.fromkeys(arguments.roles))
    chars = services.search_options.snippet_chars
    wide = _wide(access)
    async with conn.transaction():
        whose = await _whose(conn, wide, arguments.persons, arguments.workspaces)
    if arguments.query is None:
        async with conn.transaction():
            links = await _message_links(
                conn, wide, whose, roles, arguments, records=None, limit=arguments.limit
            )
            threads = _Threads(await _records(conn, wide, {link.record_id for link in links}))
        messages: list[ListedMessage] = []
        for link in links:
            found = threads.message(link.record_id, link.message_id)
            if found is not None:
                thread, message, _, _ = found
                record = threads.records[link.record_id]
                messages.append(
                    _listed_message(record, thread, message, link.roles, chars=chars, score=None)
                )
        return await _message_list(conn, access, whose, messages)

    hits = await _matches(
        conn,
        services,
        wide,
        arguments.query,
        SearchFilters(
            workspaces=tuple(whose.workspaces),
            sources=None if arguments.sources is None else tuple(arguments.sources),
            kinds=("thread",),
            persons=whose.roots,
            roles=roles,
            # A thread's time is its latest message, so a thread before `since` holds no message
            # after it; `until` cannot narrow threads, whose earlier messages may be in range.
            since=arguments.since,
            projects=None if arguments.projects is None else tuple(arguments.projects),
            domains=None if arguments.domains is None else tuple(arguments.domains),
        ),
    )
    by_record: dict[str, list[tuple[int, Hit]]] = {}
    for rank, hit in enumerate(hits):
        by_record.setdefault(hit.record_id, []).append((rank, hit))
    if not by_record:
        return MessageList(persons=whose.persons, items=[])
    async with conn.transaction():
        links = await _message_links(
            conn, wide, whose, roles, arguments, records=list(by_record), limit=None
        )
        threads = _Threads(await _records(conn, wide, {link.record_id for link in links}))
    ranked: list[tuple[int, datetime, str, ListedMessage]] = []
    for link in links:
        found = threads.message(link.record_id, link.message_id)
        if found is None:
            continue
        thread, message, first, last = found
        match = next(
            ((rank, hit) for rank, hit in by_record[link.record_id] if _overlaps(hit, first, last)),
            None,
        )
        if match is None:
            continue  # the thread matched, but not where this message is
        rank, hit = match
        record = threads.records[link.record_id]
        listed = _listed_message(record, thread, message, link.roles, chars=chars, score=hit.score)
        ranked.append((rank, message.sent_at, message.id, listed))
    ranked.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return await _message_list(
        conn, access, whose, [entry[3] for entry in ranked[: arguments.limit]]
    )


async def _message_list(
    conn: Conn, access: ReadFilter, whose: _Whose, messages: Sequence[ListedMessage]
) -> MessageList:
    """The listing, without the messages of threads the caller did not ask for, counted."""
    hidden = await _sensitive(conn, access, [message.record_id for message in messages])
    return MessageList(
        persons=whose.persons,
        items=[message for message in messages if message.record_id not in hidden],
        left_out=sum(message.record_id in hidden for message in messages),
    )


# --- get_meeting, get_thread ---------------------------------------------------------------------

_BY_ID: Final = sql.SQL(
    "SELECT {columns} FROM {from_} WHERE r.id = %(id)s AND r.deleted_at IS NULL AND {access}"
)

_KIND_TOOLS: Final = {"meeting": "get_meeting", "thread": "get_thread"}


async def _find(conn: Conn, access: ReadFilter, arguments: RecordArguments, kind: str) -> _Record:
    """The live record the arguments name, of ``kind``; see the module for the refusals."""
    template = _BY_ID
    params = {"id": arguments.id}
    query = template.format(
        columns=_RECORD_COLUMNS, from_=_RECORD_FROM, access=_record_scope(access)
    )
    cur = await conn.execute(query, params)
    rows = await cur.fetchall()
    if not rows and not access.include_sensitive:
        # Say why, so the reader knows to ask: the record is there, marked sensitive.
        marked = template.format(
            columns=_RECORD_COLUMNS,
            from_=_RECORD_FROM,
            access=_record_scope(replace(access, include_sensitive=True)),
        )
        cur = await conn.execute(marked, params)
        if await cur.fetchall():
            raise QueryError(403, "sensitive_record", sensitive_message(kind))
    if not rows:
        raise QueryError(404, "not_found", f"No such {kind}.")
    record = _record(rows[0])
    if record.kind != kind:
        other = _KIND_TOOLS.get(record.kind)
        hint = f": use {other}" if other else ""
        raise QueryError(422, "wrong_kind", f"The record is a {record.kind}, not a {kind}{hint}.")
    return record


_RECORD_PERSONS: Final = sql.SQL("""
WITH linked AS (
    SELECT person_root(rp.person_id) AS id, rp.role FROM record_person rp
    WHERE rp.record_id = %(record)s
)
SELECT p.id, p.display_name, array_agg(DISTINCT l.role ORDER BY l.role),
       coalesce(p.id = (SELECT person_root(m.id) FROM person m
                        WHERE m.workspace_id = %(workspace)s AND m.principal_id = %(principal)s),
                false)
FROM linked l JOIN person p ON p.id = l.id
WHERE {access}
GROUP BY p.id, p.display_name
ORDER BY p.display_name, p.id
""")


async def _record_persons(conn: Conn, access: ReadFilter, record: _Record) -> list[RecordPerson]:
    """The canonical persons a record names, with their roles in it."""
    cur = await conn.execute(
        _RECORD_PERSONS.format(access=_scope(access, "p")),
        {
            "record": record.id,
            "workspace": record.workspace_id,
            "principal": access.principal.principal_id,
        },
    )
    return [
        RecordPerson(
            id=row[0],
            workspace=record.workspace,
            display_name=row[1],
            roles=row[2],
            is_me=row[3],
        )
        for row in await cur.fetchall()
    ]


async def run_get_meeting(
    conn: Conn, access: ReadFilter, arguments: GetMeetingArguments
) -> MeetingRecord:
    """``get_meeting``: a meeting's notes; the transcript on request."""
    async with conn.transaction():
        record = await _find(conn, access, arguments, "meeting")
        persons = await _record_persons(conn, access, record)
    meeting = _parsed(record)
    if not isinstance(meeting, Meeting):
        raise _unreadable()
    transcript = None
    if arguments.include_transcript:
        transcript = [
            SegmentInfo(
                id=segment.id,
                anchor=f"seg-{segment.id}",
                speaker=segment.speaker,
                start=segment.start,
                end=segment.end,
                text=segment.text,
            )
            for segment in meeting.transcript
        ]
    return MeetingRecord(
        **record.fields(),
        locator=record.locator(),
        link=record.external_url,
        title=meeting.title,
        started_at=meeting.started_at,
        ended_at=meeting.ended_at,
        participants=list(meeting.participants),
        persons=persons,
        summary=meeting.summary,
        action_items=[
            ActionItemInfo(text=item.text, owner=item.owner, due=item.due, id=item.id)
            for item in meeting.action_items
        ],
        segments=len(meeting.transcript),
        transcript=transcript,
    )


async def run_get_thread(
    conn: Conn, access: ReadFilter, arguments: GetThreadArguments
) -> ThreadRecord:
    """``get_thread``: a whole thread, its messages in order."""
    async with conn.transaction():
        record = await _find(conn, access, arguments, "thread")
        persons = await _record_persons(conn, access, record)
    thread = _parsed(record)
    if not isinstance(thread, Thread):
        raise _unreadable()
    return ThreadRecord(
        **record.fields(),
        locator=record.locator(),
        link=record.external_url,
        title=thread.title,
        channel=thread.channel,
        persons=persons,
        messages=[MessageInfo(**_message_info(record, message)) for message in thread.messages],
    )


# --- put_document --------------------------------------------------------------------------------


def document_external_id(title: str, text: str) -> str:
    """The external id ``put_document`` gives a document without one: ``doc-`` and 16 hex
    digits of a hash of its title and text, so the same document sent again is the same record
    and another text another one."""
    return "doc-" + hash_fields(DOCUMENT_ID_DOMAIN, title.encode(), text.encode())[:16]


@contextmanager
def _ingest_errors() -> Generator[None]:
    """The refusals of :func:`memini.ingest.ingest` as the push API answers them."""
    try:
        yield
    except ingest.BatchTooLargeError as error:
        raise QueryError(413, error.code, f"The document is too large: {error}.") from None
    except ingest.InvalidBatchError as error:
        raise QueryError(422, "invalid_batch", f"The document is not valid: {error}.") from None
    except ingest.NotIngestibleError:
        raise QueryError(
            409, "not_ingestible", "Records of this source come from its repository."
        ) from None
    except (ingest.StreamChangedError, ingest.BatchConflictError):
        raise QueryError(
            409,
            "ingest_conflict",
            "Another batch changed the same record; add the document again.",
        ) from None
    except IndexMismatchError as error:
        raise QueryError(503, "index_mismatch", f"Nothing was stored: {error}.") from None
    except EmbeddingError as error:
        log.warning("a document could not be embedded: %s", error)
        raise QueryError(
            503,
            "embedding_failed",
            "The document could not be embedded and nothing was stored; the server log says"
            " why. Add it again later.",
        ) from None


_SOURCE_KIND: Final = sql.SQL(
    "SELECT s.kind FROM source s JOIN workspace w ON w.id = s.workspace_id"
    " WHERE w.name = %s AND s.name = %s AND {access}"
)


def _stored_as(result: ingest.IngestResult) -> StoredAs:
    if result.inserted:
        return "inserted"
    if result.updated:
        return "updated"
    if result.revived:
        return "revived"
    if result.forgotten:
        return "forgotten"
    return "unchanged"


async def run_put_document(
    conn: Conn,
    services: QueryServices,
    access: WriteAccess,
    arguments: PutDocumentArguments,
    *,
    runs: AsyncConnectionPool[Any] | AsyncConnection[Any] | None = None,
) -> PutDocumentResult:
    """``put_document``: store one document record in a push source through
    :func:`memini.ingest.ingest`, with its redaction, filters, persons and indexing (with the
    service's embedder, when one is configured). ``runs`` is where the ingest run is recorded."""
    external_id = arguments.external_id or document_external_id(arguments.title, arguments.text)
    occurred_at = arguments.occurred_at or datetime.now(UTC)
    async with conn.transaction():
        cur = await conn.execute(
            _SOURCE_KIND.format(access=_scope(access, "s")),
            (arguments.workspace, arguments.source),
        )
        row = await cur.fetchone()
    if row is None:
        raise QueryError(404, "not_found", "No such source.")
    if row[0] != "push":
        raise QueryError(
            409,
            "not_ingestible",
            f"put_document writes to push sources; this is a {row[0]} source, which its own"
            " sync fills.",
        )
    record = ingest.RecordIn(
        kind="document",
        external_id=external_id,
        occurred_at=occurred_at,
        payload={"title": arguments.title, "text": arguments.text},
        external_url=arguments.url,
    )
    embedder = services.embedder
    indexer = None if embedder is None else ingest.PayloadIndexer(embedder)
    with query_errors(), _ingest_errors():
        result = await ingest.ingest(
            conn,
            access,
            arguments.workspace,
            arguments.source,
            ingest.Batch(records=[record]),
            limits=services.ingest_limits,
            runs=runs,
            indexer=indexer,
        )
    return PutDocumentResult(
        id=record_id(result.source_id, "document", external_id),
        workspace=arguments.workspace,
        source=arguments.source,
        external_id=external_id,
        locator=f"{arguments.source}:{external_id}",
        url=arguments.url,
        occurred_at=occurred_at,
        stored=_stored_as(result),
        run_id=result.run_id,
        indexed=indexer is not None,
        note=None if indexer is not None else NOT_INDEXED_NOTE,
    )
