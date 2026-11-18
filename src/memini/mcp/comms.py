"""The comms tools: ``list_meetings``, ``get_meeting``, ``list_messages``, ``get_thread`` and the
write ``add_document``.

Each read tool authorizes the read (:func:`~memini.auth.authorization.authorize_read`), borrows a
connection from the pool and calls its function in :mod:`memini.comms.service`, which the HTTP
routes call too. ``add_document`` is a write: it needs the ``ingest`` scope (requirements,
section 6), so a token without it neither sees nor calls it, and it goes through
:func:`~memini.auth.authorization.authorize_write`. A :class:`~memini.query.QueryError` becomes an
error result with its message, so the model can correct its call; the messages never repeat an
argument or stored content.

The structured result is the shape of :mod:`memini.comms.model`. The text content says the same
for clients that read only text: locators, kinds, times, links and evidence references (record id
and anchor) as plain lines, and everything the sources hold (titles, names, texts) in fenced
blocks, as it is: data, never instructions.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import BaseModel

from memini.auth import ReadFilter, authorization
from memini.comms import (
    AddDocumentArguments,
    AddedDocument,
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
    PersonInfo,
    ThreadRecord,
    run_add_document,
    run_get_meeting,
    run_get_thread,
    run_list_meetings,
    run_list_messages,
)
from memini.mcp.query import fenced
from memini.mcp.tools import Tool, ToolCall, ToolError
from memini.payloads import format_offset
from memini.query import QueryError, QueryServices

__all__ = [
    "ADD_DOCUMENT_TOOL",
    "COMMS_READ_TOOLS",
    "COMMS_WRITE_TOOLS",
    "GET_MEETING_TOOL",
    "GET_THREAD_TOOL",
    "LIST_MEETINGS_TOOL",
    "LIST_MESSAGES_TOOL",
    "summarize_added",
    "summarize_meeting",
    "summarize_meetings",
    "summarize_messages",
    "summarize_thread",
]

type Conn = AsyncConnection[TupleRow]
type Runner[A: BaseModel, R: BaseModel] = Callable[
    [Conn, QueryServices, ReadFilter, A], Awaitable[R]
]


def _read_run[A: BaseModel, R: BaseModel](
    run: Runner[A, R],
) -> Callable[[ToolCall, A], Awaitable[R]]:
    """A read tool's ``run``: authorize, borrow a connection, call ``run``, report a refusal."""

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


async def _get_meeting(
    conn: Conn, _services: QueryServices, access: ReadFilter, arguments: GetMeetingArguments
) -> MeetingRecord:
    return await run_get_meeting(conn, access, arguments)


async def _get_thread(
    conn: Conn, _services: QueryServices, access: ReadFilter, arguments: GetThreadArguments
) -> ThreadRecord:
    return await run_get_thread(conn, access, arguments)


async def _add_document(call: ToolCall, arguments: AddDocumentArguments) -> AddedDocument:
    # Called through its module, so a test can watch the tool go through the write access.
    access = authorization.authorize_write(call.principal, "ingest")
    pool = call.pool
    if pool is None:
        raise ToolError("The database is not available.")
    try:
        async with pool.connection() as conn:
            return await run_add_document(conn, call.services, access, arguments, runs=pool)
    except QueryError as error:
        raise ToolError(error.message) from None


# --- Text summaries ------------------------------------------------------------------------------


def _time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _count(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _whose(persons: Sequence[PersonInfo]) -> str:
    named = ", ".join(
        f"person {person.person_id} in {person.workspace}{' (me)' if person.is_me else ''}"
        for person in persons
    )
    return f"Of {named}." if named else ""


def _evidence(record_id: str, anchor: str | None) -> str | None:
    return None if anchor is None else f"evidence: record_id {record_id}, anchor {anchor}"


def _listed_meeting(number: int, meeting: ListedMeeting) -> str:
    details = [f"meeting in {meeting.workspace}/{meeting.source} ({meeting.source_kind})"]
    started = meeting.started_at or meeting.occurred_at
    details.append(f"started {_time(started)}")
    if meeting.ended_at is not None:
        details.append(f"ended {_time(meeting.ended_at)}")
    if meeting.score is not None:
        details.append(f"score {meeting.score:.4g}")
    if meeting.truncated:
        details.append("snippet cut short")
    lines = [f"{number}. {meeting.locator}", "; ".join(details), f"record_id {meeting.record_id}"]
    if meeting.link is not None:
        lines.append(f"link: {meeting.link}")
    evidence = _evidence(meeting.record_id, meeting.anchor)
    if evidence is not None:
        lines.append(evidence)
    shown = [meeting.title or "(no title)"]
    if meeting.participants:
        shown.append("Participants: " + ", ".join(meeting.participants))
    if meeting.snippet is not None:
        shown += ["", meeting.snippet]
    lines.append(fenced("\n".join(shown)))
    return "\n".join(lines)


def summarize_meetings(result: MeetingList) -> str:
    """The text content of a ``list_meetings`` result: every meeting with its title,
    participants and, for a query, the passage that matched."""
    parts = [f"{_count(len(result.meetings), 'meeting', 'meetings')}. {_whose(result.persons)}"]
    parts += [
        _listed_meeting(number, meeting) for number, meeting in enumerate(result.meetings, start=1)
    ]
    return "\n\n".join(parts)


def _message_text(message: MessageInfo, *, label: str = "") -> str:
    lines = [label] if label else []
    lines.append(f"From: {message.author}")
    if message.recipients:
        lines.append("To: " + ", ".join(message.recipients))
    if message.cc:
        lines.append("Cc: " + ", ".join(message.cc))
    if message.mentions:
        lines.append("Mentions: " + ", ".join(message.mentions))
    lines += ["", message.text]
    return "\n".join(lines)


def _listed_message(number: int, message: ListedMessage) -> str:
    details = [
        f"message in {message.workspace}/{message.source} ({message.source_kind})",
        f"sent {_time(message.sent_at)}",
        f"the person is {', '.join(message.roles)}",
    ]
    if message.score is not None:
        details.append(f"score {message.score:.4g}")
    if message.truncated:
        details.append("text cut short")
    lines = [f"{number}. {message.locator}", "; ".join(details)]
    if message.link is not None:
        lines.append(f"link: {message.link}")
    lines.append(f"evidence: record_id {message.record_id}, anchor {message.anchor}")
    label = " · ".join(part for part in (message.channel, message.title) if part)
    lines.append(fenced(_message_text(message, label=label)))
    return "\n".join(lines)


def summarize_messages(result: MessageList) -> str:
    """The text content of a ``list_messages`` result: every message with its thread, people and
    text."""
    parts = [f"{_count(len(result.messages), 'message', 'messages')}. {_whose(result.persons)}"]
    parts += [
        _listed_message(number, message) for number, message in enumerate(result.messages, start=1)
    ]
    return "\n\n".join(parts)


def _persons_line(record: MeetingRecord | ThreadRecord) -> str:
    return "Persons: " + "; ".join(
        f"{person.display_name} ({', '.join(person.roles)}{', me' if person.is_me else ''},"
        f" {person.person_id})"
        for person in record.persons
    )


def summarize_meeting(meeting: MeetingRecord) -> str:
    """The text content of a ``get_meeting`` result."""
    when = f"started {_time(meeting.started_at)}"
    if meeting.ended_at is not None:
        when += f", ended {_time(meeting.ended_at)}"
    lines = [
        f"Meeting {meeting.locator} in {meeting.workspace}/{meeting.source}"
        f" ({meeting.source_kind}); {when}.",
        f"record_id {meeting.record_id}. Anchors for evidence: summary, action-items, seg-<id>.",
    ]
    if meeting.link is not None:
        lines.append(f"link: {meeting.link}")
    if meeting.transcript is None and meeting.segments:
        lines.append(
            f"The transcript has {_count(meeting.segments, 'segment', 'segments')}: ask with"
            " include_transcript."
        )
    shown = [meeting.title]
    if meeting.participants:
        shown.append("Participants: " + ", ".join(meeting.participants))
    if meeting.persons:
        shown.append(_persons_line(meeting))
    if meeting.summary:
        shown += ["", "Summary", meeting.summary]
    if meeting.action_items:
        shown += ["", "Action items"]
        for item in meeting.action_items:
            owner = f"{item.owner}: " if item.owner else ""
            due = f" (due {item.due})" if item.due else ""
            shown.append(f"- {owner}{item.text}{due}")
    if meeting.transcript:
        shown += ["", "Transcript"]
        for segment in meeting.transcript:
            at = "" if segment.start is None else f"[{format_offset(segment.start)}] "
            speaker = f"{segment.speaker}: " if segment.speaker else ""
            shown.append(f"{at}{speaker}{segment.text} (seg-{segment.id})")
    lines.append(fenced("\n".join(shown)))
    return "\n".join(lines)


def summarize_thread(thread: ThreadRecord) -> str:
    """The text content of a ``get_thread`` result: the messages in order, each with its anchor."""
    lines = [
        f"Thread {thread.locator} in {thread.workspace}/{thread.source} ({thread.source_kind});"
        f" {_count(len(thread.messages), 'message', 'messages')}, the latest"
        f" {_time(thread.occurred_at)}.",
        f"record_id {thread.record_id}. Each message's anchor (msg-<id>) cites it as evidence.",
    ]
    if thread.link is not None:
        lines.append(f"link: {thread.link}")
    shown: list[str] = []
    label = " · ".join(part for part in (thread.channel, thread.title) if part)
    if label:
        shown.append(label)
    if thread.persons:
        shown.append(_persons_line(thread))
    for message in thread.messages:
        shown += ["", f"--- {message.anchor}, {_time(message.sent_at)}"]
        shown.append(_message_text(message))
    lines.append(fenced("\n".join(shown)))
    return "\n".join(lines)


def summarize_added(added: AddedDocument) -> str:
    """The text content of an ``add_document`` result."""
    indexed = "indexed" if added.indexed else f"not indexed: {added.note}"
    return (
        f"Document {added.locator} in {added.workspace}/{added.source}: {added.stored}, {indexed}"
        f"\nrecord_id {added.record_id} (cite it in record_decision as evidence);"
        f" occurred_at {_time(added.occurred_at)}."
    )


# --- The tools -----------------------------------------------------------------------------------

_PERSON_NOTE = (
    "person is 'me' (the default: the person marked as the caller), a person id, or an alias as "
    "the sources show it (a mail address, a chat user id, a display name); a person stands for "
    "everyone merged into it."
)
_QUERY_NOTE = (
    "query keeps those whose text holds a word of it (stemmed English and Russian words, names "
    "and identifiers as written), best match first; without it, newest first. No language model "
    "is involved: for a search by meaning use search with the persons filter."
)
_RESULT_NOTE = (
    "Each result carries its workspace, source and source kind, record_id, external_id, locator "
    "(source:external_id#anchor), link and time; record_id with anchor is an evidence reference "
    "for record_decision."
)

LIST_MEETINGS_TOOL = Tool(
    name="list_meetings",
    title="List meetings",
    description=(
        "Meetings a person took part in (listed as a participant, or a speaker of the "
        f"transcript): 'which meetings did I attend about X'. {_PERSON_NOTE} {_QUERY_NOTE} "
        "Filters: since and until on the meeting's start (ISO 8601 with a time zone), "
        f"workspaces, sources. {_RESULT_NOTE} With a query, a meeting also carries the passage "
        "that matched (anchor, snippet). Read a meeting's notes with get_meeting."
    ),
    arguments=ListMeetingsArguments,
    result=MeetingList,
    run=_read_run(run_list_meetings),
    summarize=summarize_meetings,
)

GET_MEETING_TOOL = Tool(
    name="get_meeting",
    title="Get a meeting",
    description=(
        "A meeting's notes by record_id, or by source and external_id (as results give them): "
        "title, start and end, participants as the notes name them and as canonical persons, "
        "summary, action items, and the transcript with include_transcript (segments with "
        "speaker, offsets and anchor seg-<id>)."
    ),
    arguments=GetMeetingArguments,
    result=MeetingRecord,
    run=_read_run(_get_meeting),
    summarize=summarize_meeting,
)

LIST_MESSAGES_TOOL = Tool(
    name="list_messages",
    title="List messages",
    description=(
        "Messages of chat and mail threads, one result per message: by default those sent to a "
        "person (to or cc) or that mention them, 'which messages were sent to me about X'; roles "
        "picks recipient, mention or author (['author']: messages from the person). "
        f"{_PERSON_NOTE} {_QUERY_NOTE} Filters: since and until on the time the message was sent "
        f"(ISO 8601 with a time zone), workspaces, sources. {_RESULT_NOTE} A message carries "
        "its thread's title and channel, author, recipients, mentions and text, and the roles "
        "the person has in it. Read the whole thread with get_thread."
    ),
    arguments=ListMessagesArguments,
    result=MessageList,
    run=_read_run(run_list_messages),
    summarize=summarize_messages,
)

GET_THREAD_TOOL = Tool(
    name="get_thread",
    title="Get a thread",
    description=(
        "A whole thread by record_id, or by source and external_id (as results give them): its "
        "title and channel, the canonical persons in it with their roles, and every message in "
        "order with its id, anchor msg-<id>, link, author, time, recipients, cc, mentions and "
        "text."
    ),
    arguments=GetThreadArguments,
    result=ThreadRecord,
    run=_read_run(_get_thread),
    summarize=summarize_thread,
)

ADD_DOCUMENT_TOOL = Tool(
    name="add_document",
    title="Add a document",
    description=(
        "Store a document (a title and a Markdown text) as a record of a push source, through "
        "the ingest contract: secrets and card numbers are redacted and it is indexed for search "
        "as any pushed document. Use it to keep what a conversation decided, then cite the "
        "returned record_id in record_decision. external_id names it in the source: the same id "
        "again replaces it; without one, an id is derived from the title and text, so the same "
        "document again changes nothing. occurred_at defaults to now. Returns the record and "
        "whether it was inserted, updated or unchanged."
    ),
    arguments=AddDocumentArguments,
    result=AddedDocument,
    run=_add_document,
    summarize=summarize_added,
    scope="ingest",
    read_only=False,
)

COMMS_READ_TOOLS: Sequence[Tool[Any, Any]] = (
    LIST_MEETINGS_TOOL,
    GET_MEETING_TOOL,
    LIST_MESSAGES_TOOL,
    GET_THREAD_TOOL,
)
"""The comms read tools, in the order ``tools/list`` shows them."""

COMMS_WRITE_TOOLS: Sequence[Tool[Any, Any]] = (ADD_DOCUMENT_TOOL,)
"""The comms write tools: ``add_document``, with the ``ingest`` scope."""
