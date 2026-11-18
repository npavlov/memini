"""Run routes: what the service did, with reports (``read`` scope).

- ``GET /v1/workspaces/{workspace}/runs``: the runs of a workspace, newest first, optionally of one
  source or one stream, a page at a time.
- ``GET /v1/runs/{run_id}``: one run with its report. A run id is unique across workspaces, so a
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
    runs: list[RunSummary] = Field(description="Newest first.")
    next_before: str | None = Field(
        description="Pass it as before= for the next page; null on the last page."
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
        source: Annotated[str | None, Query(description="Only runs of this source.")] = None,
        stream: Annotated[
            str | None,
            Query(
                description="Only runs of the stream with this key (refs/heads/main); needs source."
            ),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=runs.MAX_RUN_PAGE)] = runs.DEFAULT_RUN_PAGE,
        before: Annotated[
            str | None, Query(description="next_before of the previous page.")
        ] = None,
    ) -> RunList:
        """The runs of a workspace, a source or a stream, newest first. 404 if the workspace,
        source or stream does not exist or the token may not read it."""
        if stream is not None and source is None:
            raise ApiError(
                422, "invalid_filter", "A stream is named within its source: pass source too."
            )
        cursor = None
        if before is not None:
            try:
                cursor = runs.RunCursor.decode(before)
            except ValueError:
                raise ApiError(
                    422, "invalid_cursor", "before is not the next_before of a run listing."
                ) from None
        with admin_errors():
            page = await runs.list_runs(
                conn, access, workspace, source=source, stream=stream, limit=limit, before=cursor
            )
        return RunList(
            runs=[RunSummary.of(run) for run in page.runs],
            next_before=None if page.next is None else page.next.encode(),
        )

    @router.get("/runs/{run_id}", summary="Get a run")
    async def get_run(run_id: UUID, access: ReadAccess, conn: Connection) -> RunDetail:
        """One run with its report. 404 if it does not exist or the token may not read it."""
        with admin_errors():
            run = await runs.get_run(conn, access, run_id)
        summary = RunSummary.of(run)
        return RunDetail(
            **summary.model_dump(), workspace=run.workspace, report=dict(run.report or {})
        )

    return router
