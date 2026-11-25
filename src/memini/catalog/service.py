"""The catalog tools as functions: from validated arguments to a result, or a
:class:`CatalogError`.

One function per tool, for the MCP tools (:mod:`memini.mcp.catalog`) and the HTTP routes
(:mod:`memini.api.catalog`) alike: :func:`run_get_catalog`, :func:`run_get_domain`,
:func:`run_get_project`, :func:`run_put_project` and :func:`catalog_guide`. The reads take the
caller's :class:`~memini.auth.ReadFilter`. The write takes its
:class:`~memini.auth.WriteAccess` (the ``ingest`` scope) and its ``ReadFilter`` (the ``read``
scope) both: its answer counts the files each mask matches, which is a read of the index, so a
token that may only push records cannot probe a repository's paths through it.

``put_project`` in order:

1. The document is checked and cleaned without the database (:func:`normalize`): names are
   handles, a root is a relative directory, every mask is one the matcher takes; titles and
   descriptions have their secrets redacted, as ingest redacts a payload (principle 11). A name,
   a root or a mask is an identifier and is not rewritten: one that holds a secret is refused.
   Link notes are redacted as descriptions are. A link names a service of the document, or of
   another project of the same workspace: nothing else can be named.
2. One transaction: the project's row is made if there is none, and locked; ``if_version`` is
   compared with its version; the document is compared with what is stored, entry by entry, and
   only what differs is written, each changed row stamped with who and when. If anything was
   written the project's version goes up by one. The same document again writes nothing and
   leaves the version.
3. After it commits, the report: what each mask matches in the index as it is now
   (:mod:`memini.catalog.report`), the masks of the links too. Nothing of the report is stored.

Every refusal becomes a :class:`CatalogError` with an HTTP status, a stable code and a message
that never repeats a name, a mask or a text of the document; it names the place in the document
(``domains[1].code[0].paths[2]``):

| Error | Status | Code |
|---|---|---|
| a field of the document that is refused | 422 | ``invalid_project`` |
| no workspace named and the token reads several | 422 | ``ambiguous_workspace`` |
| no such workspace, project, domain, repository or service in reach | 404 | ``not_found`` |
| ``if_version`` is not the project's version | 409 | ``version_conflict`` |
| the document removes a service another project's domain or link names | 409 | ``service_in_use`` |
| a service's directory is a service of another project already | 409 | ``service_conflict`` |
| the catalog changed under the write (a service named was deleted) | 409 | ``catalog_conflict`` |
"""

import unicodedata
from collections import Counter
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow

from memini.auth import ReadFilter, WriteAccess
from memini.catalog import store
from memini.catalog.guide import CATALOG_GUIDE
from memini.catalog.model import (
    MAX_DESCRIPTION_CHARS,
    MAX_NOTE_CHARS,
    MAX_ROOT_CHARS,
    MAX_TITLE_CHARS,
    Catalog,
    CatalogGuide,
    CodeDocument,
    CodeEntry,
    DomainDetail,
    DomainEntry,
    GetCatalogArguments,
    GetDomainArguments,
    GetProjectArguments,
    LinkDetail,
    LinkDocument,
    LinkEntry,
    LinkMaskReport,
    MaskFiles,
    ProjectDetail,
    ProjectDocument,
    ProjectEntry,
    PutProjectResult,
    RefInfo,
    RepositoryInfo,
    ServiceEntry,
    SubdomainDetail,
    SubdomainDocument,
    SubdomainEntry,
)
from memini.catalog.report import domain_code, languages, project_report
from memini.catalog.store import (
    Author,
    Repository,
    StoredCode,
    StoredDomain,
    StoredLink,
    StoredProject,
)
from memini.decisions.reads import domain_decisions
from memini.redaction import RedactionKind, redact_text
from memini.search.catalog import CodeArea, domain_address, load_code_maps
from memini.search.errors import InvalidSearchError
from memini.search.patterns import directory_regex, glob_regex, path_matcher
from memini.workspaces import (
    NotFoundError,
    Workspace,
    check_handle,
    get_workspace,
    get_writable_workspace,
    list_workspaces,
)

__all__ = [
    "CatalogError",
    "InvalidProjectError",
    "NewCode",
    "NewDomain",
    "NewLink",
    "NewProject",
    "NewService",
    "catalog_errors",
    "catalog_guide",
    "normalize",
    "run_get_catalog",
    "run_get_domain",
    "run_get_project",
    "run_put_project",
]

type Conn = AsyncConnection[TupleRow]

_CATALOG_CONSTRAINTS: Final = ("project_", "service_", "domain_")
"""How the names of the catalog's constraints start (migration 0018)."""


class CatalogError(Exception):
    """A catalog call that was refused: an HTTP status, a stable code and a message for the
    caller. The message never holds a name, a mask, a text of the document or a secret."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class InvalidProjectError(ValueError):
    """A field of the project document was refused. ``field`` is its place in the document, and
    ``reason`` never repeats its value."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


@contextmanager
def catalog_errors() -> Generator[None]:
    """Raise the :class:`CatalogError` for a refusal of the catalog's code or constraints; see
    the module."""
    try:
        yield
    except InvalidProjectError as error:
        raise CatalogError(
            422,
            "invalid_project",
            f"The project document is not valid: {error.field}: {error.reason}.",
        ) from None
    except NotFoundError as error:
        raise CatalogError(404, "not_found", f"No such {error.what}.") from None
    except (errors.UniqueViolation, errors.ForeignKeyViolation, errors.CheckViolation) as error:
        constraint = error.diag.constraint_name or ""
        if constraint == store.SOURCE_ROOT_KEY:
            raise CatalogError(
                409,
                "service_conflict",
                "A directory of a repository is one service in the workspace, and another"
                " service is that directory of that repository already. Do not declare it"
                " again: name the existing service in the domain's code, with its project.",
            ) from None
        if not constraint.startswith(_CATALOG_CONSTRAINTS):
            raise
        # What the code does not check first: a race it lost, or a rule only the database has.
        raise CatalogError(
            409,
            "catalog_conflict",
            f"The catalog changed while the project was written (constraint {constraint});"
            " read it again with get_catalog and send the document again.",
        ) from None


# --- The document, checked and cleaned -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewService:
    where: str
    """Its place in the document, for messages."""
    name: str
    description: str
    repository: str
    root: str


@dataclass(frozen=True, slots=True)
class NewCode:
    where: str
    project: str
    """The project of the service: the document's, unless the entry names another."""
    service: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NewDomain:
    address: tuple[str, ...]
    """``(domain,)`` or ``(domain, subdomain)``."""
    title: str
    description: str
    code: tuple[NewCode, ...]


@dataclass(frozen=True, slots=True)
class NewLink:
    where: str
    service: str
    """The service of the document that uses."""
    project: str
    """The project of the service used: the document's, unless the entry names another."""
    target: str
    """The service used."""
    kind: str
    note: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NewProject:
    name: str
    title: str
    description: str
    services: tuple[NewService, ...]
    domains: tuple[NewDomain, ...]
    """Every domain before its subdomains."""
    links: tuple[NewLink, ...] = ()
    """The links of its services, in the order of the document."""


def _handle(field: str, kind: str, value: str) -> str:
    try:
        name = check_handle(kind, value)
    except ValueError:
        raise InvalidProjectError(
            field,
            f"a {kind} name is 1 to 63 lowercase letters, digits, '-' or '_', starting with a"
            " letter or digit",
        ) from None
    _no_secret(field, name)
    return name


def _no_secret(field: str, value: str) -> None:
    """Refuse an identifier that holds a secret: rewriting it would change what it names."""
    if redact_text(value).total:
        raise InvalidProjectError(field, "holds what reads as a secret or a card number")


def _controls(text: str, *, allowed: str = "") -> bool:
    return any(unicodedata.category(char) == "Cc" and char not in allowed for char in text)


def _title(field: str, value: str, counts: Counter[RedactionKind]) -> str:
    """A title on one line: runs of white space are one space; secrets are redacted."""
    cleaned = redact_text(" ".join(value.split()))
    counts.update(cleaned.counts)
    title = cleaned.value
    if _controls(title):
        raise InvalidProjectError(field, "holds a control character")
    if len(title) > MAX_TITLE_CHARS:
        raise InvalidProjectError(field, f"longer than {MAX_TITLE_CHARS} characters")
    return title


def _description(
    field: str,
    value: str,
    counts: Counter[RedactionKind],
    *,
    limit: int = MAX_DESCRIPTION_CHARS,
) -> str:
    """A description, or a link's note: line ends are ``\\n``, the ends are trimmed; secrets are
    redacted."""
    cleaned = redact_text(value.replace("\r\n", "\n").replace("\r", "\n").strip())
    counts.update(cleaned.counts)
    description = cleaned.value
    if _controls(description, allowed="\n\t"):
        raise InvalidProjectError(field, "holds a control character")
    if len(description) > limit:
        raise InvalidProjectError(field, f"longer than {limit} characters")
    return description


def _root(field: str, value: str) -> str:
    """A service's directory as it is stored: from the root of the repository, without a slash
    at either end (``/services/billing/`` is ``services/billing``); empty, or only slashes, for
    the whole repository."""
    root = value.strip("/")
    if not root:
        return ""
    if (
        len(root) > MAX_ROOT_CHARS
        or _controls(root)
        or any(part in ("", ".", "..") for part in root.split("/"))
    ):
        raise InvalidProjectError(field, _ROOT_RULE)
    _no_secret(field, root)
    return root


_ROOT_RULE: Final = (
    "a root is a directory of the repository, from its root (empty for the whole repository),"
    " without '.', '..' or empty components and without control characters"
)


def _masks(field: str, values: Sequence[str]) -> tuple[str, ...]:
    for index, mask in enumerate(values):
        place = f"{field}[{index}]"
        try:
            glob_regex(mask)
        except InvalidSearchError as error:
            raise InvalidProjectError(place, str(error)) from None
        _no_secret(place, mask)
    return tuple(dict.fromkeys(values))


def _code(
    field: str, project: str, entries: Sequence[CodeDocument], services: frozenset[str]
) -> tuple[NewCode, ...]:
    found: dict[tuple[str, str], NewCode] = {}
    for index, entry in enumerate(entries):
        where = f"{field}[{index}]"
        owner = (
            project
            if entry.project is None
            else _handle(f"{where}.project", "project", entry.project)
        )
        service = _handle(f"{where}.service", "service", entry.service)
        if owner == project and service not in services:
            raise InvalidProjectError(
                f"{where}.service", "names a service the document does not have"
            )
        if (owner, service) in found:
            raise InvalidProjectError(
                where, "the domain names this service twice; put the masks into one entry"
            )
        found[(owner, service)] = NewCode(
            where=where,
            project=owner,
            service=service,
            paths=_masks(f"{where}.paths", entry.paths),
        )
    return tuple(found.values())


def _links(
    project: str,
    services: dict[str, NewService],
    uses: Sequence[Sequence[LinkDocument]],
    counts: Counter[RedactionKind],
) -> tuple[NewLink, ...]:
    """The links of the services, ``uses`` in the order of ``services``."""
    links: list[NewLink] = []
    for service, entries in zip(services.values(), uses, strict=True):
        found: set[tuple[str, str]] = set()
        for index, entry in enumerate(entries):
            where = f"{service.where}.uses[{index}]"
            owner = (
                project
                if entry.project is None
                else _handle(f"{where}.project", "project", entry.project)
            )
            target = _handle(f"{where}.service", "service", entry.service)
            if owner == project and target not in services:
                raise InvalidProjectError(
                    f"{where}.service", "names a service the document does not have"
                )
            if (owner, target) == (project, service.name):
                raise InvalidProjectError(f"{where}.service", "a service does not use itself")
            if (owner, target) in found:
                raise InvalidProjectError(
                    where, "the service names this service twice; say both uses in one entry"
                )
            found.add((owner, target))
            links.append(
                NewLink(
                    where=where,
                    service=service.name,
                    project=owner,
                    target=target,
                    kind=entry.kind,
                    note=_description(f"{where}.note", entry.note, counts, limit=MAX_NOTE_CHARS),
                    paths=_masks(f"{where}.paths", entry.paths),
                )
            )
    return tuple(links)


def normalize(
    project: str, document: ProjectDocument
) -> tuple[NewProject, dict[RedactionKind, int]]:
    """``document`` as it is stored and compared, and what was redacted in its texts, by kind.

    The same document always gives the same result, and the result of what is stored is itself,
    which is what makes ``put_project`` idempotent.

    Raises:
        InvalidProjectError: a field is refused; nothing was read or written.
    """
    counts: Counter[RedactionKind] = Counter()
    name = _handle("project", "project", project)

    services: dict[str, NewService] = {}
    places: set[tuple[str, str]] = set()
    for index, each in enumerate(document.services):
        where = f"services[{index}]"
        service = NewService(
            where=where,
            name=_handle(f"{where}.name", "service", each.name),
            description=_description(f"{where}.description", each.description, counts),
            repository=each.source,
            root=_root(f"{where}.root", each.root),
        )
        if service.name in services:
            raise InvalidProjectError(f"{where}.name", "the document has a service of that name")
        if (service.repository, service.root) in places:
            raise InvalidProjectError(
                where, "another service of the document is that directory of that repository"
            )
        services[service.name] = service
        places.add((service.repository, service.root))
    known = frozenset(services)

    def domain(where: str, address: tuple[str, ...], each: SubdomainDocument) -> NewDomain:
        return NewDomain(
            address=address,
            title=_title(f"{where}.title", each.title, counts),
            description=_description(f"{where}.description", each.description, counts),
            code=_code(f"{where}.code", name, each.code, known),
        )

    domains: dict[tuple[str, ...], NewDomain] = {}
    for index, each in enumerate(document.domains):
        where = f"domains[{index}]"
        top = (_handle(f"{where}.name", "domain", each.name),)
        if top in domains:
            raise InvalidProjectError(f"{where}.name", "the document has a domain of that name")
        domains[top] = domain(where, top, each)
        for number, part in enumerate(each.subdomains):
            below = f"{where}.subdomains[{number}]"
            address = (*top, _handle(f"{below}.name", "subdomain", part.name))
            if address in domains:
                raise InvalidProjectError(
                    f"{below}.name", "the domain has a subdomain of that name"
                )
            domains[address] = domain(below, address, part)
    return (
        NewProject(
            name=name,
            title=_title("title", document.title, counts),
            description=_description("description", document.description, counts),
            services=tuple(services.values()),
            # A domain is in the mapping before its subdomains; sorting by depth keeps that.
            domains=tuple(sorted(domains.values(), key=lambda each: len(each.address))),
            links=_links(name, services, [each.uses for each in document.services], counts),
        ),
        dict(counts),
    )


# --- Reads ---------------------------------------------------------------------------------------


async def _readable_workspace(conn: Conn, access: ReadFilter, name: str | None) -> Workspace:
    """The workspace ``name``; without a name, the one workspace ``access`` reads."""
    if name is not None:
        found = await get_workspace(conn, access, name)
        if found is None:
            raise NotFoundError("workspace")
        return found
    spaces = await list_workspaces(conn, access)
    if not spaces:
        raise NotFoundError("workspace")
    if len(spaces) > 1:
        raise CatalogError(
            422, "ambiguous_workspace", "The token reads more than one workspace: name the one."
        )
    return spaces[0]


def _code_entry(code: StoredCode) -> CodeEntry:
    return CodeEntry(
        project=code.project,
        service=code.service,
        paths=list(code.paths),
        updated_at=code.stamp.at,
        updated_by=code.stamp.by,
        updated_token=code.stamp.token,
    )


def _subdomain_entry(
    project: StoredProject, domain: StoredDomain, address: tuple[str, ...]
) -> SubdomainEntry:
    return SubdomainEntry(
        name=domain.name,
        domain="/".join(address),
        title=domain.title,
        description=domain.description,
        code=[_code_entry(code) for code in project.code if code.domain_id == domain.id],
        updated_at=domain.stamp.at,
        updated_by=domain.stamp.by,
        updated_token=domain.stamp.token,
    )


def _under(root: str, paths: Sequence[str]) -> list[str]:
    """The paths below the directory ``root``; all of them for the whole repository."""
    if not root:
        return list(paths)
    below = path_matcher(directory_regex(root))
    return [path for path in paths if below(path)]


def _refs(repositories: Sequence[Repository]) -> dict[UUID, list[RefInfo]]:
    return {
        each.id: [RefInfo(ref=ref, commit=commit) for ref, commit in each.refs]
        for each in repositories
    }


def _link_entry(link: StoredLink) -> LinkEntry:
    return LinkEntry(
        project=link.to_project,
        service=link.to_service,
        kind=link.kind,
        note=link.note,
        paths=list(link.paths),
        updated_at=link.stamp.at,
        updated_by=link.stamp.by,
        updated_token=link.stamp.token,
    )


def _project_entry(
    project: StoredProject, refs: dict[UUID, list[RefInfo]], files: dict[UUID, list[str]]
) -> ProjectEntry:
    """A stored project in the shape of its document, with what the index holds of it."""
    addresses = project.addresses()
    services: list[ServiceEntry] = []
    for service in project.services:
        below = _under(service.root, files.get(service.source_id, ()))
        services.append(
            ServiceEntry(
                name=service.name,
                description=service.description,
                source=service.repository,
                root=service.root,
                refs=refs.get(service.source_id, []),
                files=len(below),
                languages=languages(below),
                uses=[_link_entry(link) for link in project.links if link.service_id == service.id],
                updated_at=service.stamp.at,
                updated_by=service.stamp.by,
                updated_token=service.stamp.token,
            )
        )
    domains: list[DomainEntry] = []
    for domain in project.domains:
        if domain.parent_id is not None:
            continue
        entry = _subdomain_entry(project, domain, addresses[domain.id])
        domains.append(
            DomainEntry(
                **entry.model_dump(),
                subdomains=[
                    _subdomain_entry(project, part, addresses[part.id])
                    for part in project.domains
                    if part.parent_id == domain.id
                ],
            )
        )
    return ProjectEntry(
        name=project.name,
        title=project.title,
        description=project.description,
        version=project.version,
        updated_at=project.stamp.at,
        updated_by=project.stamp.by,
        updated_token=project.stamp.token,
        services=services,
        domains=domains,
    )


async def run_get_catalog(
    conn: Conn, access: ReadFilter, arguments: GetCatalogArguments
) -> Catalog:
    """``get_catalog``: the projects of a workspace in the shape of their documents, and the
    repositories no service is in."""
    with catalog_errors():
        async with conn.transaction():
            workspace = await _readable_workspace(conn, access, arguments.workspace)
            projects = await store.read_projects(conn, access, workspace.id)
            repositories = await store.repositories(conn, access, workspace.id)
            files = await store.source_files(conn, access, [each.id for each in repositories])
    refs = _refs(repositories)
    entries = [_project_entry(project, refs, files) for project in projects]
    in_a_service = {service.source_id for project in projects for service in project.services}
    return Catalog(
        workspace=workspace.name,
        projects=entries,
        unassigned_sources=[
            RepositoryInfo(
                source=each.name,
                refs=refs[each.id],
                files=len(files[each.id]),
                languages=languages(files[each.id]),
            )
            for each in repositories
            if each.id not in in_a_service
        ],
    )


async def run_get_domain(
    conn: Conn, access: ReadFilter, arguments: GetDomainArguments
) -> DomainDetail:
    """``get_domain``: a domain or subdomain with its code and what each mask matches now, and
    its active decisions (:func:`memini.decisions.reads.domain_decisions`)."""
    address = tuple(arguments.domain.split("/"))
    with catalog_errors():
        async with conn.transaction():
            workspace = await _readable_workspace(conn, access, arguments.workspace)
            found = await store.read_projects(conn, access, workspace.id, name=arguments.project)
            if not found:
                raise NotFoundError("project")
            project = found[0]
            addresses = project.addresses()
            domain = next((d for d in project.domains if addresses[d.id] == address), None)
            if domain is None:
                raise NotFoundError("domain")
            catalog = (await load_code_maps(conn, access, [workspace.id]))[workspace.id]
            names = {
                each.id: each.name for each in await store.repositories(conn, access, workspace.id)
            }
            own = [
                area
                for area in catalog.areas
                if area.project == project.name and area.domain == address[0]
            ]
            files = await store.source_files(conn, access, {area.service.source_id for area in own})
            decisions, total = await domain_decisions(
                conn,
                access,
                workspace_id=workspace.id,
                project=project.name,
                domain="/".join(address),
            )

    def areas(of: tuple[str, ...]) -> list[CodeArea]:
        wanted = domain_address(of[0], of[1] if len(of) > 1 else None)
        return [area for area in own if area.address == wanted]

    code, matched = domain_code(catalog, areas(address), files, names)
    subdomains: list[SubdomainDetail] = []
    for part in project.domains:
        if part.parent_id != domain.id:
            continue
        part_code, part_matched = domain_code(catalog, areas(addresses[part.id]), files, names)
        matched |= part_matched
        subdomains.append(
            SubdomainDetail(
                name=part.name,
                domain="/".join(addresses[part.id]),
                title=part.title,
                description=part.description,
                files=len(part_matched),
                code=part_code,
                updated_at=part.stamp.at,
                updated_by=part.stamp.by,
                updated_token=part.stamp.token,
            )
        )
    return DomainDetail(
        workspace=workspace.name,
        project=project.name,
        name=domain.name,
        domain="/".join(address),
        parent=address[0] if len(address) > 1 else None,
        title=domain.title,
        description=domain.description,
        files=len(matched),
        code=code,
        subdomains=subdomains,
        updated_at=domain.stamp.at,
        updated_by=domain.stamp.by,
        updated_token=domain.stamp.token,
        decisions=decisions,
        decisions_total=total,
    )


def _mask_files(root: str, masks: Sequence[str], paths: Sequence[str]) -> list[MaskFiles]:
    """Each mask, read inside ``root``, with the number of ``paths`` it matches."""
    counted: list[MaskFiles] = []
    for mask in masks:
        matches = path_matcher(glob_regex(mask, root=root))
        counted.append(MaskFiles(path=mask, files=sum(1 for path in paths if matches(path))))
    return counted


def _other_end(link: StoredLink, project_id: UUID) -> UUID:
    """The project at the other end of a link of ``project_id``: of the service used when the
    project uses it, of the service that uses otherwise; ``project_id`` for a link between two
    of its own services."""
    return link.to_project_id if link.from_project_id == project_id else link.from_project_id


async def run_get_project(
    conn: Conn, access: ReadFilter, arguments: GetProjectArguments
) -> ProjectDetail:
    """``get_project``: a project in the shape of its document, and its links in both
    directions, each mask with the files it matches now; with ``linked_projects``, only the links
    whose other end is in one of those projects (the project itself: the links between its own
    services)."""
    with catalog_errors():
        async with conn.transaction():
            workspace = await _readable_workspace(conn, access, arguments.workspace)
            found = await store.read_projects(conn, access, workspace.id, name=arguments.project)
            if not found:
                raise NotFoundError("project")
            project = found[0]
            linked: set[UUID] | None = None
            if arguments.linked_projects is not None:
                linked = set()
                for name in dict.fromkeys(arguments.linked_projects):
                    other = await store.read_projects(conn, access, workspace.id, name=name)
                    if not other:
                        raise NotFoundError("project")
                    linked.add(other[0].id)
            links = [
                link
                for link in await store.read_links(conn, access, workspace.id, [project.id])
                if linked is None or _other_end(link, project.id) in linked
            ]
            repositories = await store.repositories(conn, access, workspace.id)
            wanted = {service.source_id for service in project.services}
            wanted |= {link.source_id for link in links}
            files = await store.source_files(conn, access, wanted)

    def detail(link: StoredLink) -> LinkDetail:
        below = _under(link.root, files.get(link.source_id, ()))
        return LinkDetail(
            from_project=link.from_project,
            from_service=link.from_service,
            to_project=link.to_project,
            to_service=link.to_service,
            kind=link.kind,
            note=link.note,
            source=link.repository,
            root=link.root,
            paths=_mask_files(link.root, link.paths, below),
            updated_at=link.stamp.at,
            updated_by=link.stamp.by,
            updated_token=link.stamp.token,
        )

    uses = [detail(link) for link in links if link.from_project_id == project.id]
    used_by = [detail(link) for link in links if link.from_project_id != project.id]
    return ProjectDetail(
        workspace=workspace.name,
        project=_project_entry(project, _refs(repositories), files),
        uses=uses,
        used_by=used_by,
    )


def catalog_guide() -> CatalogGuide:
    """``catalog_guide``: the instruction for filling the catalog. Reads nothing."""
    return CatalogGuide(guide=CATALOG_GUIDE)


# --- The write -----------------------------------------------------------------------------------


async def _write(
    conn: Conn, access: WriteAccess, author: Author, stored: StoredProject, new: NewProject
) -> bool:
    """Make the stored project say what ``new`` says, writing only what differs. Whether
    anything was written."""
    changed = (stored.title, stored.description) != (new.title, new.description)

    # Services: the removed ones first, then the new and the changed.
    sources = await store.git_sources(
        conn, access, stored.workspace_id, {service.repository for service in new.services}
    )
    for service in new.services:
        if service.repository not in sources:
            raise CatalogError(
                404, "not_found", f"No such repository ({service.where}.repository)."
            )
    held = {service.name: service for service in stored.services}
    removed = [held[name].id for name in held.keys() - {s.name for s in new.services}]
    if await store.services_named_elsewhere(conn, stored.id, removed):
        raise CatalogError(
            409,
            "service_in_use",
            "The document removes a service that a domain of another project names code in."
            " Change that project first, or keep the service.",
        )
    if await store.services_linked_elsewhere(conn, stored.id, removed):
        raise CatalogError(
            409,
            "service_in_use",
            "The document removes a service that a service of another project declares it uses."
            " Change that project first, or keep the service.",
        )
    await store.delete_services(conn, removed)
    changed = changed or bool(removed)
    service_ids: dict[tuple[str, str], UUID] = {}
    for service in new.services:
        source_id = sources[service.repository]
        old = held.get(service.name)
        if old is None:
            service_ids[(new.name, service.name)] = await store.insert_service(
                conn,
                author,
                stored,
                name=service.name,
                description=service.description,
                source_id=source_id,
                root=service.root,
            )
            changed = True
            continue
        service_ids[(new.name, service.name)] = old.id
        if (old.description, old.source_id, old.root) != (
            service.description,
            source_id,
            service.root,
        ):
            await store.update_service(
                conn,
                author,
                old.id,
                description=service.description,
                source_id=source_id,
                root=service.root,
            )
            changed = True
    await store.check_service_places(conn)

    # The services of other projects the domains and the links name.
    wanted_code = {
        (domain.address, code.project, code.service): code
        for domain in new.domains
        for code in domain.code
    }
    named = {(code.project, code.service) for code in wanted_code.values()}
    named |= {(link.project, link.target) for link in new.links}
    others = await store.services_of_others(
        conn,
        access,
        stored.workspace_id,
        {(project, service) for project, service in named if project != new.name},
    )
    for code in wanted_code.values():
        if code.project != new.name and (code.project, code.service) not in others:
            raise CatalogError(404, "not_found", f"No such service ({code.where}).")
    for link in new.links:
        # A service of another workspace is not there to name, as one that does not exist.
        if link.project != new.name and (link.project, link.target) not in others:
            raise CatalogError(404, "not_found", f"No such service ({link.where}).")
    service_ids.update(others)

    # Domains: the removed ones first (their subdomains and code go with them), then every
    # domain before its subdomains.
    addresses = stored.addresses()
    held_domains = {addresses[domain.id]: domain for domain in stored.domains}
    kept = {domain.address for domain in new.domains}
    removed_domains = [held_domains[address].id for address in held_domains.keys() - kept]
    await store.delete_domains(conn, removed_domains)
    changed = changed or bool(removed_domains)
    domain_ids: dict[tuple[str, ...], UUID] = {}
    for domain in new.domains:
        old_domain = held_domains.get(domain.address)
        if old_domain is None:
            domain_ids[domain.address] = await store.insert_domain(
                conn,
                author,
                stored,
                parent_id=None if len(domain.address) == 1 else domain_ids[domain.address[:1]],
                name=domain.address[-1],
                title=domain.title,
                description=domain.description,
            )
            changed = True
            continue
        domain_ids[domain.address] = old_domain.id
        if (old_domain.title, old_domain.description) != (domain.title, domain.description):
            await store.update_domain(
                conn, author, old_domain.id, title=domain.title, description=domain.description
            )
            changed = True

    # Code: what is still stored after the deletions above, against what the document names.
    gone = set(removed)
    held_code = {
        (addresses[code.domain_id], code.project, code.service): code
        for code in stored.code
        if addresses[code.domain_id] in kept and code.service_id not in gone
    }
    dropped = [held_code[key].id for key in held_code.keys() - wanted_code.keys()]
    await store.delete_code(conn, dropped)
    changed = changed or bool(dropped)
    for key, code in wanted_code.items():
        old_code = held_code.get(key)
        if old_code is None:
            await store.insert_code(
                conn,
                author,
                stored,
                domain_id=domain_ids[key[0]],
                service_id=service_ids[(code.project, code.service)],
                paths=code.paths,
            )
            changed = True
        elif old_code.paths != code.paths:
            await store.update_code(conn, author, old_code.id, code.paths)
            changed = True

    # Links: what is still stored after the deletions above, against what the document says.
    held_links = {
        (link.from_service, link.to_project, link.to_service): link
        for link in stored.links
        if link.service_id not in gone and link.target_id not in gone
    }
    wanted_links = {(link.service, link.project, link.target): link for link in new.links}
    unlinked = [held_links[key].id for key in held_links.keys() - wanted_links.keys()]
    await store.delete_links(conn, unlinked)
    changed = changed or bool(unlinked)
    for key, link in wanted_links.items():
        old_link = held_links.get(key)
        if old_link is None:
            await store.insert_link(
                conn,
                author,
                stored,
                service_id=service_ids[(new.name, link.service)],
                target_id=service_ids[(link.project, link.target)],
                kind=link.kind,
                note=link.note,
                paths=link.paths,
            )
            changed = True
        elif (old_link.kind, old_link.note, old_link.paths) != (link.kind, link.note, link.paths):
            await store.update_link(
                conn, author, old_link.id, kind=link.kind, note=link.note, paths=link.paths
            )
            changed = True
    return changed


async def run_put_project(
    conn: Conn,
    access: WriteAccess,
    read: ReadFilter,
    workspace: str,
    project: str,
    document: ProjectDocument,
    *,
    token_id: UUID | None = None,
) -> PutProjectResult:
    """``put_project``: make the project ``project`` of ``workspace`` say what ``document``
    says, and report what its masks match; see the module. ``token_id`` is the token of the
    request, kept with each row the call changes."""
    with catalog_errors():
        new, redacted = normalize(project, document)
        author = Author(owner_id=access.owner_id, token_id=token_id)
        async with conn.transaction():
            found = await get_writable_workspace(conn, access, workspace)
            await store.ensure_project(conn, author, found.id, new.name)
            (stored,) = await store.read_projects(conn, access, found.id, name=new.name, lock=True)
            created = stored.version == 0
            if document.if_version is not None and document.if_version != stored.version:
                raise CatalogError(
                    409,
                    "version_conflict",
                    "The project does not exist yet, so its version is 0, not the if_version given."
                    if created
                    else f"The project is at version {stored.version}, not the if_version given:"
                    " it changed since it was read. Read it again with get_catalog, apply the"
                    " change to what is stored now and send it with that version.",
                )
            changed = await _write(conn, access, author, stored, new) or created
            version = stored.version
            if changed:
                version = await store.bump_project(
                    conn, author, stored.id, title=new.title, description=new.description
                )
        async with conn.transaction():
            catalog = (await load_code_maps(conn, read, [found.id]))[found.id]
            names = {each.id: each.name for each in await store.repositories(conn, read, found.id)}
            sources = {
                service.source_id for service in catalog.services if service.project == new.name
            }
            sources |= {
                area.service.source_id for area in catalog.areas if area.project == new.name
            }
            files = await store.source_files(conn, read, sources)
    report = project_report(catalog, new.name, files, names)
    own = {service.name: service for service in catalog.services if service.project == new.name}
    link_masks = [
        LinkMaskReport(
            service=link.service,
            to_project=link.project,
            to_service=link.target,
            path=counted.path,
            files=counted.files,
        )
        for link in new.links
        if link.service in own
        for counted in _mask_files(
            own[link.service].root, link.paths, files.get(own[link.service].source_id, ())
        )
    ]
    return PutProjectResult(
        workspace=found.name,
        project=new.name,
        version=version,
        created=created,
        changed=changed,
        redacted={str(kind): count for kind, count in sorted(redacted.items())},
        masks=report.masks,
        empty_masks=[mask for mask in report.masks if mask.files == 0],
        overlaps=report.overlaps,
        services=report.services,
        link_masks=link_masks,
        empty_link_masks=[mask for mask in link_masks if mask.files == 0],
    )
