"""The catalog's rows: reading projects whole, the single writes ``put_project`` is made of, and
what the index holds of the repositories the catalog names.

Tables ``project``, ``service``, ``domain`` and ``domain_code`` (migration 0018), and
``service_link`` (migration 0019). Every read
takes the caller's :class:`~memini.auth.ReadFilter` or :class:`~memini.auth.WriteAccess` and puts
it into the SQL that finds the rows; a workspace out of reach has no rows. The writes run inside
the transaction of :func:`memini.catalog.service.run_put_project`, on a project row it has locked,
and stamp each row they change with who changed it and when (:class:`Author`).

The files of a repository are its live file records (code and Markdown documents; requirements,
section 9, Code sync), each path once however many tracked refs hold it. A record's external id
is ``<full ref>:<path>``, and a ref name holds no colon, so the path is what follows the first
one.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter, WriteAccess
from memini.catalog.model import LinkKind
from memini.gitsync import read_pin

__all__ = [
    "SOURCE_ROOT_KEY",
    "Author",
    "Repository",
    "Stamp",
    "StoredCode",
    "StoredDomain",
    "StoredLink",
    "StoredProject",
    "StoredService",
    "bump_project",
    "check_service_places",
    "delete_code",
    "delete_domains",
    "delete_links",
    "delete_services",
    "ensure_project",
    "git_sources",
    "insert_code",
    "insert_domain",
    "insert_link",
    "insert_service",
    "read_links",
    "read_projects",
    "repositories",
    "services_linked_elsewhere",
    "services_named_elsewhere",
    "services_of_others",
    "source_files",
    "update_code",
    "update_domain",
    "update_link",
    "update_service",
]

type Conn = AsyncConnection[TupleRow]
type Access = ReadFilter | WriteAccess

SOURCE_ROOT_KEY: Final = "service_source_root_key"
"""The constraint by which a directory of a repository is one service (migration 0018)."""


@dataclass(frozen=True, slots=True)
class Author:
    """Who writes: the principal, and the token of the request if there is one."""

    owner_id: UUID
    token_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Stamp:
    """Who changed a row last, and when."""

    at: datetime
    by: UUID
    token: UUID | None


@dataclass(frozen=True, slots=True)
class StoredService:
    id: UUID
    name: str
    description: str
    source_id: UUID
    repository: str
    """The name of the repository."""
    root: str
    stamp: Stamp


@dataclass(frozen=True, slots=True)
class StoredDomain:
    id: UUID
    parent_id: UUID | None
    name: str
    title: str
    description: str
    stamp: Stamp


@dataclass(frozen=True, slots=True)
class StoredCode:
    id: UUID
    domain_id: UUID
    service_id: UUID
    project: str
    """The project of the service."""
    service: str
    paths: tuple[str, ...]
    stamp: Stamp


@dataclass(frozen=True, slots=True)
class StoredLink:
    """A service that uses another."""

    id: UUID
    service_id: UUID
    """The service that uses."""
    target_id: UUID
    """The service it uses."""
    from_project_id: UUID
    from_project: str
    from_service: str
    to_project_id: UUID
    to_project: str
    to_service: str
    kind: LinkKind
    note: str
    paths: tuple[str, ...]
    """Read inside the root of the service that uses."""
    stamp: Stamp
    source_id: UUID
    """The repository of the service that uses."""
    repository: str
    root: str
    """The root of the service that uses."""


@dataclass(frozen=True, slots=True)
class StoredProject:
    """A project with everything in it."""

    id: UUID
    workspace_id: UUID
    name: str
    title: str
    description: str
    version: int
    stamp: Stamp
    services: tuple[StoredService, ...]
    domains: tuple[StoredDomain, ...]
    """Domains before their subdomains, each level by name."""
    code: tuple[StoredCode, ...]
    links: tuple[StoredLink, ...] = ()
    """The links of its services: what they use, by service and then by the service used."""

    def addresses(self) -> dict[UUID, tuple[str, ...]]:
        """Each domain's address by its id: ``(domain,)`` or ``(domain, subdomain)``."""
        names = {domain.id: domain.name for domain in self.domains}
        return {
            domain.id: (domain.name,)
            if domain.parent_id is None
            else (names[domain.parent_id], domain.name)
            for domain in self.domains
        }


@dataclass(frozen=True, slots=True)
class Repository:
    """A repository with its tracked refs."""

    id: UUID
    name: str
    refs: tuple[tuple[str, str | None], ...]
    """``(full ref, pinned commit)``, by ref; no commit before the ref's first complete sync, or
    after a sync that failed part way."""


def _where(access: Access, alias: str, *, source: str | None = None) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
        source_id=None if source is None else sql.Identifier(alias, source),
    )


# --- Reading projects ----------------------------------------------------------------------------

_PROJECTS: Final = sql.SQL(
    "SELECT p.id, p.name, p.title, p.description, p.version,"
    " p.updated_at, p.updated_by, p.updated_token"
    " FROM project p WHERE p.workspace_id = %(workspace)s AND {access}{named}"
    " ORDER BY p.name{lock}"
)

_SERVICES: Final = sql.SQL(
    "SELECT s.project_id, s.id, s.name, s.description, s.source_id, src.name, s.root,"
    " s.updated_at, s.updated_by, s.updated_token"
    " FROM service s JOIN source src ON src.id = s.source_id"
    " WHERE s.project_id = ANY(%s) AND {access} ORDER BY s.name"
)

_DOMAINS: Final = sql.SQL(
    "SELECT d.project_id, d.id, d.parent_id, d.name, d.title, d.description,"
    " d.updated_at, d.updated_by, d.updated_token"
    " FROM domain d WHERE d.project_id = ANY(%s) AND {access} ORDER BY d.level, d.name"
)

_CODE: Final = sql.SQL(
    "SELECT d.project_id, c.id, c.domain_id, c.service_id, sp.name, s.name, c.paths,"
    " c.updated_at, c.updated_by, c.updated_token"
    " FROM domain_code c JOIN domain d ON d.id = c.domain_id"
    " JOIN service s ON s.id = c.service_id JOIN project sp ON sp.id = s.project_id"
    " WHERE d.project_id = ANY(%s) AND {access} ORDER BY sp.name, s.name"
)


_LINKS: Final = sql.SQL(
    "SELECT l.id, l.service_id, l.target_id, fs.project_id, fp.name, fs.name, ts.project_id,"
    " tp.name, ts.name, l.kind, l.note, l.paths, l.updated_at, l.updated_by, l.updated_token,"
    " fs.source_id, src.name, fs.root"
    " FROM service_link l"
    " JOIN service fs ON fs.id = l.service_id JOIN project fp ON fp.id = fs.project_id"
    " JOIN source src ON src.id = fs.source_id"
    " JOIN service ts ON ts.id = l.target_id JOIN project tp ON tp.id = ts.project_id"
    " WHERE l.workspace_id = %(workspace)s AND {where} AND {access}"
    " ORDER BY fp.name, fs.name, tp.name, ts.name"
)


def _link(row: Sequence[Any]) -> StoredLink:
    return StoredLink(
        id=row[0],
        service_id=row[1],
        target_id=row[2],
        from_project_id=row[3],
        from_project=row[4],
        from_service=row[5],
        to_project_id=row[6],
        to_project=row[7],
        to_service=row[8],
        kind=row[9],
        note=row[10],
        paths=tuple(row[11]),
        stamp=Stamp(*row[12:15]),
        source_id=row[15],
        repository=row[16],
        root=row[17],
    )


async def read_links(
    conn: Conn, access: Access, workspace_id: UUID, project_ids: Collection[UUID]
) -> list[StoredLink]:
    """The links of the workspace that ``access`` reaches whose service that uses, or service
    used, is of one of ``project_ids``: by the project and service that uses, then the project
    and service used."""
    if not project_ids:
        return []
    query = _LINKS.format(
        where=sql.SQL("(fs.project_id = ANY(%(ids)s) OR ts.project_id = ANY(%(ids)s))"),
        access=_where(access, "l"),
    )
    cur = await conn.execute(query, {"workspace": workspace_id, "ids": list(project_ids)})
    return [_link(row) for row in await cur.fetchall()]


async def read_projects(
    conn: Conn,
    access: Access,
    workspace_id: UUID,
    *,
    name: str | None = None,
    lock: bool = False,
) -> list[StoredProject]:
    """The projects of the workspace that ``access`` reaches, by name, each with its services,
    domains and code; only the project ``name`` when one is given. ``lock`` holds the project
    rows until the transaction ends (``FOR UPDATE``), so that two writers of a project take
    turns."""
    query = _PROJECTS.format(
        access=_where(access, "p"),
        named=sql.SQL("" if name is None else " AND p.name = %(name)s"),
        lock=sql.SQL(" FOR UPDATE" if lock else ""),
    )
    params: dict[str, Any] = {"workspace": workspace_id}
    if name is not None:
        params["name"] = name
    cur = await conn.execute(query, params)
    rows = await cur.fetchall()
    if not rows:
        return []
    ids = [row[0] for row in rows]
    services: dict[UUID, list[StoredService]] = {each: [] for each in ids}
    domains: dict[UUID, list[StoredDomain]] = {each: [] for each in ids}
    code: dict[UUID, list[StoredCode]] = {each: [] for each in ids}
    links: dict[UUID, list[StoredLink]] = {each: [] for each in ids}

    cur = await conn.execute(_SERVICES.format(access=_where(access, "s")), (ids,))
    for row in await cur.fetchall():
        services[row[0]].append(
            StoredService(
                id=row[1],
                name=row[2],
                description=row[3],
                source_id=row[4],
                repository=row[5],
                root=row[6],
                stamp=Stamp(*row[7:10]),
            )
        )
    cur = await conn.execute(_DOMAINS.format(access=_where(access, "d")), (ids,))
    for row in await cur.fetchall():
        domains[row[0]].append(
            StoredDomain(
                id=row[1],
                parent_id=row[2],
                name=row[3],
                title=row[4],
                description=row[5],
                stamp=Stamp(*row[6:9]),
            )
        )
    cur = await conn.execute(_CODE.format(access=_where(access, "c")), (ids,))
    for row in await cur.fetchall():
        code[row[0]].append(
            StoredCode(
                id=row[1],
                domain_id=row[2],
                service_id=row[3],
                project=row[4],
                service=row[5],
                paths=tuple(row[6]),
                stamp=Stamp(*row[7:10]),
            )
        )
    query = _LINKS.format(where=sql.SQL("fs.project_id = ANY(%(ids)s)"), access=_where(access, "l"))
    cur = await conn.execute(query, {"workspace": workspace_id, "ids": ids})
    for row in await cur.fetchall():
        links[row[3]].append(_link(row))
    return [
        StoredProject(
            id=row[0],
            workspace_id=workspace_id,
            name=row[1],
            title=row[2],
            description=row[3],
            version=row[4],
            stamp=Stamp(*row[5:8]),
            services=tuple(services[row[0]]),
            domains=tuple(domains[row[0]]),
            code=tuple(code[row[0]]),
            links=tuple(links[row[0]]),
        )
        for row in rows
    ]


# --- Repositories and their files ----------------------------------------------------------------


async def git_sources(
    conn: Conn, access: Access, workspace_id: UUID, names: Collection[str]
) -> dict[str, UUID]:
    """The repositories of the workspace among ``names`` that ``access`` reaches, by name."""
    if not names:
        return {}
    query = sql.SQL(
        "SELECT s.name, s.id FROM source s"
        " WHERE s.workspace_id = %s AND s.kind = 'repository' AND s.name = ANY(%s) AND {}"
    ).format(_where(access, "s", source="id"))
    cur = await conn.execute(query, (workspace_id, sorted(names)))
    return {row[0]: row[1] for row in await cur.fetchall()}


async def repositories(conn: Conn, access: ReadFilter, workspace_id: UUID) -> list[Repository]:
    """Every repository of the workspace that ``access`` reads, by name, with its tracked refs
    and the commit each is pinned at."""
    query = sql.SQL(
        "SELECT s.id, s.name, st.key, st.cursor FROM source s"
        " LEFT JOIN stream st ON st.source_id = s.id AND {streams}"
        " WHERE s.workspace_id = %s AND s.kind = 'repository' AND {sources}"
        " ORDER BY s.name, st.key"
    ).format(
        streams=_where(access, "st", source="source_id"), sources=_where(access, "s", source="id")
    )
    cur = await conn.execute(query, (workspace_id,))
    names: dict[UUID, str] = {}
    refs: dict[UUID, list[tuple[str, str | None]]] = {}
    for source_id, name, ref, cursor in await cur.fetchall():
        names[source_id] = name
        held = refs.setdefault(source_id, [])
        if ref is not None:
            pin = read_pin(cursor)
            held.append((ref, None if pin is None or pin.partial else pin.commit))
    return [
        Repository(source_id, name, tuple(refs[source_id])) for source_id, name in names.items()
    ]


async def source_files(
    conn: Conn, access: ReadFilter, source_ids: Collection[UUID]
) -> dict[UUID, list[str]]:
    """The paths of the files the index holds of each repository of ``source_ids``, sorted; see
    the module. A source without files, or out of reach, has none."""
    found: dict[UUID, list[str]] = {each: [] for each in source_ids}
    if not found:
        return found
    query = sql.SQL(
        "SELECT DISTINCT r.source_id, substr(r.external_id, strpos(r.external_id, ':') + 1)"
        " FROM record r WHERE r.source_id = ANY(%s) AND r.kind IN ('code_file', 'document')"
        " AND r.deleted_at IS NULL AND {} ORDER BY 1, 2"
    ).format(_where(access, "r", source="source_id"))
    cur = await conn.execute(query, (sorted(found, key=lambda each: each.hex),))
    for source_id, path in await cur.fetchall():
        found[source_id].append(path)
    return found


# --- Writing -------------------------------------------------------------------------------------


async def ensure_project(conn: Conn, author: Author, workspace_id: UUID, name: str) -> None:
    """Make the row of the project ``name`` if the workspace has none: at version 0, which the
    write that follows in the same transaction raises to 1."""
    await conn.execute(
        "INSERT INTO project (workspace_id, owner_id, name, updated_by, updated_token)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (workspace_id, name) DO NOTHING",
        (workspace_id, author.owner_id, name, author.owner_id, author.token_id),
    )


async def bump_project(
    conn: Conn, author: Author, project_id: UUID, *, title: str, description: str
) -> int:
    """Store the project's own fields, count the change and stamp it; its new version."""
    cur = await conn.execute(
        "UPDATE project SET title = %s, description = %s, version = version + 1,"
        " updated_at = now(), updated_by = %s, updated_token = %s WHERE id = %s"
        " RETURNING version",
        (title, description, author.owner_id, author.token_id, project_id),
    )
    row = await cur.fetchone()
    if row is None:  # the caller holds its lock
        raise RuntimeError(f"project {project_id} vanished")
    version: int = row[0]
    return version


async def insert_service(
    conn: Conn,
    author: Author,
    project: StoredProject,
    *,
    name: str,
    description: str,
    source_id: UUID,
    root: str,
) -> UUID:
    cur = await conn.execute(
        "INSERT INTO service (workspace_id, owner_id, project_id, name, description, source_id,"
        " root, updated_by, updated_token) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING id",
        (
            project.workspace_id,
            author.owner_id,
            project.id,
            name,
            description,
            source_id,
            root,
            author.owner_id,
            author.token_id,
        ),
    )
    return await _returned_id(cur)


async def update_service(
    conn: Conn, author: Author, service_id: UUID, *, description: str, source_id: UUID, root: str
) -> None:
    await conn.execute(
        "UPDATE service SET description = %s, source_id = %s, root = %s, updated_at = now(),"
        " updated_by = %s, updated_token = %s WHERE id = %s",
        (description, source_id, root, author.owner_id, author.token_id, service_id),
    )


async def delete_services(conn: Conn, service_ids: Sequence[UUID]) -> None:
    """Delete the services, with the code entries that name them."""
    if service_ids:
        await conn.execute("DELETE FROM service WHERE id = ANY(%s)", (list(service_ids),))


async def services_named_elsewhere(
    conn: Conn, project_id: UUID, service_ids: Sequence[UUID]
) -> bool:
    """Whether a domain of another project names code in one of the project's services."""
    if not service_ids:
        return False
    cur = await conn.execute(
        "SELECT 1 FROM domain_code c JOIN domain d ON d.id = c.domain_id"
        " WHERE c.service_id = ANY(%s) AND d.project_id <> %s LIMIT 1",
        (list(service_ids), project_id),
    )
    return await cur.fetchone() is not None


async def services_linked_elsewhere(
    conn: Conn, project_id: UUID, service_ids: Sequence[UUID]
) -> bool:
    """Whether a service of another project declares it uses one of the project's services."""
    if not service_ids:
        return False
    cur = await conn.execute(
        "SELECT 1 FROM service_link l JOIN service s ON s.id = l.service_id"
        " WHERE l.target_id = ANY(%s) AND s.project_id <> %s LIMIT 1",
        (list(service_ids), project_id),
    )
    return await cur.fetchone() is not None


async def services_of_others(
    conn: Conn, access: WriteAccess, workspace_id: UUID, names: Collection[tuple[str, str]]
) -> dict[tuple[str, str], UUID]:
    """The services ``names`` (``(project, service)``) of the workspace, by name: those a domain
    or a link of another project may name."""
    if not names:
        return {}
    query = sql.SQL(
        "SELECT p.name, s.name, s.id FROM service s JOIN project p ON p.id = s.project_id"
        " JOIN unnest(%s::text[], %s::text[]) AS wanted (project, service)"
        " ON wanted.project = p.name AND wanted.service = s.name"
        " WHERE s.workspace_id = %s AND {}"
    ).format(_where(access, "s"))
    ordered = sorted(names)
    cur = await conn.execute(
        query, ([each[0] for each in ordered], [each[1] for each in ordered], workspace_id)
    )
    return {(row[0], row[1]): row[2] for row in await cur.fetchall()}


async def check_service_places(conn: Conn) -> None:
    """Have the database check now that no two services are the same directory of a repository
    (:data:`SOURCE_ROOT_KEY`, deferred to the end of the transaction otherwise).

    Raises:
        psycopg.errors.UniqueViolation: two are; its constraint is :data:`SOURCE_ROOT_KEY`.
    """
    constraint = sql.Identifier(SOURCE_ROOT_KEY)
    await conn.execute(sql.SQL("SET CONSTRAINTS {} IMMEDIATE").format(constraint))
    await conn.execute(sql.SQL("SET CONSTRAINTS {} DEFERRED").format(constraint))


async def insert_domain(
    conn: Conn,
    author: Author,
    project: StoredProject,
    *,
    parent_id: UUID | None,
    name: str,
    title: str,
    description: str,
) -> UUID:
    cur = await conn.execute(
        "INSERT INTO domain (workspace_id, owner_id, project_id, level, parent_id, parent_level,"
        " name, title, description, updated_by, updated_token)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            project.workspace_id,
            author.owner_id,
            project.id,
            1 if parent_id is None else 2,
            parent_id,
            None if parent_id is None else 1,
            name,
            title,
            description,
            author.owner_id,
            author.token_id,
        ),
    )
    return await _returned_id(cur)


async def update_domain(
    conn: Conn, author: Author, domain_id: UUID, *, title: str, description: str
) -> None:
    await conn.execute(
        "UPDATE domain SET title = %s, description = %s, updated_at = now(), updated_by = %s,"
        " updated_token = %s WHERE id = %s",
        (title, description, author.owner_id, author.token_id, domain_id),
    )


async def delete_domains(conn: Conn, domain_ids: Sequence[UUID]) -> None:
    """Delete the domains, with their subdomains and code entries."""
    if domain_ids:
        await conn.execute("DELETE FROM domain WHERE id = ANY(%s)", (list(domain_ids),))


async def insert_code(
    conn: Conn,
    author: Author,
    project: StoredProject,
    *,
    domain_id: UUID,
    service_id: UUID,
    paths: Sequence[str],
) -> None:
    await conn.execute(
        "INSERT INTO domain_code (workspace_id, owner_id, domain_id, service_id, paths,"
        " updated_by, updated_token) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            project.workspace_id,
            author.owner_id,
            domain_id,
            service_id,
            list(paths),
            author.owner_id,
            author.token_id,
        ),
    )


async def update_code(conn: Conn, author: Author, code_id: UUID, paths: Sequence[str]) -> None:
    await conn.execute(
        "UPDATE domain_code SET paths = %s, updated_at = now(), updated_by = %s,"
        " updated_token = %s WHERE id = %s",
        (list(paths), author.owner_id, author.token_id, code_id),
    )


async def delete_code(conn: Conn, code_ids: Sequence[UUID]) -> None:
    if code_ids:
        await conn.execute("DELETE FROM domain_code WHERE id = ANY(%s)", (list(code_ids),))


async def insert_link(
    conn: Conn,
    author: Author,
    project: StoredProject,
    *,
    service_id: UUID,
    target_id: UUID,
    kind: str,
    note: str,
    paths: Sequence[str],
) -> None:
    await conn.execute(
        "INSERT INTO service_link (workspace_id, owner_id, service_id, target_id, kind, note,"
        " paths, updated_by, updated_token) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            project.workspace_id,
            author.owner_id,
            service_id,
            target_id,
            kind,
            note,
            list(paths),
            author.owner_id,
            author.token_id,
        ),
    )


async def update_link(
    conn: Conn, author: Author, link_id: UUID, *, kind: str, note: str, paths: Sequence[str]
) -> None:
    await conn.execute(
        "UPDATE service_link SET kind = %s, note = %s, paths = %s, updated_at = now(),"
        " updated_by = %s, updated_token = %s WHERE id = %s",
        (kind, note, list(paths), author.owner_id, author.token_id, link_id),
    )


async def delete_links(conn: Conn, link_ids: Sequence[UUID]) -> None:
    if link_ids:
        await conn.execute("DELETE FROM service_link WHERE id = ANY(%s)", (list(link_ids),))


async def _returned_id(cur: Any) -> UUID:
    row = await cur.fetchone()
    if row is None:  # INSERT ... RETURNING always returns the row
        raise RuntimeError("INSERT returned no row")
    found: UUID = row[0]
    return found
