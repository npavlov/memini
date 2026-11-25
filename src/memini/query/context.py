"""What a piece of code belongs to: the ``context`` every code hit, definition and ``read_source``
answer carries.

The catalog (:mod:`memini.catalog`; requirements, section 3) says which project and service a
file of a repository is in and which domains name it. Nothing of that is stored with the file:
:func:`code_contexts` computes it from the catalog's path masks when the answer is made
(:mod:`memini.search.catalog`), so a change of the catalog shows in the next answer.

The names in a context are handles the catalog checked (lowercase letters, digits, ``-`` and
``_``), never free text, and each can be passed back as a ``projects``, ``services`` or
``domains`` filter of search.
"""

from collections.abc import Sequence
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from memini.auth import ReadFilter
from memini.search.catalog import FileContext, load_code_maps

__all__ = ["CodeContext", "DomainRef", "FilePlace", "code_contexts"]

type Conn = AsyncConnection[TupleRow]

type FilePlace = tuple[UUID, UUID, str]
"""A file of a repository: its workspace, its git source and its path there."""


class DomainRef(BaseModel):
    """A domain or subdomain of the catalog."""

    project: str = Field(description="The project the domain is of.")
    domain: str = Field(description="The domain, or domain/subdomain.")


class CodeContext(BaseModel):
    """What a file of a repository belongs to, by the catalog as it is now."""

    project: str | None = Field(
        description="The project of the service the file is in; null for a file in no service."
    )
    service: str | None = Field(
        description="The service the file is in: its repository, or the nearest directory of it"
        " that is a service; null when none is."
    )
    domains: list[DomainRef] = Field(
        description="The domains and subdomains whose path masks match the file: several, one"
        " or none. A domain of another project may name the file."
    )

    @classmethod
    def of(cls, found: FileContext) -> "CodeContext":
        return cls(
            project=found.project,
            service=found.service,
            domains=[
                DomainRef(project=project, domain=domain) for project, domain in found.domains
            ],
        )


async def code_contexts(
    conn: Conn, access: ReadFilter, places: Sequence[FilePlace | None]
) -> list[CodeContext | None]:
    """The context of each of ``places``, in order; ``None`` stays ``None`` (a record that is no
    file of a repository has no context).

    Reads the catalog of the workspaces named, in one transaction of its own (a savepoint inside
    the caller's), so a connection that was idle is left idle. Without any place it reads
    nothing.
    """
    workspace_ids = {place[0] for place in places if place is not None}
    if not workspace_ids:
        return [None] * len(places)
    async with conn.transaction():
        maps = await load_code_maps(conn, access, workspace_ids)
    return [
        None if place is None else CodeContext.of(maps[place[0]].context(place[1], place[2]))
        for place in places
    ]
