"""The project and domains a push or mcp source, or one of its streams, gives the records pushed
without their own (requirements, section 3, **Decisions by project and domain**).

A default is the second of the ways an item comes by a project and domains
(:mod:`memini.decisions.context`): after what whoever pushed a record gave with it, before what a
person linked by hand. A stream's default comes before its source's, and a stream is named by its
key, so a default can be set before the stream's first batch. The names are checked against the
workspace's catalog when the default is set, and compared with it again whenever it is read.

Routes (:mod:`memini.api.decisions`): ``GET``, ``PUT`` and ``DELETE``
``/v1/workspaces/{workspace}/sources/{source}/catalog-default``; the writes need the ``ingest``
scope. Errors are :class:`~memini.decisions.DecisionError`: 404 ``not_found`` for a workspace or
source out of reach (or a git source: its code has the catalog's masks), 422 ``invalid_place`` or
``unknown_place`` for the names.
"""

from datetime import datetime
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import BaseModel, ConfigDict, Field

from memini.auth import ReadFilter, WriteAccess
from memini.decisions import context
from memini.decisions.model import Name
from memini.decisions.service import DecisionError, decision_errors
from memini.ingest.catalog import MAX_PLACE_DOMAINS
from memini.ingest.validate import MAX_STREAM_KEY_BYTES
from memini.search.catalog import load_code_maps
from memini.workspaces import NotFoundError, get_workspace, get_writable_workspace

__all__ = [
    "SourceDefaultBody",
    "SourceDefaultInfo",
    "SourceDefaults",
    "run_delete_source_default",
    "run_get_source_defaults",
    "run_put_source_default",
]

type Conn = AsyncConnection[TupleRow]


class SourceDefaultBody(BaseModel):
    """The body of ``PUT …/sources/{source}/catalog-default``."""

    model_config = ConfigDict(extra="forbid")

    stream: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_STREAM_KEY_BYTES,
        description="A stream of the source by its key; null: the whole source. A stream's"
        " default comes before its source's.",
    )
    project: Name = Field(description="The project of the workspace's catalog, by name.")
    domains: list[Name] = Field(
        default_factory=list[Name],
        max_length=MAX_PLACE_DOMAINS,
        description="Domains of the project: domain or domain/subdomain. None: the project only;"
        " the assign_domains model may then choose among its domains.",
    )


class SourceDefaultInfo(BaseModel):
    stream: str | None = Field(description="The stream's key; null for the whole source.")
    project: str
    domains: list[str]
    updated_at: datetime
    updated_by: UUID
    updated_token: UUID | None


class SourceDefaults(BaseModel):
    workspace: str
    source: str
    defaults: list[SourceDefaultInfo] = Field(
        description="The source's own first, then its streams' by key."
    )


_SOURCE: Final = sql.SQL(
    "SELECT s.id FROM source s WHERE s.workspace_id = %s AND s.name = %s"
    " AND s.kind IN ('push', 'mcp') AND {}"
)


async def _source(
    conn: Conn, access: ReadFilter | WriteAccess, workspace_id: UUID, name: str
) -> UUID:
    condition = access.where(
        workspace_id=sql.Identifier("s", "workspace_id"),
        owner_id=sql.Identifier("s", "owner_id"),
        source_id=sql.Identifier("s", "id"),
    )
    cur = await conn.execute(_SOURCE.format(condition), (workspace_id, name))
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("source")
    return row[0]


async def _defaults(
    conn: Conn, access: ReadFilter | WriteAccess, workspace: str, workspace_id: UUID, source: str
) -> SourceDefaults:
    source_id = await _source(conn, access, workspace_id, source)
    held = await context.list_source_defaults(
        conn, access, workspace_id=workspace_id, source_id=source_id
    )
    return SourceDefaults(
        workspace=workspace,
        source=source,
        defaults=[
            SourceDefaultInfo(
                stream=each.stream,
                project=each.project,
                domains=list(each.domains),
                updated_at=each.updated_at,
                updated_by=each.updated_by,
                updated_token=each.updated_token,
            )
            for each in held
        ],
    )


async def run_get_source_defaults(
    conn: Conn, access: ReadFilter, workspace: str, source: str
) -> SourceDefaults:
    """The defaults of ``source`` of ``workspace``."""
    with decision_errors():
        async with conn.transaction():
            found = await get_workspace(conn, access, workspace)
            if found is None:
                raise NotFoundError("workspace")
            return await _defaults(conn, access, found.name, found.id, source)


async def run_put_source_default(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    source: str,
    body: SourceDefaultBody,
    *,
    token_id: UUID | None = None,
) -> SourceDefaults:
    """Set the default of ``source`` or of one of its streams; the defaults after it."""
    with decision_errors():
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            source_id = await _source(conn, access, found.id, source)
            catalog = (await load_code_maps(conn, access, [found.id]))[found.id]
            places = context.resolve_places(
                context.CatalogNames.of(catalog), body.project, body.domains
            )
            await context.put_source_default(
                conn,
                access,
                workspace_id=found.id,
                source_id=source_id,
                stream=body.stream,
                project=body.project,
                domains=[domain for _, domain in places if domain is not None],
                token_id=token_id,
            )
            return await _defaults(conn, access, found.name, found.id, source)


async def run_delete_source_default(
    conn: Conn, access: WriteAccess, workspace: str, source: str, *, stream: str | None = None
) -> SourceDefaults:
    """Remove the default of ``source`` (``stream`` ``None``) or of one stream; 404
    ``not_found`` when there is none."""
    with decision_errors():
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            source_id = await _source(conn, access, found.id, source)
            removed = await context.delete_source_default(
                conn, access, workspace_id=found.id, source_id=source_id, stream=stream
            )
            if not removed:
                raise DecisionError(404, "not_found", "No such default.")
            return await _defaults(conn, access, found.name, found.id, source)
