"""Run routes: what the service did, with reports (``read`` scope).

- ``GET /v1/workspaces/{workspace}/runs``: the runs of a workspace, newest first, optionally of one
  source or one stream, a page at a time.
- ``GET /v1/runs/{id}``: one run with its report. A run id is unique across workspaces, so a
  client that holds one needs nothing else.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from memini import runs
from memini.api.admin_errors import admin_errors
from memini.api.auth import ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError

__all__ = ["RunDetail", "RunList", "RunSummary", "build_runs_router"]


class RunSummary(BaseModel):
    id: UUID
    kind: str = Field(description="What ran: git_sync, retention, reindex …")
    status: Literal["running", "succeeded", "partial", "failed", "cancelled"]
    source: str | None = Field(description="The name of the source the run worked on, if any.")
    stream: str | None = Field(description="The key of the stream the run worked on, if any.")
    started_at: datetime
    finished_at: datetime | None = Field(description="null while the run is running.")
    error: str | None = Field(description="Why the run failed or is partial.")

    @classmethod
    def of(cls, run: runs.RunInfo) -> "RunSummary":
        return cls(
            id=run.id,
            kind=run.kind,
            status=run.status,
            source=run.source,
            stream=run.stream,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error=run.error,
        )


class RunList(BaseModel):
    items: list[RunSummary] = Field(description="Newest first.")
    next_cursor: str | None = Field(
        description="Pass it as cursor for the next page; null on the last page."
    )


class RunDetail(RunSummary):
    workspace: str = Field(description="The name of the workspace.")
    report: dict[str, Any] = Field(
        description="The run report, version 1: version, duration_ms, steps (name, started_at,"
        " duration_ms, status, error), steps_omitted, counts, llm (calls, input_tokens,"
        " output_tokens, by_model). Check version first: a later version may change the shape."
    )


def build_runs_router() -> APIRouter:
    """The run routes; see the module."""
    router = APIRouter(tags=["runs"])

    @router.get("/workspaces/{workspace}/runs", summary="List runs")
    async def list_runs(
        workspace: str,
        access: ReadAccess,
        conn: Connection,
        sources: Annotated[
            list[str] | None, Query(description="Only runs of these sources.")
        ] = None,
        streams: Annotated[
            list[str] | None,
            Query(
                description="Only runs of the streams with these keys (refs/heads/main), in"
                " sources, which they need."
            ),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=runs.MAX_RUN_PAGE)] = runs.DEFAULT_RUN_PAGE,
        cursor: Annotated[
            str | None, Query(description="next_cursor of the previous page.")
        ] = None,
    ) -> RunList:
        """The runs of a workspace, of sources or of streams, newest first. 404 if the
        workspace, a source or a stream does not exist or the token may not read it."""
        if streams is not None and sources is None:
            raise ApiError(
                422,
                "invalid_filter",
                "A stream is named within its source: pass sources too.",
            )
        before = None
        if cursor is not None:
            try:
                before = runs.RunCursor.decode(cursor)
            except ValueError:
                raise ApiError(
                    422, "invalid_cursor", "cursor is not the next_cursor of a run listing."
                ) from None
        with admin_errors():
            page = await runs.list_runs(
                conn,
                access,
                workspace,
                sources=sources,
                streams=streams,
                limit=limit,
                before=before,
            )
        return RunList(
            items=[RunSummary.of(run) for run in page.runs],
            next_cursor=None if page.next is None else page.next.encode(),
        )

    @router.get("/runs/{id}", summary="Get a run")
    async def get_run(id: UUID, access: ReadAccess, conn: Connection) -> RunDetail:
        """One run with its report. 404 if it does not exist or the token may not read it."""
        with admin_errors():
            run = await runs.get_run(conn, access, id)
        summary = RunSummary.of(run)
        return RunDetail(
            **summary.model_dump(), workspace=run.workspace, report=dict(run.report or {})
        )

    return router
