"""Decision routes under ``/v1``: the manual operations on the knowledge journal, the HTTP face
of the MCP write tools, with the same arguments and results (:mod:`memini.decisions`).

| Route | MCP tool | Does |
|---|---|---|
| ``POST …/decisions`` | ``record_decision`` | record a decision or an open question by hand |
| ``POST …/decisions/{item_id}/supersede`` | ``supersede_decision`` | link it to earlier items |
| ``POST …/decisions/{item_id}/close`` | ``close_decision`` | close it without a replacement |
| ``POST …/decisions/{item_id}/retract`` | ``retract_decision`` | retract it: it was wrong |

``…`` is ``/workspaces/{workspace}``; an item named in the path must be in that workspace. Every
route needs the ``ingest`` scope (requirements, section 6) and writes through the caller's
:class:`~memini.auth.WriteAccess`: a token limited to one workspace changes nothing outside it,
and another workspace answers 404, as one that does not exist. Recording answers 201 with the new
item; the others answer 200 with the item. A body not of the shape answers 422
``validation_error``, with the fields.

Errors are those of :mod:`memini.decisions.service` in the API's error shape: 422
``invalid_item`` or ``ambiguous_record``; 404 ``not_found``; 409 ``not_active``,
``supersession_refused`` or ``journal_refused`` for a refusal of the append-only journal, whose
message names the database constraint.
"""

from uuid import UUID

from fastapi import APIRouter

from memini.api.auth import IngestAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.decisions import (
    DecisionError,
    DecisionItem,
    NewDecision,
    SupersedeBody,
    run_close,
    run_record_decision,
    run_retract,
    run_supersede,
)

__all__ = ["build_decisions_router"]


def _api_error(error: DecisionError) -> ApiError:
    return ApiError(error.status, error.code, error.message)


def build_decisions_router() -> APIRouter:
    """The decision routes; see the module."""
    router = APIRouter(prefix="/workspaces/{workspace}/decisions", tags=["decisions"])

    @router.post("", status_code=201, summary="Record a decision or an open question")
    async def record_decision(
        workspace: str, body: NewDecision, access: IngestAccess, conn: Connection
    ) -> DecisionItem:
        """Record a decision or an open question by hand, with its evidence (records named by
        record_id, or by source and external_id, with an anchor), its participants and the active
        items it replaces or answers. The MCP ``record_decision`` tool. The item is manual:
        extraction never supersedes or answers it."""
        try:
            return await run_record_decision(conn, access, workspace, body)
        except DecisionError as error:
            raise _api_error(error) from None

    @router.post("/{item_id}/supersede", summary="Link an item to earlier items it supersedes")
    async def supersede_decision(
        workspace: str, item_id: UUID, body: SupersedeBody, access: IngestAccess, conn: Connection
    ) -> DecisionItem:
        """Link the item to earlier items it replaces (they become superseded) or, for a
        decision, open questions it answers (they become closed). The MCP ``supersede_decision``
        tool. 409 ``not_active`` when a side is not active, ``supersession_refused`` for a link
        that breaks a rule (a cycle, the relation, the order, an extracted item over a manual
        one)."""
        try:
            return await run_supersede(conn, access, item_id, body.to_refs(), workspace=workspace)
        except DecisionError as error:
            raise _api_error(error) from None

    @router.post("/{item_id}/close", summary="Close an item without a replacement")
    async def close_decision(
        workspace: str, item_id: UUID, access: IngestAccess, conn: Connection
    ) -> DecisionItem:
        """Close an active item: an open question no longer asked, a decision no longer in
        force. The MCP ``close_decision`` tool. 409 ``not_active`` if it is not active."""
        try:
            return await run_close(conn, access, item_id, workspace=workspace)
        except DecisionError as error:
            raise _api_error(error) from None

    @router.post("/{item_id}/retract", summary="Retract a wrong item")
    async def retract_decision(
        workspace: str, item_id: UUID, access: IngestAccess, conn: Connection
    ) -> DecisionItem:
        """Retract an active item that was wrong: a misread extraction, a mistake. It stays in
        the history, marked retracted. The MCP ``retract_decision`` tool. 409 ``not_active`` if
        it is not active."""
        try:
            return await run_retract(conn, access, item_id, workspace=workspace)
        except DecisionError as error:
            raise _api_error(error) from None

    return router
