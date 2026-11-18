"""Comms routes under ``/v1``: the HTTP face of the MCP comms tools, with the same arguments and
results (:mod:`memini.comms`).

| Route | MCP tool | Does |
|---|---|---|
| ``GET /meetings`` | ``list_meetings`` | meetings of a person (default: me), by time and topic |
| ``GET /meetings/{record_id}`` | ``get_meeting`` | a meeting's notes |
| ``GET …/meetings/{external_id}`` | ``get_meeting`` | the same, by source and external id |
| ``GET /messages`` | ``list_messages`` | messages to, mentioning or from a person |
| ``GET /threads/{record_id}`` | ``get_thread`` | a whole thread |
| ``GET …/threads/{external_id}`` | ``get_thread`` | the same, by source and external id |
| ``POST …/documents`` | ``add_document`` | store a document in a push source |

``…`` is ``/workspaces/{workspace}/sources/{source}``; an external id may hold ``/``. The reads
need the ``read`` scope and read through the caller's :class:`~memini.auth.ReadFilter`; the
write needs the ``ingest`` scope and keeps a token limited to one workspace inside it. Arguments
of a read are query parameters with the names of the tool's arguments; a list is the parameter
repeated (``?roles=recipient&roles=mention``); a time takes a zone. Unknown parameters are refused
(422 ``validation_error``). The body of ``POST …/documents`` is the document: ``title``,
``text`` and optionally ``external_id``, ``occurred_at`` and ``url``.

Errors are those of :mod:`memini.comms.service` in the API's error shape: 404 ``no_me`` when no
person is marked as me, 404 ``not_found``, 422 ``ambiguous_record`` or ``wrong_kind``, 409
``unreadable_record``; with a query those of search (503 ``index_mismatch``,
``embeddings_not_configured``); for the write those of the push API.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Path, Query, Request

from memini.api.auth import IngestAccess, ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.api.query import Services
from memini.comms import (
    AddDocumentArguments,
    AddedDocument,
    GetMeetingArguments,
    GetThreadArguments,
    ListMeetingsArguments,
    ListMessagesArguments,
    MeetingList,
    MeetingRecord,
    MessageList,
    NewDocument,
    ThreadRecord,
    run_add_document,
    run_get_meeting,
    run_get_thread,
    run_list_meetings,
    run_list_messages,
)
from memini.db import Pool
from memini.query import QueryError

__all__ = ["build_comms_router"]

_RECORD_ID = "^[0-9a-f]{64}$"
_RECORD_ID_HELP = "The record's id, as results give it."
_TRANSCRIPT_HELP = "Return the transcript, segment by segment."


def _api_error(error: QueryError) -> ApiError:
    return ApiError(error.status, error.code, error.message)


def build_comms_router() -> APIRouter:
    """The comms routes; see the module."""
    router = APIRouter(tags=["comms"])

    @router.get("/meetings", summary="List meetings of a person")
    async def list_meetings(
        arguments: Annotated[ListMeetingsArguments, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> MeetingList:
        """Meetings a person (default: me) took part in, the MCP ``list_meetings`` tool: newest
        first, or with ``query`` those whose notes hold a word of it, best match first."""
        try:
            return await run_list_meetings(conn, services, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/meetings/{record_id}", summary="Get a meeting")
    async def get_meeting(
        record_id: Annotated[str, Path(pattern=_RECORD_ID, description=_RECORD_ID_HELP)],
        access: ReadAccess,
        conn: Connection,
        include_transcript: Annotated[bool, Query(description=_TRANSCRIPT_HELP)] = False,
    ) -> MeetingRecord:
        """A meeting's notes by record id, the MCP ``get_meeting`` tool."""
        arguments = GetMeetingArguments(record_id=record_id, include_transcript=include_transcript)
        try:
            return await run_get_meeting(conn, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get(
        "/workspaces/{workspace}/sources/{source}/meetings/{external_id:path}",
        summary="Get a meeting by its external id",
    )
    async def get_meeting_by_reference(
        workspace: str,
        source: str,
        external_id: str,
        access: ReadAccess,
        conn: Connection,
        include_transcript: Annotated[bool, Query(description=_TRANSCRIPT_HELP)] = False,
    ) -> MeetingRecord:
        """A meeting's notes by its source and external id, the MCP ``get_meeting`` tool."""
        # Names of any length are looked up and not found, as on the other routes.
        arguments = GetMeetingArguments.model_construct(
            record_id=None,
            workspace=workspace,
            source=source,
            external_id=external_id,
            include_transcript=include_transcript,
        )
        try:
            return await run_get_meeting(conn, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/messages", summary="List messages to, mentioning or from a person")
    async def list_messages(
        arguments: Annotated[ListMessagesArguments, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> MessageList:
        """Messages sent to a person (default: me) or that mention them, or with
        ``roles=author`` from them, one result per message: the MCP ``list_messages`` tool."""
        try:
            return await run_list_messages(conn, services, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/threads/{record_id}", summary="Get a thread")
    async def get_thread(
        record_id: Annotated[str, Path(pattern=_RECORD_ID, description=_RECORD_ID_HELP)],
        access: ReadAccess,
        conn: Connection,
    ) -> ThreadRecord:
        """A whole thread by record id, the MCP ``get_thread`` tool."""
        arguments = GetThreadArguments(record_id=record_id)
        try:
            return await run_get_thread(conn, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get(
        "/workspaces/{workspace}/sources/{source}/threads/{external_id:path}",
        summary="Get a thread by its external id",
    )
    async def get_thread_by_reference(
        workspace: str, source: str, external_id: str, access: ReadAccess, conn: Connection
    ) -> ThreadRecord:
        """A whole thread by its source and external id, the MCP ``get_thread`` tool."""
        arguments = GetThreadArguments.model_construct(
            record_id=None, workspace=workspace, source=source, external_id=external_id
        )
        try:
            return await run_get_thread(conn, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.post("/workspaces/{workspace}/sources/{source}/documents", summary="Add a document")
    async def add_document(
        workspace: str,
        source: str,
        body: NewDocument,
        access: IngestAccess,
        services: Services,
        request: Request,
        conn: Connection,
    ) -> AddedDocument:
        """Store a document in a push source through the ingest contract (redaction and
        indexing included), the MCP ``add_document`` tool. The answer says whether it was
        inserted, updated or unchanged, and gives its record_id to cite as evidence."""
        arguments = AddDocumentArguments.model_construct(
            workspace=workspace, source=source, **body.model_dump()
        )
        pool = cast(Pool | None, request.app.state.pool)
        try:
            return await run_add_document(conn, services, access, arguments, runs=pool)
        except QueryError as error:
            raise _api_error(error) from None

    return router
