"""Source routes under ``/v1/workspaces/{workspace}/sources``: the HTTP face of
:mod:`memini.sources`.

| Route | Scope | Does |
|---|---|---|
| ``GET …/sources`` | read | list the sources of a workspace |
| ``POST …/sources`` | admin | add a git or a push source (201) |
| ``GET …/sources/{source}`` | read | one source |
| ``PATCH …/sources/{source}`` | admin | change its settings |
| ``DELETE …/sources/{source}`` | admin | delete it, its streams, runs and mirror (204) |
| ``GET …/sources/{source}/streams`` | read | its streams with lag, last run and queued sync |
| ``POST …/sources/{source}/sync`` | admin | enqueue a sync of its streams (202) |

A token limited to one workspace sees and changes nothing in another: such a workspace answers
404, as one that does not exist. Errors are translated by :mod:`memini.api.admin_errors`.
"""

from datetime import datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memini import sources
from memini.api.admin_errors import admin_errors
from memini.api.auth import AdminAccess, ReadAccess
from memini.api.database import Connection
from memini.config import HANDLE_REGEX, Settings
from memini.git import GitOptions, MirrorManager
from memini.sources import (
    MAX_LOCATION_LENGTH,
    MAX_PATTERN_LENGTH,
    MAX_PATTERNS,
    MAX_REF_LENGTH,
    MAX_REFS,
    MAX_RETENTION_DAYS,
    MAX_SCHEDULE,
    MIN_SCHEDULE,
    LlmPolicy,
    SourceKind,
    Visibility,
)
from memini.status import STALE_AFTER_SCHEDULES

__all__ = [
    "NewPushSource",
    "NewSource",
    "SourceChanges",
    "SourceInfo",
    "SourceList",
    "StreamInfo",
    "StreamList",
    "SyncAccepted",
    "SyncRequest",
    "build_sources_router",
    "git_options",
]

_NAME = (
    "1 to 63 lowercase letters, digits, '-' or '_', starting with a letter or digit; unique in the"
    " workspace"
)
_LOCATION = (
    "The repository: an https:// or ssh:// URL, an scp-like address (git@host:path/repo.git), or an"
    " absolute local path. Plain http:// and local paths are refused unless the service allows"
    " them (MEMINI_GIT_ALLOW_HTTP, MEMINI_GIT_LOCAL_ROOTS); a URL with a user name or password is"
    " always refused: credentials come from the service's settings, never from a source. Checked"
    " when the source is added, not fetched."
)
_REFS = (
    "The refs to track, one stream each: a branch name (main), refs/heads/<branch> or"
    " refs/tags/<tag>. Stored as full names, sorted."
)
_SCHEDULE = (
    f"Sync every stream this often, {int(MIN_SCHEDULE.total_seconds())} seconds to"
    f" {int(MAX_SCHEDULE.total_seconds())}; null: only when triggered."
)
_PATTERNS = (
    "Path globs matched against whole repository-relative paths: * and ? within a path"
    " component, [abc] sets, ** for any number of components (**/*.md, docs/**). A file is"
    " indexed when it matches an include pattern (any file, when there is none) and no exclude"
    " pattern."
)
_BUILTIN_EXCLUDES = (
    "Leave out generated files that no exclude pattern names: lock files, minified bundles"
    " (*.min.js, *.min.css), source maps and node_modules. false indexes them too. A change lists"
    " every tracked ref in full at its next sync."
)
_VISIBILITY = "private: only the owner may read the source; workspace: everyone in the workspace."
_RETENTION = "Delete records older than this many days on every run; null: keep them."
_LLM_POLICY = "Which LLM may read the source's content: none, the local one, or a cloud one too."

type _Patterns = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=MAX_PATTERN_LENGTH)]],
    Field(max_length=MAX_PATTERNS),
]


class NewSource(BaseModel):
    """A git source to add."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["git"] = Field(description="The kind of source: git.")
    name: str = Field(pattern=HANDLE_REGEX, description=_NAME)
    location: str = Field(min_length=1, max_length=MAX_LOCATION_LENGTH, description=_LOCATION)
    refs: list[str] = Field(min_length=1, max_length=MAX_REFS, description=_REFS)
    schedule_seconds: int | None = Field(
        default=None,
        ge=int(MIN_SCHEDULE.total_seconds()),
        le=int(MAX_SCHEDULE.total_seconds()),
        description=_SCHEDULE,
    )
    include: _Patterns = Field(default_factory=list[str], description=_PATTERNS)
    exclude: _Patterns = Field(default_factory=list[str], description=_PATTERNS)
    builtin_excludes: bool = Field(default=True, strict=True, description=_BUILTIN_EXCLUDES)
    visibility: Visibility = Field(default="private", description=_VISIBILITY)
    retention_days: int | None = Field(
        default=None, ge=1, le=MAX_RETENTION_DAYS, description=_RETENTION
    )
    llm_policy: LlmPolicy = Field(default="local", description=_LLM_POLICY)

    def to_new(self) -> sources.NewGitSource:
        return sources.NewGitSource(
            name=self.name,
            location=self.location,
            refs=self.refs,
            schedule=_seconds(self.schedule_seconds),
            include=self.include,
            exclude=self.exclude,
            builtin_excludes=self.builtin_excludes,
            visibility=self.visibility,
            retention_days=self.retention_days,
            llm_policy=self.llm_policy,
        )


class NewPushSource(BaseModel):
    """A push source to add: clients send its records to ``POST …/sources/{source}/records``.
    It has no location, refs or schedule; a batch that names a stream creates it."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["push"] = Field(description="The kind of source: push.")
    name: str = Field(pattern=HANDLE_REGEX, description=_NAME)
    visibility: Visibility = Field(default="private", description=_VISIBILITY)
    retention_days: int | None = Field(
        default=None, ge=1, le=MAX_RETENTION_DAYS, description=_RETENTION
    )
    llm_policy: LlmPolicy = Field(default="local", description=_LLM_POLICY)

    def to_new(self) -> sources.NewPushSource:
        return sources.NewPushSource(
            name=self.name,
            visibility=self.visibility,
            retention_days=self.retention_days,
            llm_policy=self.llm_policy,
        )


class SourceChanges(BaseModel):
    """Settings to change. A setting left out stays as it is; ``refs``, ``include`` and
    ``exclude`` replace the whole list. The location cannot change: a different repository is a
    different source, so delete this one and add a new one."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, pattern=HANDLE_REGEX, description=_NAME)
    refs: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_REFS,
        description=_REFS + " Streams of refs left out are deleted with their runs.",
    )
    schedule_seconds: int | None = Field(
        default=None,
        ge=int(MIN_SCHEDULE.total_seconds()),
        le=int(MAX_SCHEDULE.total_seconds()),
        description=_SCHEDULE + " A new schedule starts now.",
    )
    include: _Patterns | None = Field(default=None, description=_PATTERNS)
    exclude: _Patterns | None = Field(default=None, description=_PATTERNS)
    builtin_excludes: bool | None = Field(default=None, strict=True, description=_BUILTIN_EXCLUDES)
    visibility: Visibility | None = Field(default=None, description=_VISIBILITY)
    retention_days: int | None = Field(
        default=None, ge=1, le=MAX_RETENTION_DAYS, description=_RETENTION
    )
    llm_policy: LlmPolicy | None = Field(default=None, description=_LLM_POLICY)

    @field_validator(
        "name", "refs", "include", "exclude", "builtin_excludes", "visibility", "llm_policy"
    )
    @classmethod
    def _not_null(cls, value: object) -> object:
        # Runs only for a value that was sent: null here is a client error, not "unchanged".
        if value is None:
            raise ValueError("leave the setting out to keep it; it cannot be null")
        return value

    def to_update(self) -> sources.SourceUpdate:
        given = self.model_fields_set
        unset = sources.UNSET
        return sources.SourceUpdate(
            name=self.name if "name" in given and self.name is not None else unset,
            refs=self.refs if "refs" in given and self.refs is not None else unset,
            schedule=_seconds(self.schedule_seconds) if "schedule_seconds" in given else unset,
            include=self.include if "include" in given and self.include is not None else unset,
            exclude=self.exclude if "exclude" in given and self.exclude is not None else unset,
            builtin_excludes=(
                self.builtin_excludes
                if "builtin_excludes" in given and self.builtin_excludes is not None
                else unset
            ),
            visibility=(
                self.visibility if "visibility" in given and self.visibility is not None else unset
            ),
            retention_days=self.retention_days if "retention_days" in given else unset,
            llm_policy=(
                self.llm_policy if "llm_policy" in given and self.llm_policy is not None else unset
            ),
        )


def _seconds(value: int | None) -> timedelta | None:
    return None if value is None else timedelta(seconds=value)


class SourceInfo(BaseModel):
    id: UUID
    workspace: str = Field(description="The name of the workspace.")
    name: str
    kind: SourceKind
    location: str | None = Field(
        description="git: the repository as the mirror fetches it, a canonical URL or the"
        " resolved local path; null for other kinds."
    )
    refs: list[str] = Field(
        description="The keys of the source's streams, sorted; for git, the tracked refs."
    )
    schedule_seconds: float | None = Field(
        description="Its streams sync this often; null: only when triggered."
    )
    include: list[str] = Field(description=_PATTERNS)
    exclude: list[str]
    builtin_excludes: bool | None = Field(
        description=_BUILTIN_EXCLUDES + " null for other kinds than git."
    )
    visibility: Visibility
    retention_days: int | None
    llm_policy: LlmPolicy
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, source: sources.Source) -> "SourceInfo":
        git = source.git
        return cls(
            id=source.id,
            workspace=source.workspace,
            name=source.name,
            kind=source.kind,
            location=None if git is None else git.location,
            refs=list(source.refs),
            schedule_seconds=None if source.schedule is None else source.schedule.total_seconds(),
            include=[] if git is None else list(git.include),
            exclude=[] if git is None else list(git.exclude),
            builtin_excludes=None if git is None else git.builtin_excludes,
            visibility=source.visibility,
            retention_days=source.retention_days,
            llm_policy=source.llm_policy,
            created_at=source.created_at,
            updated_at=source.updated_at,
        )


class SourceList(BaseModel):
    sources: list[SourceInfo] = Field(description="By name.")


class LastRun(BaseModel):
    """The newest run of a stream. ``GET /v1/runs/{id}`` has its report."""

    id: UUID
    kind: str
    status: Literal["running", "succeeded", "partial", "failed", "cancelled"]
    started_at: datetime
    finished_at: datetime | None = Field(description="null while the run is running.")
    error: str | None = Field(description="Why the run failed or is partial.")


class PendingSync(BaseModel):
    """The sync job of a stream that waits in the queue or runs."""

    id: UUID
    kind: str = Field(description="The job kind, e.g. git_sync.")
    status: Literal["queued", "running"]
    attempts: int = Field(description="Attempts so far, a running one included.")
    run_after: datetime = Field(description="Not started before this time.")
    last_error: str | None = Field(description="Why the last failed attempt failed.")


class StreamInfo(BaseModel):
    id: UUID
    key: str = Field(description="What the stream follows; for git, a full ref name.")
    schedule_seconds: float | None = Field(
        description="Syncs this often; null: only when triggered."
    )
    next_run_at: datetime | None = Field(
        description="When the scheduler next enqueues a sync; null with a schedule: at its next"
        " look, i.e. now."
    )
    checked_until: datetime | None = Field(
        description="Everything in the stream up to this time is stored; null before the first"
        " complete window."
    )
    lag_seconds: float | None = Field(
        description="Seconds from checked_until to now; null before the first complete window."
    )
    current: bool | None = Field(
        description=f"Whether the lag is at most {STALE_AFTER_SCHEDULES} times the schedule;"
        " false if it never completed a window; null without a schedule."
    )
    last_run: LastRun | None = Field(description="The newest run; null if it never ran.")
    pending_sync: PendingSync | None = Field(
        description="The queued or running sync; null if there is none."
    )
    missing: bool = Field(
        description="What the stream follows no longer exists at the source (a git ref deleted at"
        " the remote): its records are tombstones until it comes back."
    )

    @classmethod
    def of(cls, stream: sources.Stream) -> "StreamInfo":
        run, job = stream.last_run, stream.pending_job
        return cls(
            id=stream.id,
            key=stream.key,
            schedule_seconds=None if stream.schedule is None else stream.schedule.total_seconds(),
            next_run_at=stream.next_run_at,
            checked_until=stream.checked_until,
            lag_seconds=stream.lag_seconds,
            current=stream.current,
            last_run=None
            if run is None
            else LastRun(
                id=run.id,
                kind=run.kind,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error=run.error,
            ),
            pending_sync=None
            if job is None
            else PendingSync(
                id=job.id,
                kind=job.kind,
                status=job.status,
                attempts=job.attempts,
                run_after=job.run_after,
                last_error=job.last_error,
            ),
            missing=stream.missing,
        )


class StreamList(BaseModel):
    streams: list[StreamInfo] = Field(description="By key.")


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streams: list[Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_REFS,
        description="The keys of the streams to sync, as listed (refs/heads/main); null or left"
        " out: every stream of the source.",
    )


class SyncJob(BaseModel):
    stream: str = Field(description="The stream's key.")
    job_id: UUID
    created: bool = Field(
        description="false: a sync of the stream was already queued or running, and this is it."
    )


class SyncAccepted(BaseModel):
    jobs: list[SyncJob] = Field(description="One per stream, by key.")


def git_options(request: Request) -> GitOptions:
    """The git connector's options, from the application's settings."""
    settings = cast(Settings, request.app.state.settings)
    return GitOptions.from_settings(settings)


GitSettings = Annotated[GitOptions, Depends(git_options)]


def build_sources_router() -> APIRouter:
    """The source routes; see the module."""
    router = APIRouter(prefix="/workspaces/{workspace}/sources", tags=["sources"])

    @router.get("", summary="List sources")
    async def list_sources(workspace: str, access: ReadAccess, conn: Connection) -> SourceList:
        """The sources of a workspace, by name. 404 if the workspace does not exist or the token
        may not read it."""
        with admin_errors():
            found = await sources.list_sources(conn, access, workspace)
        return SourceList(sources=[SourceInfo.of(source) for source in found])

    @router.post("", status_code=201, summary="Add a source")
    async def create_source(
        workspace: str,
        body: Annotated[NewSource | NewPushSource, Field(discriminator="kind")],
        access: AdminAccess,
        options: GitSettings,
        conn: Connection,
    ) -> SourceInfo:
        """Add a git source with one stream per ref, or a push source. A git location is checked
        by the rules the mirror enforces (422 ``invalid_source`` names the setting and why, never
        the value), not fetched: the first sync creates the mirror. A push source takes batches
        of records at ``POST …/sources/{source}/records``. 409 if the workspace has a source of
        that name."""
        with admin_errors():
            if isinstance(body, NewPushSource):
                source = await sources.create_push_source(conn, access, workspace, body.to_new())
            else:
                source = await sources.create_git_source(
                    conn, access, workspace, body.to_new(), options=options
                )
        return SourceInfo.of(source)

    @router.get("/{source}", summary="Get a source")
    async def get_source(
        workspace: str, source: str, access: ReadAccess, conn: Connection
    ) -> SourceInfo:
        """One source. 404 if it, or its workspace, does not exist or the token may not read it."""
        with admin_errors():
            found = await sources.get_source(conn, access, workspace, source)
        return SourceInfo.of(found)

    @router.patch("/{source}", summary="Change a source")
    async def update_source(
        workspace: str, source: str, body: SourceChanges, access: AdminAccess, conn: Connection
    ) -> SourceInfo:
        """Change settings of a source; see the request body. A new schedule starts now. Refs
        left out lose their streams, with their runs and queued syncs."""
        with admin_errors():
            changed = await sources.update_source(conn, access, workspace, source, body.to_update())
        return SourceInfo.of(changed)

    @router.delete("/{source}", status_code=204, summary="Delete a source")
    async def delete_source(
        workspace: str,
        source: str,
        access: AdminAccess,
        options: GitSettings,
        conn: Connection,
    ) -> None:
        """Delete a source with its streams, runs, jobs and records, and remove its mirror. 409
        ``source_busy`` while a job of the source runs: try again when it is done."""
        with admin_errors():
            await sources.delete_source(
                conn, access, workspace, source, mirrors=MirrorManager(options)
            )

    @router.get("/{source}/streams", summary="List the streams of a source")
    async def list_streams(
        workspace: str, source: str, access: ReadAccess, conn: Connection
    ) -> StreamList:
        """The streams of a source with their lag, newest run and queued or running sync."""
        with admin_errors():
            found = await sources.list_streams(conn, access, workspace, source)
        return StreamList(streams=[StreamInfo.of(stream) for stream in found])

    @router.post("/{source}/sync", status_code=202, summary="Sync a source now")
    async def sync_source(
        workspace: str,
        source: str,
        access: AdminAccess,
        conn: Connection,
        body: SyncRequest | None = None,
    ) -> SyncAccepted:
        """Enqueue a sync of every stream of the source, or of the streams named. A stream whose
        sync is already queued or running gets no second one: that job is returned with
        ``created`` false. The jobs run when a worker with a handler for them is up; their runs
        appear under ``GET /v1/workspaces/{workspace}/runs``."""
        streams = None if body is None else body.streams
        with admin_errors():
            jobs = await sources.trigger_sync(conn, access, workspace, source, streams=streams)
        return SyncAccepted(
            jobs=[
                SyncJob(stream=job.stream, job_id=job.job_id, created=job.created) for job in jobs
            ]
        )

    return router
