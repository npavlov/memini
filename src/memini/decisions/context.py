"""What a decision belongs to: its project and domains, computed when a question is asked.

Requirements, section 3, **Decisions by project and domain**. One mailbox or chat holds messages of
several projects, so a project and domains are set per record (a thread, a meeting), never per
source, and an item inherits them from the records its evidence is in. They come from five places,
in the order they take precedence (:data:`VIAS`):

1. ``push``: whoever pushed the record named them (``record_catalog``,
   :mod:`memini.ingest.catalog`).
2. ``default``: the record was pushed without them, and its stream or its source has a default
   (``source_catalog``; a stream's before its source's).
3. ``manual``: a person linked the item (``record_decision`` with ``domains``, or the link
   route): ``knowledge_item_domain`` with origin ``manual``.
4. ``code``: evidence in a file of a repository, matched against the domain masks of the catalog
   (:class:`memini.search.catalog.CodeMap`, the matcher of search): the domains whose masks match
   the file, and the project of the service it is in.
5. ``model``: the ``assign_domains`` job chose them (:mod:`memini.decisions.assign`): origin
   ``model``. They count only when none of the four above gives the item a domain: the model never
   changes what came by another way.

The project of an item is the first one given, in that order. Its domains are every domain the
first four give, each once with the first way it came by; or, when they give none, the model's.
Nothing here is stored on the item or in the journal: links are rows outside it (migration 0020)
and the rest is derived from evidence, records and masks, so a change of the catalog shows in the
next answer. Names are compared with the catalog as it is: a link, default or pushed name that
names no project or domain of the workspace's catalog is left out.

Filters: an item is under a project when its project is that one or one of its domains is of it;
it is in a domain when one of its domains is that domain or, a domain including its subdomains, one
of its subdomains. An item under a project without a domain of it is counted for the project
(:meth:`Contexts.without_domain`), so a caller knows that a list by domain may be incomplete.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, get_args
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter, WriteAccess
from memini.ingest import InvalidBatchError
from memini.ingest.catalog import MAX_PLACE_DOMAINS, check_address, check_project
from memini.search.catalog import CodeMap, domain_address, load_code_maps

__all__ = [
    "LINK_ORIGINS",
    "VIAS",
    "CatalogNames",
    "Contexts",
    "EvidenceFacts",
    "ItemContext",
    "ItemFacts",
    "LinkOrigin",
    "PlaceError",
    "PlacedDomain",
    "SourceDefault",
    "UnknownPlaceError",
    "Via",
    "add_links",
    "delete_source_default",
    "item_context",
    "list_source_defaults",
    "load_contexts",
    "put_source_default",
    "remove_link",
    "resolve_places",
]

type Conn = AsyncConnection[TupleRow]
type Access = ReadFilter | WriteAccess

Via = Literal["push", "default", "manual", "code", "model"]
"""How an item came to its project or a domain, in the order they take precedence."""
VIAS: Final[tuple[Via, ...]] = get_args(Via)

LinkOrigin = Literal["manual", "model"]
"""The origins a link row of ``knowledge_item_domain`` has."""
LINK_ORIGINS: Final[tuple[LinkOrigin, ...]] = get_args(LinkOrigin)

_RANK: Final = {via: rank for rank, via in enumerate(VIAS)}


# --- Errors --------------------------------------------------------------------------------------


class PlaceError(ValueError):
    """A project or domain given was refused. ``field`` names where; neither it nor ``reason``
    repeats a value."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class UnknownPlaceError(PlaceError):
    """A project or domain the catalog of the workspace does not hold, or a domain name more than
    one of its projects has, given without its project."""


# --- The context of an item ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlacedDomain:
    project: str
    domain: str
    """The address: ``domain`` or ``domain/subdomain``."""
    via: Via


def _within(address: str, wanted: str) -> bool:
    """Whether the domain ``address`` is ``wanted`` or, for a domain, one of its subdomains."""
    return address == wanted or address.startswith(f"{wanted}/")


@dataclass(frozen=True, slots=True)
class ItemContext:
    """The project and domains of one item, each with how it came by it."""

    project: str | None = None
    project_via: Via | None = None
    domains: tuple[PlacedDomain, ...] = ()

    def under(self, projects: Collection[str]) -> bool:
        """Whether the item is under one of ``projects``: its project, or a domain's."""
        return self.project in projects or any(d.project in projects for d in self.domains)

    def in_domains(self, wanted: Iterable[tuple[str, str]]) -> bool:
        """Whether one of the item's domains is one of ``wanted`` ``(project, address)``, or a
        subdomain of one."""
        return any(
            d.project == project and _within(d.domain, address)
            for project, address in wanted
            for d in self.domains
        )

    def has_domain_of(self, project: str) -> bool:
        return any(d.project == project for d in self.domains)

    @property
    def settled(self) -> bool:
        """Whether a domain came by a way the model does not override."""
        return any(d.via != "model" for d in self.domains)

    @property
    def fixed_project(self) -> str | None:
        """The project, when it came by a way the model does not override."""
        return self.project if self.project_via not in (None, "model") else None


@dataclass(frozen=True, slots=True)
class CatalogNames:
    """The names of one workspace's catalog: its projects and the addresses of their domains."""

    projects: frozenset[str] = frozenset()
    domains: frozenset[tuple[str, str]] = frozenset()
    """``(project, address)`` of every domain and subdomain."""

    @classmethod
    def of(cls, catalog: CodeMap) -> "CatalogNames":
        return cls(
            projects=catalog.projects,
            domains=frozenset(
                (project, domain_address(domain, subdomain))
                for project, domain, subdomain in catalog.domains
            ),
        )

    def knows(self, project: str, domain: str | None) -> bool:
        if domain is None:
            return project in self.projects
        return (project, domain) in self.domains

    def projects_with(self, address: str) -> list[str]:
        return sorted(project for project, held in self.domains if held == address)


def resolve_places(
    names: CatalogNames, project: str | None, domains: Sequence[str], *, field: str = "domains"
) -> list[tuple[str, str | None]]:
    """``project`` and ``domains`` as ``(project, address)`` pairs the catalog holds; a project
    alone is ``(project, None)``. Without ``project``, each domain names the one project that has
    it.

    Raises:
        PlaceError: a name that is not a handle or an address; more than
            :data:`~memini.ingest.catalog.MAX_PLACE_DOMAINS` domains.
        UnknownPlaceError: a name the catalog does not hold, or a domain several projects have.
    """
    if len(domains) > MAX_PLACE_DOMAINS:
        raise PlaceError(field, f"at most {MAX_PLACE_DOMAINS} domains")
    try:
        if project is not None:
            check_project(project, "project")
        for n, each in enumerate(domains):
            check_address(each, f"{field}[{n}]")
    except InvalidBatchError as error:
        raise PlaceError(error.field, error.reason) from None
    if project is not None and project not in names.projects:
        raise UnknownPlaceError("project", "no project of that name in the workspace's catalog")
    if project is not None and not domains:
        return [(project, None)]
    places: list[tuple[str, str | None]] = []
    for n, address in enumerate(dict.fromkeys(domains)):
        where = f"{field}[{n}]"
        if project is not None:
            if not names.knows(project, address):
                raise UnknownPlaceError(where, "the project has no domain of that name")
            places.append((project, address))
            continue
        owners = names.projects_with(address)
        if not owners:
            raise UnknownPlaceError(where, "no project of the catalog has a domain of that name")
        if len(owners) > 1:
            raise UnknownPlaceError(
                where, "more than one project has a domain of that name: name it"
            )
        places.append((owners[0], address))
    return places


# --- Loading -------------------------------------------------------------------------------------


def _scope(access: Access, alias: str, *, source: str | None = None) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
        source_id=None if source is None else sql.Identifier(alias, source),
    )


_ITEMS: Final = sql.SQL(
    "SELECT i.id, i.workspace_id, i.kind, i.status, i.decided_at FROM knowledge_item i"
    " WHERE i.workspace_id = ANY(%(workspaces)s) AND {only} AND {access}"
)

_LINKS: Final = sql.SQL(
    "SELECT l.item_id, l.project, l.domain, l.origin FROM knowledge_item_domain l"
    " WHERE l.workspace_id = ANY(%(workspaces)s) AND {only} AND {access}"
    " ORDER BY l.item_id, l.created_at, l.id"
)

# The record and its stream may be gone (retention, forget); then neither its pushed names nor its
# stream's default are known, and only its source's default and its code are left.
_EVIDENCE: Final = sql.SQL(
    "SELECT e.item_id, e.source_id, s.kind, e.record_kind, e.external_id, st.key,"
    "       rc.project, rc.domains"
    " FROM knowledge_evidence e"
    " LEFT JOIN source s ON s.id = e.source_id AND s.workspace_id = e.workspace_id"
    " LEFT JOIN record r ON r.id = e.record_id AND r.workspace_id = e.workspace_id"
    " LEFT JOIN stream st ON st.id = r.stream_id AND st.source_id = r.source_id"
    " LEFT JOIN record_catalog rc ON rc.record_id = r.id"
    " WHERE e.workspace_id = ANY(%(workspaces)s) AND {only} AND {access}"
    " ORDER BY e.item_id, e.id"
)

_DEFAULTS: Final = sql.SQL(
    "SELECT d.source_id, d.stream_key, d.project, d.domains FROM source_catalog d"
    " WHERE d.workspace_id = ANY(%(workspaces)s) AND {access}"
)


@dataclass(frozen=True, slots=True)
class EvidenceFacts:
    """A piece of an item's evidence as its context needs it: where it is, and what the record
    was pushed with (``None``: nothing, or the record is gone)."""

    source_id: UUID
    source_kind: str | None
    record_kind: str
    external_id: str
    stream_key: str | None
    pushed: tuple[str, tuple[str, ...]] | None


@dataclass(frozen=True, slots=True)
class ItemFacts:
    """An item as the filters need it."""

    workspace_id: UUID
    kind: str
    status: str
    decided_at: datetime


@dataclass(slots=True)
class Contexts:
    """The contexts of some items of some workspaces, and the catalogs they were computed with."""

    items: dict[UUID, ItemFacts] = field(default_factory=dict[UUID, ItemFacts])
    contexts: dict[UUID, ItemContext] = field(default_factory=dict[UUID, ItemContext])
    names: dict[UUID, CatalogNames] = field(default_factory=dict[UUID, CatalogNames])
    """By workspace."""
    maps: dict[UUID, CodeMap] = field(default_factory=dict[UUID, CodeMap])

    def context(self, item_id: UUID) -> ItemContext:
        return self.contexts.get(item_id, ItemContext())

    def known_project(self, name: str) -> bool:
        return any(name in each.projects for each in self.names.values())

    def domain_targets(
        self, addresses: Sequence[str], projects: Collection[str] | None
    ) -> list[tuple[str, str]]:
        """The ``(project, address)`` each of ``addresses`` names, in ``projects`` when given.

        Raises:
            UnknownPlaceError: an address no project searched has.
        """
        targets: list[tuple[str, str]] = []
        for n, address in enumerate(addresses):
            found = sorted(
                {
                    (project, held)
                    for names in self.names.values()
                    for project, held in names.domains
                    if held == address and (projects is None or project in projects)
                }
            )
            if not found:
                raise UnknownPlaceError(
                    f"domains[{n}]", "no project searched has a domain of that name"
                )
            targets.extend(found)
        return targets

    def select(
        self,
        *,
        projects: Sequence[str] | None = None,
        domains: Sequence[str] | None = None,
        statuses: Collection[str] | None = None,
    ) -> list[UUID]:
        """The items under one of ``projects`` and in one of ``domains`` (both when given), of
        ``statuses`` when given.

        Raises:
            UnknownPlaceError: a project or domain no catalog searched holds.
        """
        if projects is not None:
            for n, name in enumerate(projects):
                if not self.known_project(name):
                    raise UnknownPlaceError(
                        f"projects[{n}]", "no project of that name in the catalogs searched"
                    )
        within = None if projects is None else frozenset(projects)
        targets = None if domains is None else self.domain_targets(domains, within)
        chosen: list[UUID] = []
        for item_id, facts in self.items.items():
            if statuses is not None and facts.status not in statuses:
                continue
            context = self.context(item_id)
            if within is not None and not context.under(within):
                continue
            if targets is not None and not context.in_domains(targets):
                continue
            chosen.append(item_id)
        return chosen

    def without_domain(
        self, projects: Iterable[str], *, statuses: Collection[str] | None = None
    ) -> dict[str, int]:
        """For each of ``projects``: its items (of ``statuses``) that have no domain of it."""
        counts = dict.fromkeys(sorted(set(projects)), 0)
        for item_id, facts in self.items.items():
            if statuses is not None and facts.status not in statuses:
                continue
            context = self.context(item_id)
            for project in counts:
                if context.under((project,)) and not context.has_domain_of(project):
                    counts[project] += 1
        return counts


def item_context(
    evidence: Sequence[EvidenceFacts],
    links: Sequence[tuple[str, str | None, str]],
    defaults: Mapping[tuple[UUID, str | None], tuple[str, tuple[str, ...]]],
    catalog: CodeMap,
    names: CatalogNames,
) -> ItemContext:
    """The context of one item from its evidence, its links ``(project, domain, origin)``, the
    defaults of the workspace's sources by ``(source, stream key)`` and its catalog; see the
    module for the order. Pure: the reads are :func:`load_contexts`'."""
    entries: list[tuple[str, str | None, Via]] = []

    def add(project: str, domains: Iterable[str], via: Via) -> None:
        entries.append((project, None, via))
        entries.extend((project, domain, via) for domain in domains)

    for each in evidence:
        if each.pushed is not None:
            add(*each.pushed, "push")
        else:
            default = None
            if each.stream_key is not None:
                default = defaults.get((each.source_id, each.stream_key))
            default = default or defaults.get((each.source_id, None))
            if default is not None:
                add(*default, "default")
        if each.source_kind == "repository":
            # A file of a repository is '<full ref>:<path>' (memini.gitsync.model).
            ref, separator, path = each.external_id.partition(":")
            if not separator or not ref.startswith("refs/") or not path:
                continue
            for area in catalog.areas_of(each.source_id, path):
                entries.append((area.project, area.address, "code"))
            service = catalog.service_of(each.source_id, path)
            if service is not None:
                entries.append((service.project, None, "code"))
    for project, domain, origin in links:
        entries.append((project, domain, "model" if origin == "model" else "manual"))

    known = sorted(
        (entry for entry in entries if names.knows(entry[0], entry[1])),
        key=lambda entry: _RANK[entry[2]],
    )
    if not known:
        return ItemContext()
    project, _, project_via = known[0]
    placed: dict[tuple[str, str], Via] = {}
    for held, domain, via in known:
        if domain is not None and via != "model":
            placed.setdefault((held, domain), via)
    if not placed:
        for held, domain, via in known:
            if domain is not None:
                placed.setdefault((held, domain), via)
    return ItemContext(
        project=project,
        project_via=project_via,
        domains=tuple(
            PlacedDomain(held, domain, via)
            for (held, domain), via in sorted(placed.items(), key=lambda e: (_RANK[e[1]], e[0]))
        ),
    )


async def load_contexts(
    conn: Conn,
    access: Access,
    workspace_ids: Collection[UUID],
    *,
    item_ids: Collection[UUID] | None = None,
) -> Contexts:
    """The context of every item of ``workspace_ids`` that ``access`` reaches, or of those of
    ``item_ids`` among them, with the catalogs of the workspaces. In the caller's transaction, if
    one is open."""
    workspaces = sorted(set(workspace_ids), key=lambda each: each.hex)
    found = Contexts()
    if not workspaces:
        return found
    params: dict[str, Any] = {"workspaces": workspaces}
    if item_ids is not None:
        params["items"] = sorted(set(item_ids), key=lambda each: each.hex)

    def only(alias: str, column: str) -> sql.Composable:
        if item_ids is None:
            return sql.SQL("TRUE")
        return sql.SQL("{} = ANY(%(items)s)").format(sql.Identifier(alias, column))

    cur = await conn.execute(
        _ITEMS.format(only=only("i", "id"), access=_scope(access, "i")), params
    )
    for item_id, workspace_id, kind, status, decided_at in await cur.fetchall():
        found.items[item_id] = ItemFacts(workspace_id, kind, status, decided_at)
    found.maps = await load_code_maps(conn, access, workspaces)
    found.names = {each: CatalogNames.of(catalog) for each, catalog in found.maps.items()}
    if not found.items:
        return found

    links: dict[UUID, list[tuple[str, str | None, str]]] = {}
    cur = await conn.execute(
        _LINKS.format(only=only("l", "item_id"), access=_scope(access, "l")), params
    )
    for item_id, project, domain, origin in await cur.fetchall():
        links.setdefault(item_id, []).append((project, domain, origin))

    evidence: dict[UUID, list[EvidenceFacts]] = {}
    cur = await conn.execute(
        _EVIDENCE.format(only=only("e", "item_id"), access=_scope(access, "e", source="source_id")),
        params,
    )
    for row in await cur.fetchall():
        item_id, source_id, source_kind, record_kind, external_id, stream_key = row[:6]
        project, domains = row[6], row[7]
        evidence.setdefault(item_id, []).append(
            EvidenceFacts(
                source_id=source_id,
                source_kind=source_kind,
                record_kind=record_kind,
                external_id=external_id,
                stream_key=stream_key,
                pushed=None if project is None else (project, tuple(domains or ())),
            )
        )

    # A source is in one workspace: a default is looked up by its source and stream key alone.
    defaults: dict[tuple[UUID, str | None], tuple[str, tuple[str, ...]]] = {}
    cur = await conn.execute(
        _DEFAULTS.format(access=_scope(access, "d", source="source_id")), params
    )
    for source_id, stream_key, project, domains in await cur.fetchall():
        defaults[(source_id, stream_key)] = (project, tuple(domains))

    for item_id, facts in found.items.items():
        found.contexts[item_id] = item_context(
            evidence.get(item_id, ()),
            links.get(item_id, ()),
            defaults,
            found.maps.get(facts.workspace_id, CodeMap()),
            found.names.get(facts.workspace_id, CatalogNames()),
        )
    return found


# --- Links ---------------------------------------------------------------------------------------

# A person's link takes the place of the model's for the same domain; the model's never takes a
# person's.
_ADD_LINKS: Final = """
INSERT INTO knowledge_item_domain (workspace_id, owner_id, item_id, project, domain, origin,
                                   created_by, created_token, assigner)
SELECT i.workspace_id, i.owner_id, i.id, p.project, p.domain, %(origin)s, %(by)s, %(token)s,
       %(assigner)s
FROM knowledge_item i, unnest(%(projects)s::text[], %(domains)s::text[]) AS p (project, domain)
WHERE i.id = %(item)s AND i.workspace_id = %(workspace)s
ON CONFLICT (item_id, project, domain) DO UPDATE
    SET origin = excluded.origin, created_at = now(), created_by = excluded.created_by,
        created_token = excluded.created_token, assigner = NULL
    WHERE knowledge_item_domain.origin = 'model' AND excluded.origin = 'manual'
"""


async def add_links(
    conn: Conn,
    access: WriteAccess,
    *,
    workspace_id: UUID,
    item_id: UUID,
    places: Sequence[tuple[str, str | None]],
    origin: LinkOrigin,
    token_id: UUID | None = None,
    assigner: str | None = None,
) -> int:
    """Link the item to ``places`` with ``origin``, as ``access``'s principal; the rows written.
    A place it is linked to already keeps its link, unless that is the model's and this one a
    person's. ``assigner`` names the model and prompt of a model link."""
    if not places or not access.allows(workspace_id):
        return 0
    if (origin == "model") != (assigner is not None):
        raise ValueError("a model link names its assigner, and only a model link does")
    cur = await conn.execute(
        _ADD_LINKS,
        {
            "origin": origin,
            "by": access.owner_id,
            "token": token_id,
            "assigner": assigner,
            "projects": [project for project, _ in places],
            "domains": [domain for _, domain in places],
            "item": item_id,
            "workspace": workspace_id,
        },
    )
    return max(cur.rowcount, 0)


async def remove_link(
    conn: Conn,
    access: WriteAccess,
    *,
    workspace_id: UUID,
    item_id: UUID,
    project: str,
    domain: str | None,
) -> LinkOrigin | None:
    """Remove the item's link to ``project`` and ``domain`` (``None``: the project's own link);
    the origin it had, or ``None`` when there was none."""
    query = sql.SQL(
        "DELETE FROM knowledge_item_domain l WHERE l.item_id = %s AND l.workspace_id = %s"
        " AND l.project = %s AND l.domain IS NOT DISTINCT FROM %s AND {} RETURNING l.origin"
    ).format(_scope(access, "l"))
    cur = await conn.execute(query, (item_id, workspace_id, project, domain))
    row = await cur.fetchone()
    return None if row is None else row[0]


# --- Defaults of sources and streams -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceDefault:
    """The project and domains of a source's records pushed without their own; of one stream's
    when ``stream`` names it."""

    stream: str | None
    project: str
    domains: tuple[str, ...]
    updated_at: datetime
    updated_by: UUID
    updated_token: UUID | None


_PUT_DEFAULT: Final = """
INSERT INTO source_catalog (workspace_id, owner_id, source_id, stream_key, project, domains,
                            updated_by, updated_token)
VALUES (%(workspace)s, %(owner)s, %(source)s, %(stream)s, %(project)s, %(domains)s, %(owner)s,
        %(token)s)
ON CONFLICT (source_id, stream_key) DO UPDATE
    SET project = excluded.project, domains = excluded.domains, updated_at = now(),
        updated_by = excluded.updated_by, updated_token = excluded.updated_token
    WHERE (source_catalog.project, source_catalog.domains)
          IS DISTINCT FROM (excluded.project, excluded.domains)
"""


async def put_source_default(
    conn: Conn,
    access: WriteAccess,
    *,
    workspace_id: UUID,
    source_id: UUID,
    stream: str | None,
    project: str,
    domains: Sequence[str],
    token_id: UUID | None = None,
) -> bool:
    """Set the default of the source (``stream`` ``None``) or of one of its streams; whether
    anything changed. The names are the caller's to check (:func:`resolve_places`)."""
    if not access.allows(workspace_id):
        return False
    cur = await conn.execute(
        _PUT_DEFAULT,
        {
            "workspace": workspace_id,
            "owner": access.owner_id,
            "source": source_id,
            "stream": stream,
            "project": project,
            "domains": list(domains),
            "token": token_id,
        },
    )
    return cur.rowcount > 0


async def delete_source_default(
    conn: Conn, access: WriteAccess, *, workspace_id: UUID, source_id: UUID, stream: str | None
) -> bool:
    """Remove the default of the source or of one stream; whether there was one."""
    query = sql.SQL(
        "DELETE FROM source_catalog d WHERE d.workspace_id = %s AND d.source_id = %s"
        " AND d.stream_key IS NOT DISTINCT FROM %s AND {}"
    ).format(_scope(access, "d", source="source_id"))
    cur = await conn.execute(query, (workspace_id, source_id, stream))
    return cur.rowcount > 0


async def list_source_defaults(
    conn: Conn, access: Access, *, workspace_id: UUID, source_id: UUID
) -> list[SourceDefault]:
    """The defaults of a source: its own first, then its streams' by key."""
    query = sql.SQL(
        "SELECT d.stream_key, d.project, d.domains, d.updated_at, d.updated_by, d.updated_token"
        " FROM source_catalog d WHERE d.workspace_id = %s AND d.source_id = %s AND {}"
        " ORDER BY d.stream_key NULLS FIRST"
    ).format(_scope(access, "d", source="source_id"))
    cur = await conn.execute(query, (workspace_id, source_id))
    return [
        SourceDefault(row[0], row[1], tuple(row[2]), row[3], row[4], row[5])
        for row in await cur.fetchall()
    ]
