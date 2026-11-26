"""The forget route: ``POST /v1/workspaces/{workspace}/records/{id}/forget``, the hard delete of
one record by hand, the HTTP face of :func:`memini.removal.forget_record` (requirements, section 6).

The path names the record by its id, as results give it. The record, its chunks, symbols, person
links and raw responses are deleted in the request, and an audit entry says who and when;
knowledge items that cite the record stay and keep their reference, and the record is not stored
again by a later sync of its source. Answers 200 with the audit entry and what was deleted. Needs
the ``admin`` scope: forget cannot be undone. There is no MCP tool for it (:mod:`memini.removal`
says why).

Errors: 404 ``not_found`` for a workspace or record the token may not change or that does not
exist; 409 ``source_busy`` while a job of the record's source runs (try again when it is done).
"""

from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from memini.api.admin_errors import admin_errors
from memini.api.auth import AdminAccess, AdminPrincipal
from memini.api.database import Connection
from memini.ids import RecordKind
from memini.removal import ForgetTarget, Forgotten, forget_record

__all__ = ["RECORD_ID", "ForgottenInfo", "RemovedInfo", "build_forget_router"]

RECORD_ID: Final = "^[0-9a-f]{64}$"
"""A record's id in a path: 64 lowercase hex characters."""


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
    router = APIRouter(prefix="/workspaces/{workspace}/records", tags=["records"])

    @router.post("/{id}/forget", summary="Forget a record")
    async def forget(
        workspace: str,
        id: Annotated[str, Path(pattern=RECORD_ID)],
        principal: AdminPrincipal,
        access: AdminAccess,
        conn: Connection,
    ) -> ForgottenInfo:
        """Delete one record, live or deleted at its source, with its chunks, symbols, person
        links and raw responses, and keep an audit entry (who, when, the record's id, source
        and kind). Knowledge items that cite it stay and keep their reference. A later sync of
        its source does not store it again. Cannot be undone. 404 when the record is not found,
        409 ``source_busy`` while a job of the source runs."""
        with admin_errors():
            forgotten = await forget_record(
                conn, access, workspace, ForgetTarget(record_id=id), token_id=principal.token_id
            )
        return ForgottenInfo.of(forgotten)

    return router
