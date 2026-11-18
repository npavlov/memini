"""Workspace routes under ``/v1``: list and read them (``read`` scope), create one (``admin``).

Also the pattern for read routes: take a :data:`~memini.api.auth.ReadAccess` before the
:data:`~memini.api.database.Connection`, and pass it to a read function, which puts its condition
into the query. Write routes take an :data:`~memini.api.auth.AdminAccess` the same way.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from memini import workspaces
from memini.api.admin_errors import admin_errors
from memini.api.auth import AdminAccess, ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.config import HANDLE_REGEX

__all__ = ["NewWorkspace", "WorkspaceInfo", "WorkspaceList", "build_workspaces_router"]

HANDLE_DESCRIPTION = (
    "1 to 63 lowercase letters, digits, '-' or '_', starting with a letter or digit; used in URLs"
    " and the CLI"
)


class WorkspaceInfo(BaseModel):
    id: UUID
    name: str = Field(description="The handle of the workspace, as used in URLs and the CLI.")
    created_at: datetime

    @classmethod
    def of(cls, workspace: workspaces.Workspace) -> "WorkspaceInfo":
        return cls(id=workspace.id, name=workspace.name, created_at=workspace.created_at)


class WorkspaceList(BaseModel):
    workspaces: list[WorkspaceInfo] = Field(description="By name.")


class NewWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=HANDLE_REGEX, description=HANDLE_DESCRIPTION)


def build_workspaces_router() -> APIRouter:
    """``GET /workspaces`` and ``GET /workspaces/{workspace}`` (``read``); ``POST /workspaces``
    (``admin``)."""
    router = APIRouter(prefix="/workspaces", tags=["workspaces"])

    @router.get("", summary="List workspaces")
    async def list_workspaces(access: ReadAccess, conn: Connection) -> WorkspaceList:
        """The workspaces the token may read: every one, or the one the token is limited to."""
        found = await workspaces.list_workspaces(conn, access)
        return WorkspaceList(workspaces=[WorkspaceInfo.of(workspace) for workspace in found])

    @router.post("", status_code=201, summary="Create a workspace")
    async def create_workspace(
        body: NewWorkspace, access: AdminAccess, conn: Connection
    ) -> WorkspaceInfo:
        """Create a workspace owned by the token's principal. Needs the ``admin`` scope and a
        token that is not limited to one workspace (403 ``workspace_limited``); 409 if the name is
        taken."""
        with admin_errors():
            workspace = await workspaces.create_workspace_as(conn, access, body.name)
        return WorkspaceInfo.of(workspace)

    @router.get("/{workspace}", summary="Get a workspace")
    async def get_workspace(workspace: str, access: ReadAccess, conn: Connection) -> WorkspaceInfo:
        """One workspace by name. 404 if it does not exist or the token may not read it."""
        found = await workspaces.get_workspace(conn, access, workspace)
        if found is None:
            raise ApiError(404, "not_found", "No such workspace.")
        return WorkspaceInfo.of(found)

    return router
