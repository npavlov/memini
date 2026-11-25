"""The catalog as search reads it: which code a project, a domain or a service names, and what a
file belongs to.

The catalog (:mod:`memini.catalog`; requirements, section 3) says that a service is a git source,
whole or one directory of it, and that a domain or subdomain names code inside a service by path
masks. Nothing of that is stored on chunks or records: membership is computed here, from the
masks, when a question is asked. So a change of the catalog changes the answers at once, without
a reindex, and the index fingerprint does not know the catalog.

Two questions, one matcher:

- **Which chunks does a filter name?** :func:`code_groups` turns the ``projects``, ``domains``
  and ``services`` filters of a search into groups of :class:`SourcePaths`, a source with a
  regular expression over paths, which the shared condition builder
  (:func:`memini.search.retrieval.chunk_conditions`) puts into SQL as
  ``c.source_id = … AND c.path ~ …``. Postgres evaluates the expression.
- **What does this file belong to?** :meth:`CodeMap.context` gives the project, the service and
  the domains of a path. Python evaluates the same expression
  (:func:`memini.search.patterns.path_matcher`).

Both expressions come from :func:`memini.search.patterns.glob_regex`, the dialect of the ``paths``
filter, so a file a filter finds is a file whose context names the filter's domain;
tests/test_catalog_db.py asks Postgres and Python about the same paths.

How names are read:

- A project is its services and the code its domains name, which may be in a service of another
  project of the workspace.
- A domain includes its subdomains. A subdomain is addressed as ``domain/subdomain``.
- With a ``projects`` filter, the domains and services named are looked up in those projects
  only. Without one, a name stands for every project of the workspaces searched that has it, as
  a source name stands for one source in each workspace.
- A name that no workspace searched knows raises
  :class:`~memini.search.errors.UnknownNameError`. A domain that exists and names no code gives
  an empty group, which matches nothing.

A file is in the service whose root holds it; when a service is a directory of another, in the
nearest one. A directory of a repository is one service (the database has the key), so there is
one nearest. The domains of a file are all whose masks match it, whichever of the services over
its repository the mask is written in.
"""

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter, WriteAccess
from memini.search.errors import InvalidSearchError, UnknownNameError
from memini.search.patterns import directory_regex, globs_regex, path_matcher

__all__ = [
    "CodeArea",
    "CodeMap",
    "FileContext",
    "ServiceArea",
    "SourcePaths",
    "code_groups",
    "domain_address",
    "load_code_maps",
]

type Conn = AsyncConnection[TupleRow]

log = logging.getLogger(__name__)


def domain_address(domain: str, subdomain: str | None) -> str:
    """How a domain is named: ``domain``, or ``domain/subdomain``."""
    return domain if subdomain is None else f"{domain}/{subdomain}"


@dataclass(frozen=True, slots=True)
class ServiceArea:
    """A service: a git source, whole or one directory of it."""

    project: str
    name: str
    source_id: UUID
    root: str
    """The directory, without a slash at either end; empty for the whole repository."""

    @property
    def regex(self) -> str | None:
        """The paths of the service; ``None``: every path of its source."""
        return directory_regex(self.root) if self.root else None


@dataclass(frozen=True, slots=True)
class CodeArea:
    """The code of a domain or subdomain inside one service: its masks."""

    project: str
    """The project of the domain; the service may be of another one."""
    domain: str
    subdomain: str | None
    service: ServiceArea
    paths: tuple[str, ...]
    """Path globs, read inside the service's root."""

    @property
    def address(self) -> str:
        return domain_address(self.domain, self.subdomain)

    @property
    def regex(self) -> str:
        """The paths the masks match, from the root of the repository."""
        return globs_regex(self.paths, root=self.service.root)


@dataclass(frozen=True, slots=True)
class SourcePaths:
    """Chunks of one source whose path matches ``regex``; ``None``: every chunk of the source."""

    source_id: UUID
    regex: str | None


@dataclass(frozen=True, slots=True)
class FileContext:
    """What a file belongs to. A file outside every service has neither project nor service, and
    a file no mask matches no domains."""

    project: str | None
    service: str | None
    domains: tuple[tuple[str, str], ...] = ()
    """``(project, address)`` of each domain or subdomain whose masks match, sorted."""


class CodeMap:
    """The catalog of one workspace, as far as membership needs it: its projects, services,
    domains and the code they name."""

    def __init__(
        self,
        *,
        projects: Iterable[str] = (),
        services: Iterable[ServiceArea] = (),
        domains: Iterable[tuple[str, str, str | None]] = (),
        areas: Iterable[CodeArea] = (),
    ) -> None:
        self.projects: frozenset[str] = frozenset(projects)
        self.services: tuple[ServiceArea, ...] = tuple(services)
        self.domains: frozenset[tuple[str, str, str | None]] = frozenset(domains)
        """``(project, domain, subdomain)``; the subdomain is ``None`` for the domain itself."""
        self.areas: tuple[CodeArea, ...] = tuple(areas)
        self._matchers: dict[str, Callable[[str], bool]] = {}
        # By source, each with its expression made once: a hit asks about one source.
        self._services: dict[UUID, list[tuple[ServiceArea, str | None]]] = {}
        for service in self.services:
            self._services.setdefault(service.source_id, []).append((service, service.regex))
        self._areas: dict[UUID, list[tuple[CodeArea, str]]] = {}
        for area in self.areas:
            self._areas.setdefault(area.service.source_id, []).append((area, area.regex))

    def matches(self, regex: str | None, path: str) -> bool:
        """Whether ``path`` matches ``regex``, as Postgres would answer; ``None`` matches all."""
        if regex is None:
            return True
        matcher = self._matchers.get(regex)
        if matcher is None:
            matcher = self._matchers[regex] = path_matcher(regex)
        return matcher(path)

    def matching(self, regex: str | None, paths: Iterable[str]) -> list[str]:
        """Those of ``paths`` that match ``regex``, in their order."""
        return [path for path in paths if self.matches(regex, path)]

    def service_of(self, source_id: UUID, path: str) -> ServiceArea | None:
        """The service the file is in: of those whose root holds it, the nearest."""
        held = [
            service
            for service, regex in self._services.get(source_id, ())
            if self.matches(regex, path)
        ]
        return max(held, key=lambda service: len(service.root), default=None)

    def areas_of(self, source_id: UUID, path: str) -> list[CodeArea]:
        """The code entries whose masks match the file."""
        return [area for area, regex in self._areas.get(source_id, ()) if self.matches(regex, path)]

    def areas_in(self, source_id: UUID) -> list[CodeArea]:
        """The code entries written in a service over the source, of whatever project."""
        return [area for area, _ in self._areas.get(source_id, ())]

    def context(self, source_id: UUID, path: str) -> FileContext:
        """What the file at ``path`` of the source ``source_id`` belongs to."""
        service = self.service_of(source_id, path)
        domains = sorted({(area.project, area.address) for area in self.areas_of(source_id, path)})
        return FileContext(
            project=None if service is None else service.project,
            service=None if service is None else service.name,
            domains=tuple(domains),
        )


# --- Filters -------------------------------------------------------------------------------------


class _Group:
    """The sources of one filter with their expressions; a source named whole stays whole."""

    def __init__(self) -> None:
        self.sources: dict[UUID, set[str] | None] = {}

    def add(self, source_id: UUID, regex: str | None) -> None:
        if regex is None:
            self.sources[source_id] = None
            return
        held = self.sources.setdefault(source_id, set[str]())
        if held is not None:
            held.add(regex)

    def build(self) -> tuple[SourcePaths, ...]:
        return tuple(
            SourcePaths(source_id, None if regexes is None else _any(sorted(regexes)))
            for source_id, regexes in sorted(self.sources.items(), key=lambda item: item[0].hex)
        )


def _any(regexes: Sequence[str]) -> str:
    """One expression that matches when any of ``regexes`` does."""
    return regexes[0] if len(regexes) == 1 else "|".join(f"(?:{regex})" for regex in regexes)


def code_groups(
    maps: Mapping[UUID, CodeMap],
    *,
    projects: Sequence[str] | None = None,
    domains: Sequence[str] | None = None,
    services: Sequence[str] | None = None,
) -> tuple[tuple[SourcePaths, ...], ...]:
    """The filters as groups of sources and paths: a chunk passes when it is in one member of
    every group (the filters all apply, the values of one are alternatives). ``maps`` are the
    catalogs of the workspaces searched; see the module for how names are read.

    Raises:
        UnknownNameError: a value names nothing in any of ``maps``.
    """
    groups: list[tuple[SourcePaths, ...]] = []
    within = None if projects is None else frozenset(projects)
    if projects is not None:
        group = _Group()
        for position, name in enumerate(projects, start=1):
            if not any(name in catalog.projects for catalog in maps.values()):
                raise UnknownNameError("project", position)
            for catalog in maps.values():
                for service in catalog.services:
                    if service.project == name:
                        group.add(service.source_id, service.regex)
                for area in catalog.areas:
                    if area.project == name:
                        group.add(area.service.source_id, area.regex)
        groups.append(group.build())
    if domains is not None:
        group = _Group()
        for position, address in enumerate(domains, start=1):
            domain, _, rest = address.partition("/")
            subdomain = rest or None
            known = False
            for catalog in maps.values():
                for project, held, part in catalog.domains:
                    if (
                        held == domain
                        and part == subdomain
                        and (within is None or project in within)
                    ):
                        known = True
                for area in catalog.areas:
                    if (
                        area.domain == domain
                        and (subdomain is None or area.subdomain == subdomain)
                        and (within is None or area.project in within)
                    ):
                        group.add(area.service.source_id, area.regex)
            if not known:
                raise UnknownNameError("domain", position)
        groups.append(group.build())
    if services is not None:
        group = _Group()
        for position, name in enumerate(services, start=1):
            known = False
            for catalog in maps.values():
                for service in catalog.services:
                    if service.name == name and (within is None or service.project in within):
                        known = True
                        group.add(service.source_id, service.regex)
            if not known:
                raise UnknownNameError("service", position)
        groups.append(group.build())
    return tuple(groups)


# --- Reading -------------------------------------------------------------------------------------

_PROJECTS: Final = sql.SQL(
    "SELECT p.workspace_id, p.name FROM project p WHERE p.workspace_id = ANY(%s) AND {access}"
)

_SERVICES: Final = sql.SQL(
    "SELECT s.workspace_id, p.name, s.name, s.source_id, s.root"
    " FROM service s JOIN project p ON p.id = s.project_id"
    " WHERE s.workspace_id = ANY(%s) AND {access}"
)

# Every domain and subdomain, once without code or once per service it names code in.
_DOMAINS: Final = sql.SQL(
    "SELECT d.workspace_id, p.name, coalesce(up.name, d.name),"
    " CASE WHEN up.id IS NULL THEN NULL ELSE d.name END,"
    " sp.name, s.name, s.source_id, s.root, c.paths"
    " FROM domain d JOIN project p ON p.id = d.project_id"
    " LEFT JOIN domain up ON up.id = d.parent_id"
    " LEFT JOIN domain_code c ON c.domain_id = d.id"
    " LEFT JOIN service s ON s.id = c.service_id"
    " LEFT JOIN project sp ON sp.id = s.project_id"
    " WHERE d.workspace_id = ANY(%s) AND {access}"
)


def _where(access: ReadFilter | WriteAccess, alias: str) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
    )


async def load_code_maps(
    conn: Conn, access: ReadFilter | WriteAccess, workspace_ids: Collection[UUID]
) -> dict[UUID, CodeMap]:
    """The catalog of each of ``workspace_ids`` that ``access`` reaches, by workspace; a
    workspace without a catalog, or out of reach, has an empty one."""
    wanted = sorted(set(workspace_ids), key=lambda each: each.hex)
    if not wanted:
        return {}
    projects: dict[UUID, list[str]] = {each: [] for each in wanted}
    services: dict[UUID, list[ServiceArea]] = {each: [] for each in wanted}
    domains: dict[UUID, list[tuple[str, str, str | None]]] = {each: [] for each in wanted}
    areas: dict[UUID, list[CodeArea]] = {each: [] for each in wanted}

    cur = await conn.execute(_PROJECTS.format(access=_where(access, "p")), (wanted,))
    for workspace_id, name in await cur.fetchall():
        projects[workspace_id].append(name)
    cur = await conn.execute(_SERVICES.format(access=_where(access, "s")), (wanted,))
    for workspace_id, project, name, source_id, root in await cur.fetchall():
        services[workspace_id].append(ServiceArea(project, name, source_id, root))
    cur = await conn.execute(_DOMAINS.format(access=_where(access, "d")), (wanted,))
    for row in await cur.fetchall():
        workspace_id, project, domain, subdomain = row[:4]
        domains[workspace_id].append((project, domain, subdomain))
        service_project, service, source_id, root, paths = row[4:]
        if service is None:
            continue
        area = CodeArea(
            project=project,
            domain=domain,
            subdomain=subdomain,
            service=ServiceArea(service_project, service, source_id, root),
            paths=tuple(paths),
        )
        try:
            _ = area.regex
        except InvalidSearchError:
            # put_project checks every mask, so this is a row written past it, or a mask of an
            # earlier dialect. It matches nothing, rather than failing every search here.
            log.warning(
                "a code entry of domain %s in project %s holds a path mask that cannot be read;"
                " it matches nothing",
                area.address,
                project,
            )
            continue
        areas[workspace_id].append(area)
    return {
        each: CodeMap(
            projects=projects[each],
            services=services[each],
            domains=domains[each],
            areas=areas[each],
        )
        for each in wanted
    }
