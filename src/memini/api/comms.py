"""Comms routes under ``/v1``: the HTTP face of the MCP comms tools, with the same arguments and
results (:mod:`memini.comms`).

| Route | MCP tool | Does |
|---|---|---|
| ``GET /meetings`` | ``list_meetings`` | meetings of persons (default: me), by time and topic |
| ``GET /meetings/{id}`` | ``get_meeting`` | a meeting's notes |
| ``GET /messages`` | ``list_messages`` | messages to, mentioning or from persons |
| ``GET /threads/{id}`` | ``get_thread`` | a whole thread |
| ``PUT …/documents`` | ``put_document`` | store a document in a push source |

``…`` is ``/workspaces/{workspace}/sources/{source}``. The reads need the ``read`` scope and read
through the caller's :class:`~memini.auth.ReadFilter`; the write needs the ``ingest`` scope and
keeps a token limited to one workspace inside it. Arguments of a read are query parameters with
the names of the tool's arguments; a list is the parameter repeated
(``?roles=recipient&roles=mention``); a time takes a zone. Unknown parameters are refused (422
``validation_error``). The body of ``PUT …/documents`` is the document: ``title``, ``text`` and
optionally ``external_id``, ``occurred_at`` and ``url``; sent again, it changes nothing.

Errors are those of :mod:`memini.comms.service` in the API's error shape: 404 ``no_me`` when no
person is marked as me, 404 ``not_found``, 403 ``sensitive_record``, 422 ``wrong_kind``, 409
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
    GetMeetingArguments,
    GetThreadArguments,
    ListMeetingsArguments,
    ListMessagesArguments,
    MeetingList,
    MeetingRecord,
    MessageList,
    NewDocument,
    PutDocumentArguments,
    PutDocumentResult,
    ThreadRecord,
    run_get_meeting,
    run_get_thread,
    run_list_meetings,
    run_list_messages,
    run_put_document,
)
from memini.db import Pool
from memini.query import QueryError

__all__ = ["build_comms_router"]

_RECORD_ID = "^[0-9a-f]{64}$"
_ID_HELP = "The record's id, as results give it."
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
        """Meetings persons (default: me) took part in, the MCP ``list_meetings`` tool: newest
        first, or with ``query`` those whose notes hold a word of it, best match first."""
        try:
            return await run_list_meetings(conn, services, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/meetings/{id}", summary="Get a meeting")
    async def get_meeting(
        id: Annotated[str, Path(pattern=_RECORD_ID, description=_ID_HELP)],
        access: ReadAccess,
        conn: Connection,
        include_transcript: Annotated[bool, Query(description=_TRANSCRIPT_HELP)] = False,
    ) -> MeetingRecord:
        """A meeting's notes by its id, the MCP ``get_meeting`` tool."""
        arguments = GetMeetingArguments(id=id, include_transcript=include_transcript)
        try:
            return await run_get_meeting(conn, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/messages", summary="List messages to, mentioning or from persons")
    async def list_messages(
        arguments: Annotated[ListMessagesArguments, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> MessageList:
        """Messages sent to persons (default: me) or that mention them, or with
        ``roles=author`` from them, one result per message: the MCP ``list_messages`` tool."""
        try:
            return await run_list_messages(conn, services, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/threads/{id}", summary="Get a thread")
    async def get_thread(
        id: Annotated[str, Path(pattern=_RECORD_ID, description=_ID_HELP)],
        access: ReadAccess,
        conn: Connection,
    ) -> ThreadRecord:
        """A whole thread by its id, the MCP ``get_thread`` tool."""
        arguments = GetThreadArguments(id=id)
        try:
            return await run_get_thread(conn, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.put("/workspaces/{workspace}/sources/{source}/documents", summary="Put a document")
    async def put_document(
        workspace: str,
        source: str,
        body: NewDocument,
        access: IngestAccess,
        services: Services,
        request: Request,
        conn: Connection,
    ) -> PutDocumentResult:
        """Store a document in a push source through the ingest contract (redaction and
        indexing included), the MCP ``put_document`` tool. The answer says whether it was
        inserted, updated or unchanged, and gives its id to cite as evidence."""
        arguments = PutDocumentArguments.model_construct(
            workspace=workspace, source=source, **body.model_dump()
        )
        pool = cast(Pool | None, request.app.state.pool)
        try:
            return await run_put_document(conn, services, access, arguments, runs=pool)
        except QueryError as error:
            raise _api_error(error) from None

    return router
