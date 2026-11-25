"""Source routes under ``/v1/workspaces/{workspace}/sources``: the HTTP face of
:mod:`memini.sources`.

| Route | Scope | Does |
|---|---|---|
| ``GET …/sources`` | read | list the sources of a workspace |
| ``POST …/sources`` | admin | add a git, a push or an mcp source (201) |
| ``GET …/sources/{source}`` | read | one source |
| ``PATCH …/sources/{source}`` | admin | change its settings |
| ``DELETE …/sources/{source}`` | admin | delete it, its streams, runs and mirror (204) |
| ``GET …/sources/{source}/streams`` | read | its streams with lag, last run and queued sync |
| ``POST …/sources/{source}/sync`` | admin | enqueue a sync of its streams (202) |
| ``POST …/sources/{source}/prune`` | admin | apply its filters to its stored records |
| ``GET …/sources/{source}/preview`` | read | count what a repository would index |

A token limited to one workspace sees and changes nothing in another: such a workspace answers
404, as one that does not exist. Errors are translated by :mod:`memini.api.admin_errors`.
"""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memini import prune, sources
from memini.api.admin_errors import admin_errors
from memini.api.auth import AdminAccess, ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.config import HANDLE_REGEX, Settings
from memini.db import Pool
from memini.embeddings import EmbeddingError
from memini.executors.egress import MAX_URL_LENGTH, EgressPolicy
from memini.executors.source import (
    MAX_ARG_LENGTH,
    MAX_ARGS,
    MAX_RECIPE_TEXT,
    MAX_STREAM_KEY_LENGTH,
    MAX_STREAMS,
    Endpoint,
    HttpEndpoint,
    McpConfig,
    StdioEndpoint,
)
from memini.filters import MAX_FILTER_PATTERN_LENGTH, MAX_FILTER_PATTERNS, SourceFilters
from memini.git import GitError, GitOptions, MirrorManager, RevisionNotFound
from memini.gitsync.preview import Preview, preview_ref
from memini.gitsync.rules import FileRules
from memini.index import IndexMismatchError
from memini.ingest import PayloadIndexer
from memini.query import QueryServices
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
    "EndpointBody",
    "FiltersBody",
    "McpInfo",
    "NewMcpSource",
    "NewPushSource",
    "NewSource",
    "PruneAccepted",
    "PruneCounts",
    "RecipeBody",
    "SourceChanges",
    "SourceInfo",
    "SourceList",
    "StreamInfo",
    "StreamList",
    "SyncAccepted",
    "SyncRequest",
    "build_sources_router",
    "git_options",
    "mcp_policy",
]

log = logging.getLogger(__name__)

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
_RETENTION = (
    "Delete records older than this many days, with their chunks, on a retention pass every"
    " retention_interval and at the end of every sync; for git, only files gone from the tree"
    " that long ago. null: keep them."
)
_LLM_POLICY = "Which LLM may read the source's content: none, the local one, or a cloud one too."
_STREAMS = (
    "mcp: the stream keys to sync, one stream each, as the recipe's stream.key_description says"
    " (a channel id, a mailbox query); each must match its stream.key_pattern. Sorted."
)
_WINDOW = (
    "mcp: overrides of the recipe's [window]: overlap, initial, backfill, max_span and history"
    " (how far back history loads at most), each a duration such as 7d or 12h; null turns"
    " backfill, max_span or history off. Left out: the recipe's value."
)

type _Patterns = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=MAX_PATTERN_LENGTH)]],
    Field(max_length=MAX_PATTERNS),
]

type _Globs = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=MAX_FILTER_PATTERN_LENGTH)]],
    Field(max_length=MAX_FILTER_PATTERNS),
]

_GLOBS = (
    "Globs matched against the whole value, ignoring case: * any characters, ? one, [abc] or"
    " [a-z] one of a set, [!abc] one not of it; [*] is a *."
)


class FiltersBody(BaseModel):
    """Which records of a push or mcp source are stored, and which messages of a thread; every
    rule is optional. A message left out is taken out of its thread, which is stored with the
    others; a thread none of whose messages is kept, and a record a rule excludes whole, are not
    stored. Rules apply to the batches after a change; ``POST …/sources/{source}/prune`` applies
    them to the records stored before."""

    model_config = ConfigDict(extra="forbid")

    allow_channels: _Globs = Field(
        default_factory=list[str],
        description="Store a thread only when its channel matches one of these; a thread without"
        " a channel is not stored while any is set. " + _GLOBS,
    )
    allow_domains: _Globs = Field(
        default_factory=list[str],
        description="Keep a message only when an e-mail address among its author and recipients"
        " is in one of these domains or below one (example.test admits eu.example.test); a"
        " message without such an address is left out. Host names, stored in lower case.",
    )
    exclude_authors: _Globs = Field(
        default_factory=list[str],
        description="Leave out a message whose author, as the source shows it or the address in"
        " it, matches one of these (*bot*, build-robot@*). " + _GLOBS,
    )
    exclude_titles: _Globs = Field(
        default_factory=list[str],
        description="Do not store a meeting, document or thread whose title (a thread's subject)"
        " matches one of these. " + _GLOBS,
    )
    exclude_automated: bool = Field(
        default=False,
        strict=True,
        description="Leave out automated mail: a message from a no-reply address (noreply,"
        " no-reply, donotreply, mailer-daemon), or whose headers object has Auto-Submitted other"
        " than no, or Precedence bulk or junk.",
    )

    def to_filters(self) -> SourceFilters:
        return SourceFilters(
            allow_channels=tuple(self.allow_channels),
            allow_domains=tuple(self.allow_domains),
            exclude_authors=tuple(self.exclude_authors),
            exclude_titles=tuple(self.exclude_titles),
            exclude_automated=self.exclude_automated,
        )

    @classmethod
    def of(cls, filters: SourceFilters) -> "FiltersBody":
        return cls(
            allow_channels=list(filters.allow_channels),
            allow_domains=list(filters.allow_domains),
            exclude_authors=list(filters.exclude_authors),
            exclude_titles=list(filters.exclude_titles),
            exclude_automated=filters.exclude_automated,
        )


class NewSource(BaseModel):
    """A repository to add."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["repository"] = Field(description="The kind of source: git.")
    name: str = Field(pattern=HANDLE_REGEX, description=_NAME)
    location: str = Field(min_length=1, max_length=MAX_LOCATION_LENGTH, description=_LOCATION)
    refs: list[str] = Field(min_length=1, max_length=MAX_REFS, description=_REFS)
    schedule_seconds: int | None = Field(
        default=None,
        ge=int(MIN_SCHEDULE.total_seconds()),
        le=int(MAX_SCHEDULE.total_seconds()),
        description=_SCHEDULE,
    )
    include_paths: _Patterns = Field(default_factory=list[str], description=_PATTERNS)
    exclude_paths: _Patterns = Field(default_factory=list[str], description=_PATTERNS)
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
            include=self.include_paths,
            exclude=self.exclude_paths,
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
    filters: FiltersBody = Field(
        default_factory=FiltersBody,
        description="Which of its records, and messages of threads, it stores; none: all.",
    )

    def to_new(self) -> sources.NewPushSource:
        return sources.NewPushSource(
            name=self.name,
            visibility=self.visibility,
            retention_days=self.retention_days,
            llm_policy=self.llm_policy,
            filters=self.filters.to_filters(),
        )


class EndpointBody(BaseModel):
    """Where an mcp source's server is: a URL (streamable HTTP), or a command the service starts
    (stdio). Exactly one of ``url`` and ``command``."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_URL_LENGTH,
        description="An https:// URL of an MCP server inside MEMINI_MCP_ALLOWED_ENDPOINTS, whose"
        " host resolves to no internal address unless MEMINI_MCP_ALLOWED_NETWORKS lists it; plain"
        " http:// only with MEMINI_MCP_ALLOW_HTTP. Never with a user name, password, query or"
        " fragment: the credential comes from the credential store.",
    )
    command: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="The absolute path of an executable listed in MEMINI_MCP_ALLOWED_COMMANDS,"
        " run as an MCP server over stdio.",
    )
    args: list[Annotated[str, Field(max_length=MAX_ARG_LENGTH)]] = Field(
        default_factory=list[str],
        max_length=MAX_ARGS,
        description="With command: its arguments.",
    )
    credential_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z_][A-Z0-9_]{0,127}$",
        description="With command: the environment variable the source's credential is passed"
        " in; left out, the server gets none.",
    )

    @model_validator(mode="after")
    def _one_kind(self) -> "EndpointBody":
        if (self.url is None) == (self.command is None):
            raise ValueError("give url or command, one of them")
        if self.url is not None and (self.args or self.credential_env is not None):
            raise ValueError("args and credential_env go with command, not with url")
        return self

    def to_endpoint(self) -> Endpoint:
        if self.url is not None:
            return HttpEndpoint(self.url)
        return StdioEndpoint(
            command=self.command or "", args=tuple(self.args), credential_env=self.credential_env
        )

    @classmethod
    def of(cls, endpoint: Endpoint) -> "EndpointBody":
        if isinstance(endpoint, HttpEndpoint):
            return cls(url=endpoint.url)
        return cls(
            command=endpoint.command,
            args=list(endpoint.args),
            credential_env=endpoint.credential_env,
        )


class RecipeBody(BaseModel):
    """The recipe of an mcp source: a recipe file's text, or a recipe that ships with Memini.
    Exactly one of ``toml`` and ``bundled``."""

    model_config = ConfigDict(extra="forbid")

    toml: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RECIPE_TEXT,
        description="The text of a recipe file (docs/recipes.md), which the source holds:"
        " checked now, and its parser must be part of the service's build.",
    )
    bundled: str | None = Field(
        default=None,
        pattern=HANDLE_REGEX,
        description="The name of a recipe that ships with Memini; read again at every sync.",
    )

    @model_validator(mode="after")
    def _one_kind(self) -> "RecipeBody":
        if (self.toml is None) == (self.bundled is None):
            raise ValueError("give toml or bundled, one of them")
        return self


class NewMcpSource(BaseModel):
    """An mcp source to add: an MCP server the service syncs with a recipe, on a schedule or
    when triggered. Its credential, if it needs one, is set with ``memini credentials set``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["mcp"] = Field(description="The kind of source: mcp.")
    name: str = Field(pattern=HANDLE_REGEX, description=_NAME)
    endpoint: EndpointBody
    recipe: RecipeBody
    streams: list[Annotated[str, Field(min_length=1, max_length=MAX_STREAM_KEY_LENGTH)]] = Field(
        min_length=1, max_length=MAX_STREAMS, description=_STREAMS
    )
    schedule_seconds: int | None = Field(
        default=None,
        ge=int(MIN_SCHEDULE.total_seconds()),
        le=int(MAX_SCHEDULE.total_seconds()),
        description=_SCHEDULE,
    )
    window: dict[str, str | None] = Field(
        default_factory=dict[str, str | None], description=_WINDOW
    )
    visibility: Visibility = Field(default="private", description=_VISIBILITY)
    retention_days: int | None = Field(
        default=None, ge=1, le=MAX_RETENTION_DAYS, description=_RETENTION
    )
    llm_policy: LlmPolicy = Field(default="local", description=_LLM_POLICY)
    filters: FiltersBody = Field(
        default_factory=FiltersBody,
        description="Which of its records, and messages of threads, it stores; none: all.",
    )

    def to_new(self) -> sources.NewMcpSource:
        return sources.NewMcpSource(
            name=self.name,
            endpoint=self.endpoint.to_endpoint(),
            streams=self.streams,
            recipe_toml=self.recipe.toml,
            recipe_name=self.recipe.bundled,
            schedule=_seconds(self.schedule_seconds),
            window=self.window,
            visibility=self.visibility,
            retention_days=self.retention_days,
            llm_policy=self.llm_policy,
            filters=self.filters.to_filters(),
        )


class SourceChanges(BaseModel):
    """Settings to change. A setting left out stays as it is; ``refs``, ``include_paths`` and
    ``exclude_paths`` replace the whole list, and ``filters`` every filter rule of a push source.
    The location cannot change: a different repository is a different source, so delete this one and
    add a new one."""

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
    include_paths: _Patterns | None = Field(default=None, description=_PATTERNS)
    exclude_paths: _Patterns | None = Field(default=None, description=_PATTERNS)
    builtin_excludes: bool | None = Field(default=None, strict=True, description=_BUILTIN_EXCLUDES)
    visibility: Visibility | None = Field(default=None, description=_VISIBILITY)
    retention_days: int | None = Field(
        default=None, ge=1, le=MAX_RETENTION_DAYS, description=_RETENTION
    )
    llm_policy: LlmPolicy | None = Field(default=None, description=_LLM_POLICY)
    filters: FiltersBody | None = Field(
        default=None,
        description="Push and mcp sources: the filter rules, all of them; a rule left out is not"
        " set, and {} clears every rule. They apply to the batches after the change; prune"
        " applies them to the records stored before.",
    )
    streams: list[Annotated[str, Field(min_length=1, max_length=MAX_STREAM_KEY_LENGTH)]] | None = (
        Field(
            default=None,
            min_length=1,
            max_length=MAX_STREAMS,
            description=_STREAMS + " Streams of keys left out are deleted with their runs.",
        )
    )
    endpoint: EndpointBody | None = Field(
        default=None, description="mcp: a new endpoint, checked as when the source was added."
    )
    recipe: RecipeBody | None = Field(
        default=None,
        description="mcp: a new recipe; the source's streams and window must be valid with it.",
    )
    window: dict[str, str | None] | None = Field(
        default=None, description=_WINDOW + " Replaces every override; {} clears them."
    )

    @field_validator(
        "name",
        "refs",
        "streams",
        "endpoint",
        "recipe",
        "window",
        "include_paths",
        "exclude_paths",
        "builtin_excludes",
        "visibility",
        "llm_policy",
        "filters",
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
            include=self.include_paths
            if "include_paths" in given and self.include_paths is not None
            else unset,
            exclude=self.exclude_paths
            if "exclude_paths" in given and self.exclude_paths is not None
            else unset,
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
            filters=(
                self.filters.to_filters()
                if "filters" in given and self.filters is not None
                else unset
            ),
            streams=self.streams if "streams" in given and self.streams is not None else unset,
            endpoint=(
                self.endpoint.to_endpoint()
                if "endpoint" in given and self.endpoint is not None
                else unset
            ),
            recipe_toml=(
                self.recipe.toml
                if "recipe" in given and self.recipe is not None and self.recipe.toml is not None
                else unset
            ),
            recipe_name=(
                self.recipe.bundled
                if "recipe" in given and self.recipe is not None and self.recipe.bundled is not None
                else unset
            ),
            window=self.window if "window" in given and self.window is not None else unset,
        )


def _seconds(value: int | None) -> timedelta | None:
    return None if value is None else timedelta(seconds=value)


class McpRecipeInfo(BaseModel):
    name: str = Field(description="The recipe's name.")
    bundled: bool = Field(
        description="true: a recipe that ships with Memini, read at every sync; false: the"
        " recipe text the source holds."
    )
    digest: str | None = Field(
        description="The digest of the recipe the source holds; each run records the one it read."
    )


class McpInfo(BaseModel):
    """The settings of an mcp source."""

    endpoint: EndpointBody | None = Field(description="Where its server is.")
    recipe: McpRecipeInfo | None
    window: dict[str, str | None] = Field(description=_WINDOW)

    @classmethod
    def of(cls, config: McpConfig) -> "McpInfo":
        ref = config.recipe
        return cls(
            endpoint=None if config.endpoint is None else EndpointBody.of(config.endpoint),
            recipe=None
            if ref is None
            else McpRecipeInfo(name=ref.name, bundled=ref.bundled, digest=ref.digest),
            window=dict(config.window),
        )


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
    include_paths: list[str] = Field(description=_PATTERNS)
    exclude_paths: list[str]
    builtin_excludes: bool | None = Field(
        description=_BUILTIN_EXCLUDES + " null for other kinds than git."
    )
    visibility: Visibility
    retention_days: int | None
    llm_policy: LlmPolicy
    filters: FiltersBody | None = Field(
        description="Push and mcp sources: which records and messages they store; null for git."
    )
    mcp: McpInfo | None = Field(
        default=None, description="mcp: endpoint, recipe and window; null for other kinds."
    )
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
            include_paths=[] if git is None else list(git.include),
            exclude_paths=[] if git is None else list(git.exclude),
            builtin_excludes=None if git is None else git.builtin_excludes,
            visibility=source.visibility,
            retention_days=source.retention_days,
            llm_policy=source.llm_policy,
            filters=None if source.filters is None else FiltersBody.of(source.filters),
            mcp=None if source.mcp is None else McpInfo.of(source.mcp),
            created_at=source.created_at,
            updated_at=source.updated_at,
        )


class SourceList(BaseModel):
    items: list[SourceInfo] = Field(description="By name.")


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
    items: list[StreamInfo] = Field(description="By key.")


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streams: list[Annotated[str, Field(min_length=1, max_length=MAX_REF_LENGTH)]] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_REFS,
        description="The keys of the streams to sync, as listed (refs/heads/main); null or left"
        " out: every stream of the source.",
    )


class PreviewGroupInfo(BaseModel):
    key: str = Field(
        description="An extension in lower case with its dot (.go), (none) for a name without"
        " one; or a top-level folder, (root) for files at the top. … sums the groups past the"
        " first 50."
    )
    tracked: int = Field(description="Regular files of the group in the tree.")
    indexed: int = Field(description="Of those, the files the rules keep.")


class PreviewInfo(BaseModel):
    """What a sync with these rules would index at the ref. Binary files and files forgotten by
    hand are counted as kept: the sync finds them when it reads them."""

    ref: str
    commit: str
    tracked: int = Field(description="Regular files of the tree.")
    indexed: int = Field(description="Files the rules keep.")
    skipped: dict[str, int] = Field(
        description="Entries left out, by reason: excluded, too_large, not_a_file, bad_path."
    )
    by_extension: list[PreviewGroupInfo] = Field(description="Most files first.")
    by_folder: list[PreviewGroupInfo] = Field(description="Most files first.")

    @classmethod
    def of(cls, preview: Preview) -> "PreviewInfo":
        return cls(
            ref=preview.ref,
            commit=preview.commit,
            tracked=preview.tracked,
            indexed=preview.indexed,
            skipped={str(reason): count for reason, count in preview.skipped.items()},
            by_extension=[
                PreviewGroupInfo(key=g.key, tracked=g.tracked, indexed=g.indexed)
                for g in preview.by_extension
            ],
            by_folder=[
                PreviewGroupInfo(key=g.key, tracked=g.tracked, indexed=g.indexed)
                for g in preview.by_folder
            ],
        )


class SyncJob(BaseModel):
    stream: str = Field(description="The stream's key.")
    job_id: UUID
    created: bool = Field(
        description="false: a sync of the stream was already queued or running, and this is it."
    )


class SyncAccepted(BaseModel):
    jobs: list[SyncJob] = Field(description="One per stream, by key.")


class PruneCounts(BaseModel):
    """Each record read counts once, in deleted, trimmed, unchanged or unreadable."""

    scanned: int = Field(description="Records with a payload read, live ones and deleted ones.")
    deleted: int = Field(
        description="Records the filters exclude: deleted with their chunks, not kept as"
        " tombstones; the filters keep them out of later batches."
    )
    trimmed: int = Field(
        description="Threads written again without the messages the filters leave out, their"
        " chunks derived again."
    )
    messages_removed: int = Field(description="Messages taken out of threads, trimmed or deleted.")
    unchanged: int = Field(description="Records the filters keep as they are: nothing written.")
    unreadable: int = Field(description="Payloads the parser of their kind does not read: kept.")


class PruneAccepted(BaseModel):
    run_id: UUID = Field(description="The prune's run; GET /v1/runs/{id} has its report.")
    rules: bool = Field(
        description="Whether the source has any filter rule; without one nothing is read."
    )
    counts: PruneCounts
    chunks_deleted: int = Field(description="Chunks of deleted records, and of trimmed ones.")
    chunks_written: int = Field(description="Chunks of trimmed threads written again.")
    texts_embedded: int = Field(description="Texts sent to the embedding model.")
    records_not_indexed: int = Field(
        description="Trimmed records whose chunks were deleted, not derived again, as no"
        " embedding provider is configured: memini reindex indexes them."
    )

    @classmethod
    def of(cls, result: prune.PruneResult, run_id: UUID) -> "PruneAccepted":
        index = result.index
        return cls(
            run_id=run_id,
            rules=result.rules,
            counts=PruneCounts(
                scanned=result.scanned,
                deleted=result.deleted,
                trimmed=result.trimmed,
                messages_removed=result.messages_removed,
                unchanged=result.unchanged,
                unreadable=result.unreadable,
            ),
            chunks_deleted=index.deleted,
            chunks_written=index.inserted + index.updated,
            texts_embedded=index.embedded,
            records_not_indexed=result.not_indexed,
        )


@contextmanager
def _prune_errors() -> Generator[None]:
    try:
        yield
    except prune.NotPrunableError:
        raise ApiError(
            409,
            "not_prunable",
            "A repository has no filters: its include_paths and exclude_paths apply at its next"
            " sync.",
        ) from None
    except prune.PruneConflictError:
        raise ApiError(
            409,
            "prune_conflict",
            "Another writer held the same records; what was pruned stays, run the prune again.",
        ) from None
    except IndexMismatchError as error:
        raise ApiError(
            503, "index_mismatch", f"What was pruned before stays; the rest waits: {error}."
        ) from None
    except EmbeddingError as error:
        log.warning("the trimmed records of a prune could not be embedded: %s", error)
        raise ApiError(
            503,
            "embedding_failed",
            "Trimmed records could not be embedded; what was pruned before stays, and the server"
            " log says why. Run the prune again later.",
        ) from None


def git_options(request: Request) -> GitOptions:
    """The git connector's options, from the application's settings."""
    settings = cast(Settings, request.app.state.settings)
    return GitOptions.from_settings(settings)


GitSettings = Annotated[GitOptions, Depends(git_options)]


def mcp_policy(request: Request) -> EgressPolicy:
    """The egress policy of mcp sources, from the application's settings."""
    settings = cast(Settings, request.app.state.settings)
    return EgressPolicy.from_settings(settings)


McpPolicy = Annotated[EgressPolicy, Depends(mcp_policy)]


def build_sources_router() -> APIRouter:
    """The source routes; see the module."""
    router = APIRouter(prefix="/workspaces/{workspace}/sources", tags=["sources"])

    @router.get("", summary="List sources")
    async def list_sources(workspace: str, access: ReadAccess, conn: Connection) -> SourceList:
        """The sources of a workspace, by name. 404 if the workspace does not exist or the token
        may not read it."""
        with admin_errors():
            found = await sources.list_sources(conn, access, workspace)
        return SourceList(items=[SourceInfo.of(source) for source in found])

    @router.post("", status_code=201, summary="Add a source")
    async def create_source(
        workspace: str,
        body: Annotated[NewSource | NewPushSource | NewMcpSource, Field(discriminator="kind")],
        access: AdminAccess,
        options: GitSettings,
        policy: McpPolicy,
        conn: Connection,
    ) -> SourceInfo:
        """Add a repository with one stream per ref, a push source, or an mcp source with one
        stream per stream key. A git location is checked by the rules the mirror enforces (422
        ``invalid_source`` names the setting and why, never the value), not fetched: the first
        sync creates the mirror. A push source takes batches of records at ``POST
        …/sources/{source}/records``. An mcp source's endpoint is checked against the
        ``mcp_*`` settings, not connected to, and its recipe must be valid with a parser in this
        build. 409 if the workspace has a source of that name."""
        with admin_errors():
            if isinstance(body, NewPushSource):
                source = await sources.create_push_source(conn, access, workspace, body.to_new())
            elif isinstance(body, NewMcpSource):
                source = await sources.create_mcp_source(
                    conn, access, workspace, body.to_new(), policy=policy
                )
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
        workspace: str,
        source: str,
        body: SourceChanges,
        access: AdminAccess,
        policy: McpPolicy,
        conn: Connection,
    ) -> SourceInfo:
        """Change settings of a source; see the request body. A new schedule starts now. Refs
        (git) or stream keys (mcp) left out lose their streams, with their runs and queued
        syncs."""
        with admin_errors():
            changed = await sources.update_source(
                conn, access, workspace, source, body.to_update(), policy=policy
            )
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
        return StreamList(items=[StreamInfo.of(stream) for stream in found])

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

    @router.get("/{source}/preview", summary="Preview what a repository indexes")
    async def preview_source(
        workspace: str,
        source: str,
        access: ReadAccess,
        options: GitSettings,
        conn: Connection,
        ref: Annotated[
            str | None,
            Query(
                min_length=6,
                max_length=MAX_REF_LENGTH,
                pattern=r"^refs/[^\s]+$",
                description="The ref to look at, such as refs/heads/main: one the mirror holds."
                " Left out: the source's first tracked ref.",
            ),
        ] = None,
        include_paths: Annotated[
            list[str] | None,
            Query(description=_PATTERNS + " Left out: the source's own; an empty value clears."),
        ] = None,
        exclude_paths: Annotated[
            list[str] | None,
            Query(description=_PATTERNS + " Left out: the source's own; an empty value clears."),
        ] = None,
        builtin_excludes: Annotated[
            bool | None, Query(description=_BUILTIN_EXCLUDES + " Left out: the source's own.")
        ] = None,
    ) -> PreviewInfo:
        """Count what a sync would index at a ref, with the source's rules or with the ones in the
        query, by extension and by top-level folder, from the source's mirror. Writes nothing; the
        rules are not saved (``PATCH`` saves them). ``include_paths`` and ``exclude_paths``
        repeat, one glob each; an empty one (``exclude_paths=``) stands for an empty list. 409
        ``not_syncable`` for a source that is not a repository, 409 ``not_fetched`` before its
        first fetch, 404 for a ref the mirror lacks."""
        with admin_errors():
            found = await sources.get_source(conn, access, workspace, source)
            if found.git is None:
                raise sources.NotSyncableError
            config = found.git
            include_rules = (
                config.include
                if include_paths is None
                else sources.check_patterns("include", [p for p in include_paths if p])
            )
            exclude_rules = (
                config.exclude
                if exclude_paths is None
                else sources.check_patterns("exclude", [p for p in exclude_paths if p])
            )
        chosen = ref or (found.refs[0] if found.refs else None)
        if chosen is None:
            raise ApiError(409, "no_refs", "The source tracks no ref; name one in ref.")
        builtin = config.builtin_excludes if builtin_excludes is None else builtin_excludes
        rules = FileRules(
            sources.GitConfig(
                location=config.location,
                include=include_rules,
                exclude=exclude_rules,
                builtin_excludes=builtin,
            ),
            options.max_blob_size,
        )
        mirrors = MirrorManager(options)
        mirror = mirrors.find(found.id)
        if mirror is None:
            raise ApiError(
                409,
                "not_fetched",
                "The source has no mirror yet: its first sync fetches it. Try again then.",
            )
        try:
            return PreviewInfo.of(await preview_ref(mirrors, mirror, chosen, rules))
        except RevisionNotFound:
            raise ApiError(404, "not_found", "No such ref in the source's mirror.") from None
        except GitError as error:
            log.warning("the preview of a source failed: %s", error)
            raise ApiError(
                503, "git_failed", "git could not list the ref; the server log says why."
            ) from None

    @router.post("/{source}/prune", summary="Apply a source's filters to its stored records")
    async def prune_source(
        workspace: str, source: str, access: AdminAccess, request: Request, conn: Connection
    ) -> PruneAccepted:
        """Apply the current filters of a push source to the records it has stored: a record
        they exclude is deleted with its chunks; a thread loses the messages they leave out and
        gets its chunks again; the rest is left as it is. Running it again changes nothing. Pages
        of records commit one by one, so a prune that fails keeps what it pruned; the answer and
        the run report count what it did. 409 ``not_prunable`` for a repository."""
        pool = cast(Pool, request.app.state.pool)
        embedder = cast(QueryServices, request.app.state.services).embedder
        indexer = None if embedder is None else PayloadIndexer(embedder)
        with admin_errors(), _prune_errors():
            result = await prune.prune_source(
                conn, access, workspace, source, runs=pool, indexer=indexer
            )
        if result.run_id is None:  # a run is recorded whenever runs is given
            raise RuntimeError("the prune recorded no run")
        return PruneAccepted.of(result, result.run_id)

    return router
