"""The comms tools: "my context", the meetings and messages of a person, and a document write.

The library behind the MCP tools (:mod:`memini.mcp.comms`) and their HTTP routes
(:mod:`memini.api.comms`), independent of both (requirements, sections 1 and 6):

- ``list_meetings``: the meetings a person (default: me) took part in, by time and topic.
- ``get_meeting``: a meeting's notes: title, time, participants, summary, action items, and the
  transcript on request.
- ``list_messages``: messages sent to a person, mentioning them, or from them, one result per
  message, by source, time and topic.
- ``get_thread``: a whole thread, its messages in order.
- ``add_document``: a write (the ``ingest`` scope): one document record into a push source,
  through the ingest library, redaction and indexing included.

:mod:`memini.comms.model` holds the arguments and results, :mod:`memini.comms.service` one
function per tool. Persons are resolved through their merges (:mod:`memini.persons.lookup`);
listings read ``record_person``, the links ingest writes; a topic is matched by words through
search's lexical retriever, never by a language model. Refusals are
:class:`~memini.query.QueryError`, as for the query tools.
"""

from memini.comms.model import (
    MESSAGE_ROLES,
    ActionItemInfo,
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
    MessageRole,
    NewDocument,
    PersonInfo,
    RecordArguments,
    RecordPerson,
    SegmentInfo,
    StoredAs,
    ThreadRecord,
)
from memini.comms.service import (
    DOCUMENT_ID_DOMAIN,
    NOT_INDEXED_NOTE,
    document_external_id,
    run_add_document,
    run_get_meeting,
    run_get_thread,
    run_list_meetings,
    run_list_messages,
)

__all__ = [
    "DOCUMENT_ID_DOMAIN",
    "MESSAGE_ROLES",
    "NOT_INDEXED_NOTE",
    "ActionItemInfo",
    "AddDocumentArguments",
    "AddedDocument",
    "GetMeetingArguments",
    "GetThreadArguments",
    "ListMeetingsArguments",
    "ListMessagesArguments",
    "ListedMeeting",
    "ListedMessage",
    "MeetingList",
    "MeetingRecord",
    "MessageInfo",
    "MessageList",
    "MessageRole",
    "NewDocument",
    "PersonInfo",
    "RecordArguments",
    "RecordPerson",
    "SegmentInfo",
    "StoredAs",
    "ThreadRecord",
    "document_external_id",
    "run_add_document",
    "run_get_meeting",
    "run_get_thread",
    "run_list_meetings",
    "run_list_messages",
]
