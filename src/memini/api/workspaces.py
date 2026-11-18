"""Workspace routes under ``/v1``: the reads a token with the ``read`` scope may make.

Also the pattern for read routes: take a :data:`~memini.api.auth.ReadAccess` before the
:data:`~memini.api.database.Connection`, and pass it to a read function, which puts its condition
into the query.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from memini import workspaces
from memini.api.auth import ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError

__all__ = ["WorkspaceInfo", "WorkspaceList", "build_workspaces_router"]


class WorkspaceInfo(BaseModel):
    id: UUID
    name: str = Field(description="The handle of the workspace, as used in URLs and the CLI.")
    created_at: datetime

    @classmethod
    def of(cls, workspace: workspaces.Workspace) -> "WorkspaceInfo":
        return cls(id=workspace.id, name=workspace.name, created_at=workspace.created_at)


class WorkspaceList(BaseModel):
    workspaces: list[WorkspaceInfo] = Field(description="By name.")


def build_workspaces_router() -> APIRouter:
    """``GET /workspaces`` and ``GET /workspaces/{name}``; both need the ``read`` scope."""
    router = APIRouter(prefix="/workspaces", tags=["workspaces"])

    @router.get("", summary="List workspaces")
    async def list_workspaces(access: ReadAccess, conn: Connection) -> WorkspaceList:
        """The workspaces the token may read: every one, or the one the token is limited to."""
        found = await workspaces.list_workspaces(conn, access)
        return WorkspaceList(workspaces=[WorkspaceInfo.of(workspace) for workspace in found])

    @router.get("/{name}", summary="Get a workspace")
    async def get_workspace(name: str, access: ReadAccess, conn: Connection) -> WorkspaceInfo:
        """One workspace by name. 404 if it does not exist or the token may not read it."""
        workspace = await workspaces.get_workspace(conn, access, name)
        if workspace is None:
            raise ApiError(404, "not_found", "No such workspace.")
        return WorkspaceInfo.of(workspace)

    return router
