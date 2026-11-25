"""Decision routes under ``/v1``: the manual operations on the knowledge journal, the HTTP face
of the MCP write tools, with the same arguments and results (:mod:`memini.decisions`).

| Route | MCP tool | Does |
|---|---|---|
| ``POST …/decisions`` | ``record_decision`` | record a decision or an open question by hand |
| ``POST …/decisions/{item_id}/supersede`` | ``supersede_decision`` | link it to earlier items |
| ``POST …/decisions/{item_id}/close`` | ``close_decision`` | close it without a replacement |
| ``POST …/decisions/{item_id}/retract`` | ``retract_decision`` | retract it: it was wrong |
| ``POST …/decisions/{item_id}/domains`` | | link it by hand to a project or domains |
| ``DELETE …/decisions/{item_id}/domains/{domain}`` | | remove a link (``?project=``) |
| ``DELETE …/decisions/{item_id}/project/{project}`` | | remove the link to a project |
| ``GET``, ``PUT``, ``DELETE …/sources/{source}/catalog-default`` | | a source's default |
| ``POST …/assign-domains`` | | enqueue the ``assign_domains`` job of the workspace (admin) |

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

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from memini.api.auth import AdminAccess, IngestAccess, IngestPrincipal, ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.decisions import (
    DecisionError,
    DecisionItem,
    DomainLinkBody,
    NewDecision,
    SupersedeBody,
    run_close,
    run_link_domains,
    run_record_decision,
    run_retract,
    run_supersede,
    run_unlink_domain,
)
from memini.decisions.defaults import (
    SourceDefaultBody,
    SourceDefaults,
    run_delete_source_default,
    run_get_source_defaults,
    run_put_source_default,
)
from memini.decisions.reads import (
    DecisionChain,
    DecisionList,
    GetDecisionArguments,
    ListDecisionsArguments,
    run_get_decision,
    run_list_decisions,
)
from memini.jobs import enqueue_workspace_assignment
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = ["build_decisions_router"]


def _api_error(error: DecisionError) -> ApiError:
    return ApiError(error.status, error.code, error.message)


class AssignmentAccepted(BaseModel):
    """The answer of ``POST …/assign-domains``."""

    workspace: str
    job_id: UUID = Field(description="The assign_domains job of the workspace.")
    created: bool = Field(
        description="false: a job of the workspace was queued or running already, and is this one."
    )


def build_decisions_router() -> APIRouter:
    """The decision routes; see the module."""
    router = APIRouter(tags=["decisions"])

    @router.get("/decisions", summary="List decisions and open questions")
    async def list_decisions(
        arguments: Annotated[ListDecisionsArguments, Query()], access: ReadAccess, conn: Connection
    ) -> DecisionList:
        try:
            return await run_list_decisions(conn, access, arguments)
        except DecisionError as error:
            raise _api_error(error) from None

    @router.get("/decisions/{item_id}", summary="Get a decision and its supersession chain")
    async def get_decision(item_id: UUID, access: ReadAccess, conn: Connection) -> DecisionChain:
        try:
            return await run_get_decision(conn, access, GetDecisionArguments(item_id=item_id))
        except DecisionError as error:
            raise _api_error(error) from None

    @router.post(
        "/workspaces/{workspace}/decisions",
        status_code=201,
        summary="Record a decision or an open question",
    )
    async def record_decision(
        workspace: str,
        body: NewDecision,
        principal: IngestPrincipal,
        access: IngestAccess,
        conn: Connection,
    ) -> DecisionItem:
        """Record a decision or an open question by hand, with its evidence (records named by
        record_id, or by source and external_id, with an anchor), its participants, the active
        items it replaces or answers, and the project and domains of the catalog it belongs to.
        The MCP ``record_decision`` tool. The item is manual: extraction never supersedes or
        answers it, and the assign_domains model never changes its domains."""
        try:
            return await run_record_decision(
                conn, access, workspace, body, token_id=principal.token_id
            )
        except DecisionError as error:
            raise _api_error(error) from None

    @router.post(
        "/workspaces/{workspace}/decisions/{item_id}/supersede",
        summary="Link an item to earlier items it supersedes",
    )
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

    @router.post(
        "/workspaces/{workspace}/decisions/{item_id}/close",
        summary="Close an item without a replacement",
    )
    async def close_decision(
        workspace: str, item_id: UUID, access: IngestAccess, conn: Connection
    ) -> DecisionItem:
        """Close an active item: an open question no longer asked, a decision no longer in
        force. The MCP ``close_decision`` tool. 409 ``not_active`` if it is not active."""
        try:
            return await run_close(conn, access, item_id, workspace=workspace)
        except DecisionError as error:
            raise _api_error(error) from None

    @router.post(
        "/workspaces/{workspace}/decisions/{item_id}/retract", summary="Retract a wrong item"
    )
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

    @router.post(
        "/workspaces/{workspace}/decisions/{item_id}/domains",
        summary="Link an item to a project or domains",
    )
    async def link_decision_domains(
        workspace: str,
        item_id: UUID,
        body: DomainLinkBody,
        principal: IngestPrincipal,
        access: IngestAccess,
        conn: Connection,
    ) -> DecisionItem:
        """Link a stored item by hand (origin manual) to a project, or to domains of the
        workspace's catalog. The item does not change: links are outside the journal. A link by
        hand takes the place of the model's for the same domain, and the model never changes it.
        422 ``unknown_place`` for a name the catalog does not hold."""
        try:
            return await run_link_domains(
                conn, access, workspace, item_id, body, token_id=principal.token_id
            )
        except DecisionError as error:
            raise _api_error(error) from None

    @router.delete(
        "/workspaces/{workspace}/decisions/{item_id}/domains/{domain:path}",
        summary="Remove a link of an item to a domain",
    )
    async def unlink_decision_domain(
        workspace: str,
        item_id: UUID,
        domain: str,
        access: IngestAccess,
        conn: Connection,
        project: Annotated[str, Query(min_length=1, max_length=63)],
    ) -> DecisionItem:
        """Remove the item's link to a domain (``…/domains/fees/berth?project=port``), made by
        hand or by the model. 409 ``not_a_link`` for a domain the item has by its record (push,
        default) or its code."""
        try:
            return await run_unlink_domain(
                conn, access, workspace, item_id, project=project, domain=domain
            )
        except DecisionError as error:
            raise _api_error(error) from None

    @router.delete(
        "/workspaces/{workspace}/decisions/{item_id}/project/{project}",
        summary="Remove the link of an item to a project",
    )
    async def unlink_decision_project(
        workspace: str, item_id: UUID, project: str, access: IngestAccess, conn: Connection
    ) -> DecisionItem:
        """Remove the item's link to a project itself; its links to domains of the project
        stay."""
        try:
            return await run_unlink_domain(
                conn, access, workspace, item_id, project=project, domain=None
            )
        except DecisionError as error:
            raise _api_error(error) from None

    @router.post(
        "/workspaces/{workspace}/assign-domains",
        status_code=202,
        summary="Assign the domains of a workspace's items again",
    )
    async def assign_domains(
        workspace: str, access: AdminAccess, conn: Connection, response: Response
    ) -> AssignmentAccepted:
        """Enqueue the ``assign_domains`` job of every active item of the workspace, after its
        catalog changed. The job asks the model only about items it has not answered for the
        domains it would show them now, and never changes a domain that came by another way.
        It runs when the service's worker takes it, if an LLM provider is configured. Needs the
        ``admin`` scope: the items go to an LLM."""
        try:
            async with conn.transaction():
                found = await get_writable_workspace(conn, access, workspace)
                job = await enqueue_workspace_assignment(
                    conn, workspace_id=found.id, owner_id=access.owner_id
                )
        except NotFoundError:
            raise ApiError(404, "not_found", "No such workspace.") from None
        if not job.created:
            response.status_code = 200
        return AssignmentAccepted(workspace=found.name, job_id=job.id, created=job.created)

    default_path = "/workspaces/{workspace}/sources/{source}/catalog-default"

    @router.get(default_path, summary="The catalog defaults of a source")
    async def get_source_defaults(
        workspace: str, source: str, access: ReadAccess, conn: Connection
    ) -> SourceDefaults:
        """The project and domains the source, and each of its streams, gives the records
        pushed without their own."""
        try:
            return await run_get_source_defaults(conn, access, workspace, source)
        except DecisionError as error:
            raise _api_error(error) from None

    @router.put(default_path, summary="Set the catalog default of a source or stream")
    async def put_source_default(
        workspace: str,
        source: str,
        body: SourceDefaultBody,
        principal: IngestPrincipal,
        access: IngestAccess,
        conn: Connection,
    ) -> SourceDefaults:
        """Set the project and domains of the source's records (or of one stream's, with
        ``stream``) pushed without their own. A push or mcp source only."""
        try:
            return await run_put_source_default(
                conn, access, workspace, source, body, token_id=principal.token_id
            )
        except DecisionError as error:
            raise _api_error(error) from None

    @router.delete(default_path, summary="Remove the catalog default of a source or stream")
    async def delete_source_default(
        workspace: str,
        source: str,
        access: IngestAccess,
        conn: Connection,
        stream: Annotated[str | None, Query(min_length=1)] = None,
    ) -> SourceDefaults:
        """Remove the default of the source, or of one stream with ``?stream=``."""
        try:
            return await run_delete_source_default(conn, access, workspace, source, stream=stream)
        except DecisionError as error:
            raise _api_error(error) from None

    return router
