"""Sensitive records by hand: ``PUT /v1/workspaces/{workspace}/records/sensitive`` marks one
record sensitive or clears its mark, ``GET`` lists the marked records of a workspace
(:mod:`memini.sensitivity`, requirements section 8).

The body names the record as forget does: by ``record_id``, or by ``source`` and ``external_id``
(with ``record_kind`` when the source has records of two kinds under that id). A marked record
stays stored and indexed and is left out of every answer unless a reader asks for sensitive
records with a token that has the ``sensitive`` scope. Both routes need the ``admin`` scope; the
listing names records and reasons, never their content.

Errors: 404 ``not_found`` for a workspace or record the token may not change or that does not
exist; 422 ``ambiguous_record`` for a source and external id that name records of two kinds.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from memini.api.admin_errors import admin_errors
from memini.api.auth import AdminAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.api.forget import ForgetRequest
from memini.ids import RecordKind
from memini.removal import AmbiguousRecordError
from memini.sensitivity import MAX_REASON_CHARS, Marked, list_sensitive, mark_record

__all__ = ["MarkRequest", "MarkedInfo", "SensitiveList", "build_sensitive_router"]


class MarkRequest(ForgetRequest):
    """The record to mark, as forget names one, and the mark."""

    model_config = ConfigDict(extra="forbid")

    sensitive: bool = Field(
        strict=True, description="true: mark the record sensitive; false: clear its mark."
    )
    reason: str | None = Field(
        default=None,
        max_length=1000,
        description="Why, for the admin who lists marked records: at most"
        f" {MAX_REASON_CHARS} characters are kept, redacted like any text an agent writes."
        " Dropped when the mark is cleared.",
    )


class MarkedInfo(BaseModel):
    """A record's mark after the change."""

    workspace: str
    record_id: str
    source: str
    record_kind: RecordKind
    external_id: str
    sensitive: bool
    reason: str | None
    sensitive_at: datetime | None = Field(description="When it was marked; null when not.")
    changed: bool = Field(description="false: the record had this mark and reason already.")

    @classmethod
    def of(cls, marked: Marked) -> "MarkedInfo":
        return cls(
            workspace=marked.workspace,
            record_id=marked.record_id,
            source=marked.source,
            record_kind=marked.record_kind,
            external_id=marked.external_id,
            sensitive=marked.sensitive,
            reason=marked.reason,
            sensitive_at=marked.sensitive_at,
            changed=marked.changed,
        )


class SensitiveRecordInfo(BaseModel):
    """A record marked sensitive: what names it and why, no content."""

    record_id: str
    source: str
    record_kind: RecordKind
    external_id: str
    occurred_at: datetime
    reason: str | None
    sensitive_at: datetime


class SensitiveList(BaseModel):
    """The records of a workspace marked sensitive, last marked first."""

    workspace: str
    records: list[SensitiveRecordInfo]


def build_sensitive_router() -> APIRouter:
    """The routes; see the module."""
    router = APIRouter(prefix="/workspaces/{workspace}/records", tags=["records"])

    @router.put("/sensitive", summary="Mark a record sensitive, or clear its mark")
    async def mark(
        workspace: str, body: MarkRequest, access: AdminAccess, conn: Connection
    ) -> MarkedInfo:
        """Mark one record sensitive, or clear its mark. Its content, chunks and vectors stay;
        reads leave it out unless they ask for sensitive records with the sensitive scope, and
        no model reads it. The same mark again changes nothing. 404 when the record is not
        found, 422 ``ambiguous_record`` when the source has records of two kinds under the
        external id (name record_kind)."""
        try:
            with admin_errors():
                marked = await mark_record(
                    conn,
                    access,
                    workspace,
                    body.to_target(),
                    sensitive=body.sensitive,
                    reason=body.reason,
                )
        except AmbiguousRecordError:
            raise ApiError(
                422,
                "ambiguous_record",
                "The source has records of more than one kind under that external id; name its"
                " record_kind.",
            ) from None
        return MarkedInfo.of(marked)

    @router.get("/sensitive", summary="List the records marked sensitive")
    async def listing(
        workspace: str,
        access: AdminAccess,
        conn: Connection,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> SensitiveList:
        """The records of the workspace marked sensitive, last marked first, with their
        reasons: names only, no content. For an admin, who may change the marks."""
        with admin_errors():
            _, records = await list_sensitive(conn, access, workspace, limit=limit)
        return SensitiveList(
            workspace=workspace,
            records=[
                SensitiveRecordInfo(
                    record_id=each.record_id,
                    source=each.source,
                    record_kind=each.record_kind,
                    external_id=each.external_id,
                    occurred_at=each.occurred_at,
                    reason=each.reason,
                    sensitive_at=each.sensitive_at,
                )
                for each in records
            ],
        )

    return router
