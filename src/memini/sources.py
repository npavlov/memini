"""Sources and their streams: add, read, change and delete them, and trigger their syncs.

Source configuration lives in the database and is managed through these functions; the HTTP API
(:mod:`memini.api.sources`) is a thin layer over them, and the CLI a client of that API
(requirements, sections 6 and 7). There is no configuration file to apply.

A **push source** is a name and the shared settings below (``visibility``, ``retention_days``,
``llm_policy``), and its filters, nothing else: clients send its records through the ingest API
(:mod:`memini.ingest`), and its streams are created by the first batch that names them. It has no
location, refs or schedule, and the service never syncs it (:func:`create_push_source`).

The **filters** of a push or mcp source are its ``source.config``: ``{"filters": {...}}``, the
rules of :class:`memini.filters.SourceFilters` that are set (allowlists of channels and e-mail
domains, exclusions of authors, titles and automated mail), or ``{}`` without any. Ingest applies
them to every batch; a change applies to the batches after it, and ``memini source prune``
(:mod:`memini.prune`) applies the current rules to the records stored before.

An **mcp source** is an MCP server the service syncs with a recipe (:mod:`memini.executors`):
``source.config`` holds its endpoint (a URL, or a stdio command), its recipe (the TOML text it
holds, or the name of a recipe that ships with Memini), its overrides of the recipe's window and its
filters, in the shape :class:`memini.executors.source.McpConfig` describes. The endpoint is checked
against the egress policy when the source is added or changed (the ``mcp_*`` settings), and again
before every connection; the recipe must be valid and its parser part of this build. Each stream is
a stream key of the recipe (a channel id, a mailbox query), which must match its
``stream.key_pattern``; the schedule is set on all streams at once, as for git. Its credential is
its row in the credential store (``memini credentials set``), never part of the config.

A **repository** is:

- ``source.config``, a JSON object of the shape :class:`GitConfig` reads and writes::

      {"location": "https://git.example.com/team/app.git",
       "include": ["src/**", "docs/**/*.md"],
       "exclude": ["**/vendor/**"],
       "builtin_excludes": false}

  ``location`` is a remote URL or an absolute local path, checked when the source is added with the
  rules the mirror enforces at every fetch (:func:`check_location`), and stored in the form the
  mirror is given: a canonical URL or the resolved path. It never holds a credential: a URL with
  a user name or password is refused, and credentials come from the settings. ``include`` and
  ``exclude`` are path globs, described at :func:`path_matches`; a file is indexed when
  :meth:`GitConfig.selects` it. ``builtin_excludes`` (true when left out, and only stored when
  false) applies the connector's built-in exclusions of generated files
  (:data:`memini.gitsync.BUILTIN_EXCLUDE`: lock files, minified bundles, source maps,
  ``node_modules``); false indexes them too, unless ``exclude`` leaves them out. The location
  cannot change: a different repository is a different source, and the mirror, the pinned commits
  and the index all belong to the old one.
- One stream per tracked ref. A ref is given as a branch name (``main``) or a full name under
  ``refs/heads/`` or ``refs/tags/``; the stream's key is the full name (``refs/heads/main``).
  Changing the refs adds and removes streams; removing one deletes its runs and jobs.
- A schedule: every stream of the source syncs this often, or only when triggered. It is kept on
  each stream, where the scheduler reads it; the functions here keep all streams of a source on
  the same one. Changing it sets ``next_run_at`` to NULL, so the new schedule starts now.
- ``visibility``, ``retention_days`` and ``llm_policy``, with the schema's defaults (``private``,
  none, ``local``).

Access: every read takes the caller's :class:`~memini.auth.ReadFilter` and every write the
caller's :class:`~memini.auth.WriteAccess`; both go into the SQL that finds the rows. A workspace,
source or stream the caller may not read or change raises :class:`NotFoundError`, as one that
does not exist does.

Transactions: every write runs in a transaction of its own, or in a savepoint when the caller has
one open, so a refused write leaves the caller's connection usable.

Deleting a source deletes its streams, runs, jobs and records (the foreign keys cascade) and the
mirror of a repository, in the request: under the source's advisory lock, which a worker holds
while it runs a job of the source, so no job writes the mirror while it is removed and none starts
until the deletion commits. While a job of the source runs, the deletion is refused
(:class:`SourceBusyError`) rather than made to wait for a fetch that may take minutes.
"""

import enum
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, get_args
from uuid import UUID

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from memini.auth import ReadFilter, WriteAccess
from memini.executors.egress import EgressPolicy
from memini.executors.source import (
    Endpoint,
    McpConfig,
    McpConfigError,
    RecipeRef,
    bundled_recipe,
    check_endpoint,
    check_stream_keys,
    check_window,
    inline_recipe,
    load_recipe_ref,
)
from memini.filters import FilterError, SourceFilters, check_filters
from memini.git import (
    GitOptions,
    InvalidRevision,
    MirrorBusy,
    MirrorManager,
    RemoteNotAllowed,
    check_tracked_ref,
    parse_remote,
)
from memini.jobs import SYNC_KINDS, enqueue, source_lock_key, stream_dedupe_key
from memini.status import stream_is_current
from memini.workspaces import NotFoundError, check_handle, get_workspace, get_writable_workspace

if TYPE_CHECKING:  # memini.recipes imports memini.ingest, which imports this module's importers
    from memini.recipes import Recipe

__all__ = [
    "MAX_LOCATION_LENGTH",
    "MAX_PATTERNS",
    "MAX_PATTERN_LENGTH",
    "MAX_REFS",
    "MAX_REF_LENGTH",
    "MAX_RETENTION_DAYS",
    "MAX_SCHEDULE",
    "MIN_SCHEDULE",
    "UNSET",
    "GitConfig",
    "InvalidSourceError",
    "JobBrief",
    "LlmPolicy",
    "NewGitSource",
    "NewMcpSource",
    "NewPushSource",
    "NotFoundError",
    "NotSyncableError",
    "RunBrief",
    "Source",
    "SourceBusyError",
    "SourceExistsError",
    "SourceFilters",
    "SourceKind",
    "SourceUpdate",
    "Stream",
    "SyncJob",
    "Unset",
    "Visibility",
    "check_filters_setting",
    "check_location",
    "check_patterns",
    "check_ref",
    "check_refs",
    "check_schedule",
    "create_git_source",
    "create_mcp_source",
    "create_push_source",
    "delete_source",
    "get_source",
    "list_sources",
    "list_streams",
    "path_matches",
    "trigger_sync",
    "update_source",
]

type Conn = AsyncConnection[TupleRow]

SourceKind = Literal["repository", "push", "mcp"]
Visibility = Literal["private", "workspace"]
LlmPolicy = Literal["none", "local", "cloud"]
RunStatus = Literal["running", "succeeded", "partial", "failed", "cancelled"]
PendingJobStatus = Literal["queued", "running"]

MIN_SCHEDULE: Final = timedelta(minutes=1)
MAX_SCHEDULE: Final = timedelta(days=366)
MAX_REFS: Final = 100
MAX_REF_LENGTH: Final = 1024
MAX_PATTERNS: Final = 100
MAX_PATTERN_LENGTH: Final = 1024
MAX_LOCATION_LENGTH: Final = 2048
MAX_RETENTION_DAYS: Final = 36525

# The name Postgres gives UNIQUE (workspace_id, name) on source (migration 0002).
_SOURCE_NAME_KEY: Final = "source_workspace_id_name_key"


# --- Errors --------------------------------------------------------------------------------------


class InvalidSourceError(ValueError):
    """A source setting was refused. The message names the setting and the reason, never the
    value, so it can be shown to the caller and logged."""

    def __init__(self, setting: str, reason: str) -> None:
        super().__init__(f"{setting}: {reason}")
        self.setting = setting
        self.reason = reason


class SourceExistsError(Exception):
    """A source of that name exists in the workspace."""


class SourceBusyError(Exception):
    """A job of the source is running, so the source cannot be deleted now."""


class NotSyncableError(Exception):
    """The source's kind has no sync job: clients send its records (``push``)."""


# --- Configuration -------------------------------------------------------------------------------


def path_matches(pattern: str, path: str) -> bool:
    """Whether ``path``, relative to the repository root (``src/app/main.py``), matches ``pattern``.

    A pattern is matched against the whole path, ``/``-separated:

    - ``*`` matches any characters within one path component, ``?`` one character;
    - ``[abc]``, ``[a-z]`` and ``[!abc]`` match one character of, or not of, a set;
    - ``**`` as a whole component matches any number of components, none included:
      ``**/*.py`` matches every Python file, ``docs/**`` everything under ``docs``.

    So ``*.py`` matches Python files at the top of the repository only.
    """
    return _glob_regex(pattern).fullmatch(path) is not None


_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_regex(pattern: str) -> re.Pattern[str]:
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    components = pattern.split("/")
    parts: list[str] = []
    for index, component in enumerate(components):
        last = index == len(components) - 1
        if component == "**":
            parts.append(".*" if last else "(?:.*/)?")
        else:
            parts.append(_component_regex(component) + ("" if last else "/"))
    compiled = re.compile("".join(parts), re.DOTALL)
    if len(_GLOB_CACHE) < 4096:
        _GLOB_CACHE[pattern] = compiled
    return compiled


def _component_regex(component: str) -> str:
    out: list[str] = []
    index, end = 0, len(component)
    while index < end:
        char = component[index]
        index += 1
        if char == "*":
            while index < end and component[index] == "*":
                index += 1
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and (close := _class_end(component, index)) is not None:
            body = component[index:close]
            index = close + 1
            negate = body[:1] in ("!", "^")
            if negate:
                body = body[1:]
            escaped = "".join(c if c == "-" else re.escape(c) for c in body)
            out.append(f"[^/{escaped}]" if negate else f"[{escaped}]")
        else:
            out.append(re.escape(char))
    return "".join(out)


def _class_end(component: str, start: int) -> int | None:
    """The index of the ``]`` that closes a set opened just before ``start``; ``None`` if none."""
    index = start
    if index < len(component) and component[index] in "!^":
        index += 1
    if index < len(component) and component[index] == "]":
        index += 1  # a leading ] is a member of the set
    close = component.find("]", index)
    return None if close < 0 else close


@dataclass(frozen=True, slots=True)
class GitConfig:
    """``source.config`` of a repository. See the module for each setting."""

    location: str
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    builtin_excludes: bool = True
    """Apply the built-in exclusions of generated files (:mod:`memini.gitsync.rules`)."""

    def to_json(self) -> dict[str, Any]:
        """The JSON object stored in ``source.config``; ``builtin_excludes`` only when false."""
        value: dict[str, Any] = {
            "location": self.location,
            "include": list(self.include),
            "exclude": list(self.exclude),
        }
        if not self.builtin_excludes:
            value["builtin_excludes"] = False
        return value

    @classmethod
    def from_json(cls, config: Mapping[str, Any]) -> "GitConfig":
        """The configuration as stored. Not checked again: a setting that has changed since the
        source was added (local roots, plain http) is enforced by the mirror at the next fetch, and
        reading a source must not fail on it. Built-in exclusions apply unless the value is
        ``false``."""
        location = config.get("location")
        return cls(
            location=location if isinstance(location, str) else "",
            include=_strings(config.get("include")),
            exclude=_strings(config.get("exclude")),
            builtin_excludes=config.get("builtin_excludes") is not False,
        )

    def selects(self, path: str) -> bool:
        """Whether the file at ``path`` is indexed: it matches an ``include`` pattern (any path,
        when there is none) and no ``exclude`` pattern."""
        if self.include and not any(path_matches(pattern, path) for pattern in self.include):
            return False
        return not any(path_matches(pattern, path) for pattern in self.exclude)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items: list[object] = value  # pyright: ignore[reportUnknownVariableType] - JSON from the database
    return tuple(item for item in items if isinstance(item, str))


def check_location(location: str, options: GitOptions) -> str:
    """The location of a repository as it is stored, or :class:`InvalidSourceError`.

    Checked with the rules the mirror enforces at every fetch (:func:`memini.git.parse_remote`):
    https and ssh remotes; plain http and local paths only when the settings allow them; never a
    user name or password in a URL, a transport helper (``ext::``), another scheme, or a path
    outside ``git_local_roots``. An ssh remote also needs the ssh key and known_hosts settings.
    The error never repeats the location, which may hold a credential.
    """
    if len(location) > MAX_LOCATION_LENGTH:
        raise InvalidSourceError("location", f"longer than {MAX_LOCATION_LENGTH} characters")
    try:
        remote = parse_remote(location, options)
    except RemoteNotAllowed as error:
        raise InvalidSourceError("location", str(error)) from None
    if remote.transport == "ssh" and (
        options.ssh_key_path is None or options.ssh_known_hosts_path is None
    ):
        raise InvalidSourceError(
            "location",
            "ssh remotes need a key and a known_hosts file: set MEMINI_GIT_SSH_KEY_PATH and"
            " MEMINI_GIT_SSH_KNOWN_HOSTS_PATH",
        )
    return remote.url


def check_ref(ref: str) -> str:
    """The stream key of a tracked ref: a branch name ``main`` becomes ``refs/heads/main``; a full
    name under ``refs/heads/`` or ``refs/tags/`` stays as it is. :class:`InvalidSourceError` for
    anything else."""
    full = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
    if len(full) <= MAX_REF_LENGTH:
        try:
            return check_tracked_ref(full)
        except InvalidRevision:
            pass
    raise InvalidSourceError(
        "refs",
        "a ref is a branch name (main), refs/heads/<branch> or refs/tags/<tag>, valid by git's"
        f" rules and at most {MAX_REF_LENGTH} characters long",
    )


def check_refs(refs: Sequence[str]) -> tuple[str, ...]:
    """The stream keys of the tracked refs (:func:`check_ref`), sorted, without duplicates."""
    if isinstance(refs, str):
        raise TypeError("refs is a sequence of ref names, not one string")
    keys = sorted({check_ref(ref) for ref in refs})
    if not keys:
        raise InvalidSourceError("refs", "a repository tracks at least one ref")
    if len(keys) > MAX_REFS:
        raise InvalidSourceError("refs", f"a source tracks at most {MAX_REFS} refs")
    return tuple(keys)


def check_patterns(setting: str, patterns: Sequence[str]) -> tuple[str, ...]:
    """Path globs (:func:`path_matches`) as they are stored: in the order given, without
    duplicates. :class:`InvalidSourceError` naming ``setting`` for a pattern that is empty,
    absolute, has empty, ``.`` or ``..`` components or control characters, or is too long."""
    if isinstance(patterns, str):
        raise TypeError(f"{setting} is a sequence of patterns, not one string")
    unique = tuple(dict.fromkeys(patterns))
    if len(unique) > MAX_PATTERNS:
        raise InvalidSourceError(setting, f"at most {MAX_PATTERNS} patterns")
    for pattern in unique:
        if (
            not pattern
            or len(pattern) > MAX_PATTERN_LENGTH
            or not pattern.isprintable()
            or any(part in ("", ".", "..") for part in pattern.split("/"))
        ):
            raise InvalidSourceError(
                setting,
                "a pattern is a path relative to the repository root, such as src/** or"
                f" **/*.md, without empty, '.' or '..' components, at most {MAX_PATTERN_LENGTH}"
                " characters",
            )
    return unique


def check_filters_setting(filters: SourceFilters) -> SourceFilters:
    """The filter rules of a push or mcp source as they are stored (:func:`memini.filters.
    check_filters`), or :class:`InvalidSourceError` naming the rule (``filters.allow_domains``)."""
    try:
        return check_filters(filters)
    except FilterError as error:
        raise InvalidSourceError(f"filters.{error.setting}", error.reason) from None


def _filters_config(filters: SourceFilters) -> dict[str, Any]:
    """``source.config`` of a push or mcp source: its filters, if it has any."""
    return {} if filters.empty else {"filters": filters.to_json()}


@contextmanager
def _mcp_checks() -> Generator[None]:
    """Turn a refused setting of an mcp source (:mod:`memini.executors.source`) into
    :class:`InvalidSourceError`."""
    try:
        yield
    except McpConfigError as error:
        raise InvalidSourceError(error.setting, error.reason) from None


def _new_recipe(toml: str | None, name: str | None) -> "tuple[RecipeRef, Recipe]":
    """The recipe an mcp source is given: its own text, or a bundled one by name."""
    if (toml is None) == (name is None):
        raise InvalidSourceError(
            "recipe",
            "a recipe file's text, or the name of a recipe that ships with Memini: one of them",
        )
    return inline_recipe(toml) if toml is not None else bundled_recipe(name or "")


def check_schedule(schedule: timedelta | None) -> timedelta | None:
    """``schedule`` if it is from :data:`MIN_SCHEDULE` to :data:`MAX_SCHEDULE`, or ``None`` (only
    when triggered)."""
    if schedule is not None and not MIN_SCHEDULE <= schedule <= MAX_SCHEDULE:
        raise InvalidSourceError("schedule", "from 1 minute to 366 days, or none")
    return schedule


def _check_name(name: str) -> str:
    try:
        return check_handle("source", name)
    except ValueError as error:
        raise InvalidSourceError("name", str(error)) from None


def _check_choice[T: str](setting: str, value: T, choices: tuple[T, ...]) -> T:
    if value not in choices:
        raise InvalidSourceError(setting, f"one of {', '.join(choices)}")
    return value


def _check_retention(days: int | None) -> int | None:
    if days is not None and (isinstance(days, bool) or not 1 <= days <= MAX_RETENTION_DAYS):
        raise InvalidSourceError(
            "retention_days", f"a number of days from 1 to {MAX_RETENTION_DAYS}, or none"
        )
    return days


def _check_flag(setting: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise InvalidSourceError(setting, "true or false")
    return value


# --- Values --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class NewGitSource:
    """What :func:`create_git_source` adds. See the module for each setting."""

    name: str
    location: str
    refs: Sequence[str]
    schedule: timedelta | None = None
    include: Sequence[str] = ()
    exclude: Sequence[str] = ()
    builtin_excludes: bool = True
    visibility: Visibility = "private"
    retention_days: int | None = None
    llm_policy: LlmPolicy = "local"


@dataclass(frozen=True, slots=True, kw_only=True)
class NewPushSource:
    """What :func:`create_push_source` adds. See the module for each setting."""

    name: str
    visibility: Visibility = "private"
    retention_days: int | None = None
    llm_policy: LlmPolicy = "local"
    filters: SourceFilters = field(default_factory=SourceFilters)
    """Which records, and messages of threads, it stores (:mod:`memini.filters`)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class NewMcpSource:
    """What :func:`create_mcp_source` adds. See the module, and
    :mod:`memini.executors.source` for each setting."""

    name: str
    endpoint: Endpoint
    """A URL (:class:`~memini.executors.source.HttpEndpoint`) or a stdio command
    (:class:`~memini.executors.source.StdioEndpoint`)."""
    streams: Sequence[str]
    """Stream keys of the recipe: one stream each."""
    recipe_toml: str | None = None
    """The recipe the source holds: a recipe file's text. Exactly one of this and
    ``recipe_name``."""
    recipe_name: str | None = None
    """A recipe that ships with Memini, by name."""
    schedule: timedelta | None = None
    window: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    """Overrides of the recipe's ``[window]``: durations, or ``None`` for none."""
    visibility: Visibility = "private"
    retention_days: int | None = None
    llm_policy: LlmPolicy = "local"
    filters: SourceFilters = field(default_factory=SourceFilters)


class Unset(enum.Enum):
    """The type of :data:`UNSET`."""

    UNSET = "unset"


UNSET: Final = Unset.UNSET
"""A setting that :func:`update_source` leaves as it is; ``None`` means "none" where allowed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceUpdate:
    """What :func:`update_source` changes; every setting left :data:`UNSET` stays as it is.

    ``refs`` (git), ``streams`` (mcp), ``include`` and ``exclude`` replace the whole list,
    ``filters`` every rule of a push or mcp source (an empty :class:`~memini.filters.
    SourceFilters` clears them), and ``window`` every override of an mcp source. ``schedule`` and
    ``retention_days`` take ``None`` for none. A git location cannot change; an mcp endpoint and
    recipe can (``recipe_toml`` or ``recipe_name``).
    """

    name: str | Unset = UNSET
    refs: Sequence[str] | Unset = UNSET
    schedule: timedelta | Unset | None = UNSET
    include: Sequence[str] | Unset = UNSET
    exclude: Sequence[str] | Unset = UNSET
    builtin_excludes: bool | Unset = UNSET
    visibility: Visibility | Unset = UNSET
    retention_days: int | Unset | None = UNSET
    llm_policy: LlmPolicy | Unset = UNSET
    filters: SourceFilters | Unset = UNSET
    streams: Sequence[str] | Unset = UNSET
    endpoint: Endpoint | Unset = UNSET
    recipe_toml: str | Unset = UNSET
    recipe_name: str | Unset = UNSET
    window: Mapping[str, str | None] | Unset = UNSET


@dataclass(frozen=True, slots=True)
class Source:
    """A source with its settings and the keys of its streams."""

    id: UUID
    workspace_id: UUID
    workspace: str
    """The name of the workspace."""
    owner_id: UUID
    name: str
    kind: SourceKind
    git: GitConfig | None
    """The configuration of a repository; ``None`` for other kinds."""
    refs: tuple[str, ...]
    """The keys of the source's streams, sorted: for git, the tracked refs."""
    schedule: timedelta | None
    """How often its streams sync; ``None``: only when triggered."""
    visibility: Visibility
    retention_days: int | None
    llm_policy: LlmPolicy
    created_at: datetime
    updated_at: datetime
    filters: SourceFilters | None = None
    """The filter rules of a push or mcp source (:mod:`memini.filters`); ``None`` for git."""
    mcp: McpConfig | None = None
    """The configuration of an mcp source: endpoint, recipe, window; ``None`` for other kinds."""


@dataclass(frozen=True, slots=True)
class RunBrief:
    """The newest run of a stream."""

    id: UUID
    kind: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class JobBrief:
    """The sync job of a stream that is queued or running."""

    id: UUID
    kind: str
    status: PendingJobStatus
    attempts: int
    run_after: datetime
    """Not started before this time; later than its creation after a failed attempt."""
    last_error: str | None
    """Why the last failed attempt failed, with credentials in URLs masked."""


@dataclass(frozen=True, slots=True)
class Stream:
    """A stream of a source and how current it is (the definitions are in :mod:`memini.status`)."""

    id: UUID
    key: str
    schedule: timedelta | None
    next_run_at: datetime | None
    """When the scheduler next enqueues a sync; ``None`` with a schedule: at its next look."""
    checked_until: datetime | None
    lag_seconds: float | None
    current: bool | None
    last_run: RunBrief | None
    pending_job: JobBrief | None
    missing: bool = False
    """What it follows no longer exists at the source, e.g. a deleted branch
    (:mod:`memini.status`)."""


@dataclass(frozen=True, slots=True)
class SyncJob:
    """The sync job of one stream that :func:`trigger_sync` enqueued, or found queued or running."""

    stream: str
    """The stream's key."""
    job_id: UUID
    created: bool
    """False when a sync of the stream was already queued or running: that one is returned."""


# --- Reading -------------------------------------------------------------------------------------

_SOURCES: Final = sql.SQL("""
SELECT s.id, s.workspace_id, w.name, s.owner_id, s.name, s.kind, s.config, s.visibility,
       s.retention_days, s.llm_policy, s.created_at, s.updated_at,
       coalesce(array_agg(st.key ORDER BY st.key) FILTER (WHERE st.id IS NOT NULL), '{{}}'),
       min(st.schedule)
FROM source s
JOIN workspace w ON w.id = s.workspace_id
LEFT JOIN stream st ON st.source_id = s.id AND {stream_condition}
WHERE {condition}
GROUP BY s.id, w.name
ORDER BY s.name
""")


def _source(row: Sequence[Any]) -> Source:
    (
        source_id,
        workspace_id,
        workspace,
        owner_id,
        name,
        kind,
        config,
        visibility,
        retention_days,
        llm_policy,
        created_at,
        updated_at,
        refs,
        schedule,
    ) = row
    return Source(
        id=source_id,
        workspace_id=workspace_id,
        workspace=workspace,
        owner_id=owner_id,
        name=name,
        kind=kind,
        git=GitConfig.from_json(config) if kind == "repository" else None,
        refs=tuple(refs),
        schedule=schedule,
        visibility=visibility,
        retention_days=retention_days,
        llm_policy=llm_policy,
        created_at=created_at,
        updated_at=updated_at,
        filters=None if kind == "repository" else SourceFilters.from_json(config.get("filters")),
        mcp=McpConfig.from_json(config) if kind == "mcp" else None,
    )


def _columns(alias: str, *, source: str = "source_id") -> dict[str, sql.Composable]:
    return {
        "workspace_id": sql.Identifier(alias, "workspace_id"),
        "owner_id": sql.Identifier(alias, "owner_id"),
        "source_id": sql.Identifier(alias, source),
    }


async def _find_sources(
    conn: Conn,
    access: ReadFilter | WriteAccess,
    condition: sql.Composable,
    params: Sequence[Any],
) -> list[Source]:
    query = _SOURCES.format(
        stream_condition=access.where(**_columns("st")),
        condition=sql.SQL(" AND ").join([condition, access.where(**_columns("s", source="id"))]),
    )
    cur = await conn.execute(query, params)
    return [_source(row) for row in await cur.fetchall()]


async def list_sources(conn: Conn, access: ReadFilter, workspace: str) -> list[Source]:
    """The sources of the workspace ``workspace`` that ``access`` may read, by name.

    Raises:
        NotFoundError: no such workspace that ``access`` may read.
    """
    found = await get_workspace(conn, access, workspace)
    if found is None:
        raise NotFoundError("workspace")
    return await _find_sources(conn, access, sql.SQL("s.workspace_id = %s"), (found.id,))


async def get_source(conn: Conn, access: ReadFilter, workspace: str, name: str) -> Source:
    """The source ``name`` of the workspace ``workspace``.

    Raises:
        NotFoundError: no such workspace or source that ``access`` may read.
    """
    found = await get_workspace(conn, access, workspace)
    if found is None:
        raise NotFoundError("workspace")
    sources = await _find_sources(
        conn, access, sql.SQL("s.workspace_id = %s AND s.name = %s"), (found.id, name)
    )
    if not sources:
        raise NotFoundError("source")
    return sources[0]


_STREAMS: Final = sql.SQL("""
SELECT st.id, st.key, st.schedule, st.next_run_at, st.checked_until,
       extract(epoch FROM now() - st.checked_until)::float8,
       r.id, r.kind, r.status, r.started_at, r.finished_at, r.error,
       j.id, j.kind, j.status, j.attempts, j.run_after, j.last_error,
       coalesce(st.cursor @> '{{"missing": true}}'::jsonb, false)
FROM stream st
LEFT JOIN LATERAL (
    SELECT r.id, r.kind, r.status, r.started_at, r.finished_at, r.error
    FROM run r
    WHERE r.stream_id = st.id AND {run_condition}
    ORDER BY r.started_at DESC, r.id DESC
    LIMIT 1
) r ON TRUE
LEFT JOIN LATERAL (
    SELECT j.id, j.kind, j.status, j.attempts, j.run_after, j.last_error
    FROM job j
    WHERE j.source_id = st.source_id AND j.stream_id = st.id
      AND j.status IN ('queued', 'running') AND {job_condition}
    ORDER BY j.created_at DESC, j.id DESC
    LIMIT 1
) j ON TRUE
WHERE st.source_id = %s AND {stream_condition}
ORDER BY st.key
""")


def _stream(row: Sequence[Any]) -> Stream:
    (stream_id, key, schedule, next_run_at, checked_until, lag) = row[:6]
    run = row[6:12]
    job = row[12:18]
    schedule_seconds = None if schedule is None else schedule.total_seconds()
    return Stream(
        id=stream_id,
        key=key,
        schedule=schedule,
        next_run_at=next_run_at,
        checked_until=checked_until,
        lag_seconds=lag,
        current=stream_is_current(schedule_seconds, lag),
        last_run=None if run[0] is None else RunBrief(*run),
        pending_job=None if job[0] is None else JobBrief(*job),
        missing=row[18],
    )


async def list_streams(conn: Conn, access: ReadFilter, workspace: str, name: str) -> list[Stream]:
    """The streams of a source, by key, with their lag, their newest run and their queued or
    running sync job.

    Raises:
        NotFoundError: no such workspace or source that ``access`` may read.
    """
    source = await get_source(conn, access, workspace, name)
    query = _STREAMS.format(
        run_condition=access.where(**_columns("r")),
        job_condition=access.where(**_columns("j")),
        stream_condition=access.where(**_columns("st")),
    )
    cur = await conn.execute(query, (source.id,))
    return [_stream(row) for row in await cur.fetchall()]


# --- Writing -------------------------------------------------------------------------------------


async def _writable_source(conn: Conn, access: WriteAccess, workspace: str, name: str) -> Source:
    """The source ``name`` that ``access`` may change, its row locked until the transaction ends.

    FOR NO KEY UPDATE, not FOR UPDATE: it lets a concurrent insert that refers to the source (a job
    the scheduler enqueues, a run) take its FOR KEY SHARE lock and commit, instead of waiting for
    this transaction while holding a stream row it wants to delete, which would deadlock.
    """
    found = await get_writable_workspace(conn, access, workspace)
    lock = sql.SQL(
        "SELECT s.id FROM source s WHERE s.workspace_id = %s AND s.name = %s AND {}"
        " FOR NO KEY UPDATE"
    ).format(access.where(**_columns("s", source="id")))
    cur = await conn.execute(lock, (found.id, name))
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("source")
    return await _reload(conn, access, row[0])


async def _reload(conn: Conn, access: WriteAccess, source_id: UUID) -> Source:
    sources = await _find_sources(conn, access, sql.SQL("s.id = %s"), (source_id,))
    if not sources:  # the caller found or created it in this transaction
        raise RuntimeError(f"source {source_id} vanished")
    return sources[0]


async def _add_streams(
    conn: Conn,
    source: UUID,
    workspace_id: UUID,
    owner_id: UUID,
    keys: Sequence[str],
    schedule: timedelta | None,
) -> None:
    # next_run_at stays NULL: a scheduled stream syncs at the scheduler's next look.
    await conn.execute(
        "INSERT INTO stream (workspace_id, owner_id, source_id, key, schedule)"
        " SELECT %s, %s, %s, key, %s::interval FROM unnest(%s::text[]) AS key"
        " ON CONFLICT (source_id, key) DO NOTHING",
        (workspace_id, owner_id, source, schedule, list(keys)),
    )


async def create_git_source(
    conn: Conn, access: WriteAccess, workspace: str, new: NewGitSource, *, options: GitOptions
) -> Source:
    """Add a repository and one stream per tracked ref to the workspace ``workspace``.

    The location is checked (:func:`check_location`), not fetched. The source's mirror is created
    by its first sync.

    Raises:
        InvalidSourceError: a setting is refused; nothing was written.
        NotFoundError: no such workspace that ``access`` may change.
        SourceExistsError: the workspace has a source of that name.
    """
    name = _check_name(new.name)
    config = GitConfig(
        location=check_location(new.location, options),
        include=check_patterns("include_paths", new.include),
        exclude=check_patterns("exclude_paths", new.exclude),
        builtin_excludes=_check_flag("builtin_excludes", new.builtin_excludes),
    )
    refs = check_refs(new.refs)
    schedule = check_schedule(new.schedule)
    visibility = _check_choice("visibility", new.visibility, get_args(Visibility))
    retention_days = _check_retention(new.retention_days)
    llm_policy = _check_choice("llm_policy", new.llm_policy, get_args(LlmPolicy))
    try:
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            cur = await conn.execute(
                "INSERT INTO source (workspace_id, owner_id, name, kind, config, visibility,"
                " retention_days, llm_policy) VALUES (%s, %s, %s, 'repository', %s, %s, %s, %s)"
                " RETURNING id",
                (
                    found.id,
                    access.owner_id,
                    name,
                    Jsonb(config.to_json()),
                    visibility,
                    retention_days,
                    llm_policy,
                ),
            )
            row = await cur.fetchone()
            if row is None:  # INSERT ... RETURNING always returns the row
                raise RuntimeError("INSERT INTO source returned no row")
            source_id: UUID = row[0]
            await _add_streams(conn, source_id, found.id, access.owner_id, refs, schedule)
            return await _reload(conn, access, source_id)
    except errors.UniqueViolation as error:
        if error.diag.constraint_name == _SOURCE_NAME_KEY:
            raise SourceExistsError(f"the workspace has a source named {name}") from None
        raise


async def create_push_source(
    conn: Conn, access: WriteAccess, workspace: str, new: NewPushSource
) -> Source:
    """Add a push source to the workspace ``workspace``: no location, refs or schedule, and no
    streams until a batch of records names one (:func:`memini.ingest.ingest`).

    Raises:
        InvalidSourceError: a setting is refused; nothing was written.
        NotFoundError: no such workspace that ``access`` may change.
        SourceExistsError: the workspace has a source of that name.
    """
    name = _check_name(new.name)
    visibility = _check_choice("visibility", new.visibility, get_args(Visibility))
    retention_days = _check_retention(new.retention_days)
    llm_policy = _check_choice("llm_policy", new.llm_policy, get_args(LlmPolicy))
    filters = check_filters_setting(new.filters)
    try:
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            cur = await conn.execute(
                "INSERT INTO source (workspace_id, owner_id, name, kind, config, visibility,"
                " retention_days, llm_policy) VALUES (%s, %s, %s, 'push', %s, %s, %s, %s)"
                " RETURNING id",
                (
                    found.id,
                    access.owner_id,
                    name,
                    Jsonb(_filters_config(filters)),
                    visibility,
                    retention_days,
                    llm_policy,
                ),
            )
            row = await cur.fetchone()
            if row is None:  # INSERT ... RETURNING always returns the row
                raise RuntimeError("INSERT INTO source returned no row")
            return await _reload(conn, access, row[0])
    except errors.UniqueViolation as error:
        if error.diag.constraint_name == _SOURCE_NAME_KEY:
            raise SourceExistsError(f"the workspace has a source named {name}") from None
        raise


async def create_mcp_source(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    new: NewMcpSource,
    *,
    policy: EgressPolicy,
) -> Source:
    """Add an mcp source and one stream per stream key to the workspace ``workspace``.

    The endpoint is checked against ``policy`` (the ``mcp_*`` settings), not connected to; the
    recipe is checked and its parser looked up in this build; each stream key must match the
    recipe's ``stream.key_pattern``, and the window overrides must be valid with its ``[window]``.

    Raises:
        InvalidSourceError: a setting is refused; nothing was written.
        NotFoundError: no such workspace that ``access`` may change.
        SourceExistsError: the workspace has a source of that name.
    """
    name = _check_name(new.name)
    with _mcp_checks():
        endpoint = check_endpoint(new.endpoint, policy)
        ref, recipe = _new_recipe(new.recipe_toml, new.recipe_name)
        keys = check_stream_keys(new.streams, recipe)
        window = check_window(new.window, recipe)
    schedule = check_schedule(new.schedule)
    visibility = _check_choice("visibility", new.visibility, get_args(Visibility))
    retention_days = _check_retention(new.retention_days)
    llm_policy = _check_choice("llm_policy", new.llm_policy, get_args(LlmPolicy))
    filters = check_filters_setting(new.filters)
    config = McpConfig(endpoint=endpoint, recipe=ref, window=window).to_json()
    config |= _filters_config(filters)
    try:
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            cur = await conn.execute(
                "INSERT INTO source (workspace_id, owner_id, name, kind, config, visibility,"
                " retention_days, llm_policy) VALUES (%s, %s, %s, 'mcp', %s, %s, %s, %s)"
                " RETURNING id",
                (
                    found.id,
                    access.owner_id,
                    name,
                    Jsonb(config),
                    visibility,
                    retention_days,
                    llm_policy,
                ),
            )
            row = await cur.fetchone()
            if row is None:  # INSERT ... RETURNING always returns the row
                raise RuntimeError("INSERT INTO source returned no row")
            source_id: UUID = row[0]
            await _add_streams(conn, source_id, found.id, access.owner_id, keys, schedule)
            return await _reload(conn, access, source_id)
    except errors.UniqueViolation as error:
        if error.diag.constraint_name == _SOURCE_NAME_KEY:
            raise SourceExistsError(f"the workspace has a source named {name}") from None
        raise


def _mcp_changes(
    source: Source,
    changes: SourceUpdate,
    filters: SourceFilters | Unset,
    policy: EgressPolicy,
) -> tuple[dict[str, Any] | None, tuple[str, ...] | Unset]:
    """The new ``config`` of an mcp source, if a setting of it changes, and its new stream keys.

    A new recipe is checked with the source's stream keys and window as they will be."""
    mcp = source.mcp
    recipe_changed = not (
        isinstance(changes.recipe_toml, Unset) and isinstance(changes.recipe_name, Unset)
    )
    touched = recipe_changed or not (
        isinstance(changes.endpoint, Unset)
        and isinstance(changes.window, Unset)
        and isinstance(changes.streams, Unset)
        and isinstance(filters, Unset)
    )
    if mcp is None or not touched:
        return None, UNSET
    with _mcp_checks():
        endpoint = mcp.endpoint
        if not isinstance(changes.endpoint, Unset):
            endpoint = check_endpoint(changes.endpoint, policy)
        ref = mcp.recipe
        recipe: Recipe | None = None
        if recipe_changed:
            ref, recipe = _new_recipe(
                None if isinstance(changes.recipe_toml, Unset) else changes.recipe_toml,
                None if isinstance(changes.recipe_name, Unset) else changes.recipe_name,
            )
        elif not (isinstance(changes.window, Unset) and isinstance(changes.streams, Unset)):
            if ref is None:
                raise McpConfigError("recipe", "the source holds no recipe; set one")
            recipe = load_recipe_ref(ref)
        window = dict(mcp.window)
        keys: tuple[str, ...] | Unset = UNSET
        if recipe is not None:
            if not isinstance(changes.window, Unset):
                window = check_window(changes.window, recipe)
            elif recipe_changed:
                window = check_window(window, recipe)
            if not isinstance(changes.streams, Unset):
                keys = check_stream_keys(changes.streams, recipe)
            elif recipe_changed:
                check_stream_keys(source.refs, recipe)
    config = McpConfig(endpoint=endpoint, recipe=ref, window=window).to_json()
    config |= _filters_config(
        (source.filters or SourceFilters()) if isinstance(filters, Unset) else filters
    )
    return config, keys


async def update_source(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    name: str,
    changes: SourceUpdate,
    *,
    policy: EgressPolicy | None = None,
) -> Source:
    """Change the settings of a source; see :class:`SourceUpdate`. Returns the source as changed.

    ``policy`` checks a new endpoint of an mcp source; without it, none is allowed.

    A new schedule applies to every stream and sets their ``next_run_at`` to NULL; streams already
    on it are left alone. New refs get streams on the source's schedule; streams of refs no longer
    listed are deleted with their runs and jobs, and the records they stored keep no stream. A
    changed ``retention_days`` applies at once: the scheduler passes retention over the source at
    its next look (:mod:`memini.removal`).

    Raises:
        InvalidSourceError: a setting is refused, or does not apply to the source's kind; nothing
            was written.
        NotFoundError: no such workspace or source that ``access`` may change.
        SourceExistsError: ``name`` is changed to that of another source of the workspace.
    """
    new_name = UNSET if isinstance(changes.name, Unset) else _check_name(changes.name)
    refs = UNSET if isinstance(changes.refs, Unset) else check_refs(changes.refs)
    schedule = UNSET if isinstance(changes.schedule, Unset) else check_schedule(changes.schedule)
    include = (
        UNSET
        if isinstance(changes.include, Unset)
        else check_patterns("include_paths", changes.include)
    )
    exclude = (
        UNSET
        if isinstance(changes.exclude, Unset)
        else check_patterns("exclude_paths", changes.exclude)
    )
    builtin_excludes = (
        UNSET
        if isinstance(changes.builtin_excludes, Unset)
        else _check_flag("builtin_excludes", changes.builtin_excludes)
    )
    rules_changed = not (
        isinstance(include, Unset)
        and isinstance(exclude, Unset)
        and isinstance(builtin_excludes, Unset)
    )
    visibility = (
        UNSET
        if isinstance(changes.visibility, Unset)
        else _check_choice("visibility", changes.visibility, get_args(Visibility))
    )
    retention_days = (
        UNSET
        if isinstance(changes.retention_days, Unset)
        else _check_retention(changes.retention_days)
    )
    llm_policy = (
        UNSET
        if isinstance(changes.llm_policy, Unset)
        else _check_choice("llm_policy", changes.llm_policy, get_args(LlmPolicy))
    )
    filters = (
        UNSET if isinstance(changes.filters, Unset) else check_filters_setting(changes.filters)
    )
    try:
        async with conn.transaction():
            source = await _writable_source(conn, access, workspace, name)
            git = source.git
            if git is None and (not isinstance(refs, Unset) or rules_changed):
                raise InvalidSourceError(
                    "refs" if not isinstance(refs, Unset) else "include",
                    f"refs and path rules are settings of repositories, not of {source.kind}",
                )
            mcp_only = [
                setting
                for setting in ("streams", "endpoint", "recipe_toml", "recipe_name", "window")
                if not isinstance(getattr(changes, setting), Unset)
            ]
            if source.kind != "mcp" and mcp_only:
                raise InvalidSourceError(
                    mcp_only[0].removesuffix("_toml").removesuffix("_name"),
                    "streams, endpoint, recipe and window are settings of mcp sources, not of"
                    f" {source.kind}",
                )
            mcp_config, mcp_keys = _mcp_changes(source, changes, filters, policy or EgressPolicy())
            if git is not None and not isinstance(filters, Unset):
                raise InvalidSourceError(
                    "filters",
                    "filters are settings of push and mcp sources; a repository selects files by"
                    " include and exclude",
                )
            if source.kind == "push" and schedule is not None and not isinstance(schedule, Unset):
                raise InvalidSourceError(
                    "schedule", "push sources are not synced by the service; clients send records"
                )
            assignments: list[sql.Composable] = [sql.SQL("updated_at = now()")]
            params: list[Any] = []

            def assign(column: str, value: object) -> None:
                assignments.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
                params.append(value)

            if not isinstance(new_name, Unset):
                assign("name", new_name)
            if git is not None and rules_changed:
                config = GitConfig(
                    location=git.location,
                    include=git.include if isinstance(include, Unset) else include,
                    exclude=git.exclude if isinstance(exclude, Unset) else exclude,
                    builtin_excludes=(
                        git.builtin_excludes
                        if isinstance(builtin_excludes, Unset)
                        else builtin_excludes
                    ),
                )
                assign("config", Jsonb(config.to_json()))
            if mcp_config is not None:
                assign("config", Jsonb(mcp_config))
            elif not isinstance(filters, Unset):
                # Only the filters of the configuration.
                assignments.append(sql.SQL("config = (config - 'filters') || %s"))
                params.append(Jsonb(_filters_config(filters)))
            if not isinstance(mcp_keys, Unset):
                refs = mcp_keys
            if not isinstance(visibility, Unset):
                assign("visibility", visibility)
            if not isinstance(retention_days, Unset):
                assign("retention_days", retention_days)
                # A changed retention applies at the scheduler's next look (0013).
                assign("retention_next_at", None)
            if not isinstance(llm_policy, Unset):
                assign("llm_policy", llm_policy)
            if len(assignments) > 1 or not (
                isinstance(refs, Unset) and isinstance(schedule, Unset)
            ):
                await conn.execute(
                    sql.SQL("UPDATE source SET {} WHERE id = %s").format(
                        sql.SQL(", ").join(assignments)
                    ),
                    (*params, source.id),
                )

            target_schedule = source.schedule if isinstance(schedule, Unset) else schedule
            if not isinstance(refs, Unset):
                await conn.execute(
                    "DELETE FROM stream WHERE source_id = %s AND key <> ALL(%s::text[])",
                    (source.id, list(refs)),
                )
            if not isinstance(schedule, Unset):
                # Code that changes the schedule of a stream sets next_run_at to NULL (0003).
                await conn.execute(
                    "UPDATE stream SET schedule = %s::interval, next_run_at = NULL"
                    " WHERE source_id = %s AND schedule IS DISTINCT FROM %s::interval",
                    (schedule, source.id, schedule),
                )
            if not isinstance(refs, Unset):
                await _add_streams(
                    conn, source.id, source.workspace_id, source.owner_id, refs, target_schedule
                )
            return await _reload(conn, access, source.id)
    except errors.UniqueViolation as error:
        if error.diag.constraint_name == _SOURCE_NAME_KEY:
            raise SourceExistsError(f"the workspace has a source named {new_name}") from None
        raise


async def delete_source(
    conn: Conn, access: WriteAccess, workspace: str, name: str, *, mirrors: MirrorManager
) -> Source:
    """Delete a source with its streams, runs, jobs and records, and its mirror if it has one.

    Takes the advisory lock of the source for the transaction (:func:`memini.jobs.source_lock_key`):
    no job of the source runs while the mirror is removed, and no worker claims one before the
    deletion commits, after which its jobs are gone. The mirror is removed before the commit;
    should the commit fail, the next sync of the source creates it again. Returns the source as it
    was.

    Raises:
        NotFoundError: no such workspace or source that ``access`` may change.
        SourceBusyError: a job of the source is running (or another writer holds its mirror);
            nothing was deleted. Try again when it is done.
    """
    async with conn.transaction():
        source = await _writable_source(conn, access, workspace, name)
        cur = await conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)", source_lock_key(source.id)
        )
        row = await cur.fetchone()
        if row is None or not row[0]:
            raise SourceBusyError(f"a job of source {source.name} is running")
        # The cascade to every record, chunk and symbol takes as long as the source is large, and
        # the mirror is removed inside the transaction: neither is cut by a request's time limits
        # (memini.db.limits). The database's own defaults still apply.
        await conn.execute(
            "SET LOCAL statement_timeout TO DEFAULT;"
            " SET LOCAL idle_in_transaction_session_timeout TO DEFAULT"
        )
        await conn.execute("DELETE FROM source WHERE id = %s", (source.id,))
        if source.kind == "repository":
            try:
                await mirrors.remove(source.id)
            except MirrorBusy:
                raise SourceBusyError(f"another writer holds the mirror of {source.name}") from None
        return source


async def trigger_sync(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    name: str,
    *,
    streams: Iterable[str] | None = None,
) -> list[SyncJob]:
    """Enqueue a sync of each stream of a source, or of the ``streams`` named by key.

    Each job has the stream's dedupe key (:func:`memini.jobs.stream_dedupe_key`), so a sync that is
    already queued or running, by hand or by the scheduler, is returned instead of a second one.
    The jobs are run by the worker that has a handler for the source kind's sync job; until one
    is registered they wait in the queue.

    Raises:
        NotFoundError: no such workspace, source or stream that ``access`` may change.
        NotSyncableError: the source's kind is not synced by the service.
    """
    async with conn.transaction():
        source = await _writable_source(conn, access, workspace, name)
        kind = SYNC_KINDS.get(source.kind)
        if kind is None:
            raise NotSyncableError(f"{source.kind} sources are not synced by the service")
        wanted = None if streams is None else sorted(set(streams))
        condition = sql.SQL("source_id = %s")
        params: list[Any] = [source.id]
        if wanted is not None:
            condition = sql.SQL("source_id = %s AND key = ANY(%s::text[])")
            params.append(wanted)
        cur = await conn.execute(
            sql.SQL("SELECT id, key, owner_id FROM stream WHERE {} ORDER BY key").format(condition),
            params,
        )
        rows = await cur.fetchall()
        if wanted is not None and len(rows) != len(wanted):
            raise NotFoundError("stream")
        jobs: list[SyncJob] = []
        for stream_id, key, owner_id in rows:
            enqueued = await enqueue(
                conn,
                workspace_id=source.workspace_id,
                owner_id=owner_id,
                kind=kind,
                source_id=source.id,
                stream_id=stream_id,
                dedupe_key=stream_dedupe_key(stream_id),
            )
            jobs.append(SyncJob(stream=key, job_id=enqueued.id, created=enqueued.created))
        return jobs
