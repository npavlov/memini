"""The forget route: ``POST /v1/workspaces/{workspace}/forget``, the hard delete of one record by
hand, the HTTP face of :func:`memini.removal.forget_record` (requirements, section 6).

The body names the record by ``record_id``, or by ``source`` and ``external_id`` (with
``record_kind`` when the source has records of two kinds under that id). The record, its chunks,
symbols, person links and raw responses are deleted in the request, and an audit entry says who
and when; knowledge items that cite the record stay and keep their reference, and the record is
not stored again by a later sync of its source. Answers 200 with the audit entry and what was
deleted. Needs the ``admin`` scope: forget cannot be undone. There is no MCP tool for it
(:mod:`memini.removal` says why).

Errors: 404 ``not_found`` for a workspace or record the token may not change or that does not
exist; 422 ``ambiguous_record`` for a source and external id that name records of two kinds; 409
``source_busy`` while a job of the record's source runs (try again when it is done).
"""

from datetime import datetime
from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from memini.api.admin_errors import admin_errors
from memini.api.auth import AdminAccess, AdminPrincipal
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.config import HANDLE_REGEX
from memini.ids import RecordKind
from memini.ingest import MAX_EXTERNAL_ID_BYTES
from memini.removal import AmbiguousRecordError, ForgetTarget, Forgotten, forget_record

__all__ = ["ForgetRequest", "ForgottenInfo", "RemovedInfo", "build_forget_router"]

_RECORD_ID: Final = "^[0-9a-f]{64}$"


class ForgetRequest(BaseModel):
    """The record to forget: by ``record_id``, or by ``source`` and ``external_id``."""

    model_config = ConfigDict(extra="forbid")

    record_id: str | None = Field(
        default=None,
        pattern=_RECORD_ID,
        description="The record's id, 64 lowercase hex characters, as search results give it.",
    )
    source: Annotated[str, Field(pattern=HANDLE_REGEX)] | None = Field(
        default=None, description="The record's source by name. With external_id."
    )
    external_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_EXTERNAL_ID_BYTES,
        description="The record's id in its source, as search results give it: a thread key, a"
        " meeting id; for a file of a repository '<full ref>:<path>'.",
    )
    record_kind: RecordKind | None = Field(
        default=None,
        description="code_file, thread, meeting or document; needed only when the source has"
        " records of two kinds under that external id.",
    )

    @model_validator(mode="after")
    def _one_way_to_name_the_record(self) -> Self:
        by_reference = (
            self.source is not None or self.external_id is not None or self.record_kind is not None
        )
        if self.record_id is not None and by_reference:
            raise ValueError("name a record by record_id or by source and external_id, not both")
        if self.record_id is None and (self.source is None or self.external_id is None):
            raise ValueError("name a record by record_id, or by source and external_id")
        return self

    def to_target(self) -> ForgetTarget:
        if self.record_id is not None:
            return ForgetTarget(record_id=self.record_id)
        return ForgetTarget(source=self.source, external_id=self.external_id, kind=self.record_kind)


class RemovedInfo(BaseModel):
    """What was deleted with the record."""

    chunks: int
    symbols: int
    raw_responses: int = Field(description="Stored raw responses the record was parsed from.")


class ForgottenInfo(BaseModel):
    """The audit entry of a forgotten record, and what was deleted with it."""

    id: UUID = Field(description="The audit entry.")
    workspace: str
    record_id: str
    source_id: UUID
    source: str
    record_kind: RecordKind
    forgotten_at: datetime
    removed: RemovedInfo
    citing_items: int = Field(
        description="Knowledge items whose evidence cites the record: they stay, with the"
        " reference, and their evidence reads as forgotten."
    )

    @classmethod
    def of(cls, forgotten: Forgotten) -> "ForgottenInfo":
        return cls(
            id=forgotten.id,
            workspace=forgotten.workspace,
            record_id=forgotten.record_id,
            source_id=forgotten.source_id,
            source=forgotten.source,
            record_kind=forgotten.record_kind,
            forgotten_at=forgotten.forgotten_at,
            removed=RemovedInfo(
                chunks=forgotten.removed.chunks,
                symbols=forgotten.removed.symbols,
                raw_responses=forgotten.removed.raw_responses,
            ),
            citing_items=forgotten.citing_items,
        )


def build_forget_router() -> APIRouter:
    """The forget route; see the module."""
    router = APIRouter(prefix="/workspaces/{workspace}", tags=["records"])

    @router.post("/forget", summary="Forget a record")
    async def forget(
        workspace: str,
        body: ForgetRequest,
        principal: AdminPrincipal,
        access: AdminAccess,
        conn: Connection,
    ) -> ForgottenInfo:
        """Delete one record, live or deleted at its source, with its chunks, symbols, person
        links and raw responses, and keep an audit entry (who, when, the record's id, source
        and kind). Knowledge items that cite it stay and keep their reference. A later sync of
        its source does not store it again. Cannot be undone. 404 when the record is not found,
        422 ``ambiguous_record`` when the source has records of two kinds under the external id
        (name record_kind), 409 ``source_busy`` while a job of the source runs."""
        try:
            with admin_errors():
                forgotten = await forget_record(
                    conn, access, workspace, body.to_target(), token_id=principal.token_id
                )
        except AmbiguousRecordError:
            raise ApiError(
                422,
                "ambiguous_record",
                "The source has records of more than one kind under that external id; name its"
                " record_kind.",
            ) from None
        return ForgottenInfo.of(forgotten)

    return router
