"""Scoped decision journal reads shared by MCP and HTTP.

Every item read carries its ``context``: the project and domains of the catalog it belongs to and
how each came (:mod:`memini.decisions.context`). ``list_decisions`` filters by ``projects`` and
``domains`` (a domain includes its subdomains) and, without a query, lists newest first. With a
project or domain filter, the answer counts for each project named (or of each domain named) its
items in the statuses asked for that have no domain of it (``without_domain``): a list by domain
leaves them out, so the caller knows that it may be incomplete.
"""

from typing import Annotated
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from memini import knowledge
from memini.auth import ReadFilter
from memini.decisions import context
from memini.decisions.model import DecisionItem, Name
from memini.decisions.service import decision_errors
from memini.provenance import Labelled
from memini.search.model import MAX_FILTER_VALUES, MAX_QUERY_CHARS
from memini.workspaces import list_workspaces

__all__ = [
    "DecisionChain",
    "DecisionList",
    "GetDecisionArguments",
    "ListDecisionsArguments",
    "WithoutDomain",
    "domain_decisions",
    "run_get_decision",
    "run_list_decisions",
]


class ListDecisionsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaces: Annotated[list[Name], Field(max_length=MAX_FILTER_VALUES)] | None = None
    person_ids: Annotated[list[UUID], Field(max_length=MAX_FILTER_VALUES)] | None = None
    projects: Annotated[list[Name], Field(max_length=MAX_FILTER_VALUES)] | None = Field(
        default=None,
        description="Only items under one of these projects of the catalog: their project, or"
        " a domain of theirs, is one of them.",
    )
    domains: Annotated[list[Name], Field(max_length=MAX_FILTER_VALUES)] | None = Field(
        default=None,
        description="Only items in one of these domains: domain or domain/subdomain; a domain"
        " includes its subdomains. With projects, the domains of those projects.",
    )
    query: str | None = Field(default=None, min_length=1, max_length=MAX_QUERY_CHARS)
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    history: bool = Field(
        default=False, description="Include superseded, closed and retracted items."
    )
    limit: int = Field(default=50, ge=1, le=knowledge.MAX_LIST_LIMIT)


class GetDecisionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: UUID


class WithoutDomain(BaseModel):
    project: str
    items: int = Field(
        description="Items of the project, in the statuses asked for, that no domain of it"
        " holds: a list by domain does not show them."
    )


class DecisionList(Labelled):
    items: list[DecisionItem]
    without_domain: list[WithoutDomain] = Field(
        default_factory=list[WithoutDomain],
        description="With a projects or domains filter: for each project named, or of each"
        " domain named, how many of its items have no domain.",
    )


class DecisionChain(Labelled):
    item: DecisionItem
    chain: list[DecisionItem] = Field(description="The full supersession chain, oldest first.")


async def _workspace_ids(
    conn: AsyncConnection[TupleRow], access: ReadFilter, names: list[str] | None
) -> list[UUID]:
    spaces = await list_workspaces(conn, access)
    return [each.id for each in spaces if names is None or each.name in names]


async def run_list_decisions(
    conn: AsyncConnection[TupleRow], access: ReadFilter, arguments: ListDecisionsArguments
) -> DecisionList:
    statuses = knowledge.ALL_STATUSES if arguments.history else ("active",)
    filtered = arguments.projects is not None or arguments.domains is not None
    with decision_errors():
        async with conn.transaction():
            workspace_ids = await _workspace_ids(conn, access, arguments.workspaces)
            item_ids: tuple[UUID, ...] | None = None
            counts: dict[str, int] = {}
            found: context.Contexts | None = None
            if filtered:
                found = await context.load_contexts(conn, access, workspace_ids)
                item_ids = tuple(
                    found.select(
                        projects=arguments.projects, domains=arguments.domains, statuses=statuses
                    )
                )
                named = set(arguments.projects or ())
                if arguments.domains is not None:
                    named |= {
                        project
                        for project, _ in found.domain_targets(
                            arguments.domains,
                            None if arguments.projects is None else set(arguments.projects),
                        )
                    }
                counts = found.without_domain(named, statuses=statuses)
            items = await knowledge.list_items(
                conn,
                access,
                knowledge.ItemFilters(
                    workspaces=None
                    if arguments.workspaces is None
                    else tuple(arguments.workspaces),
                    person_ids=None
                    if arguments.person_ids is None
                    else tuple(arguments.person_ids),
                    statuses=statuses,
                    query=arguments.query,
                    decided_from=arguments.since,
                    decided_until=arguments.until,
                    limit=arguments.limit,
                    item_ids=item_ids,
                ),
            )
            if found is None:
                found = await context.load_contexts(
                    conn,
                    access,
                    {item.workspace_id for item in items},
                    item_ids=[item.id for item in items],
                )
    return DecisionList(
        items=[DecisionItem.of(item, found.context(item.id)) for item in items],
        without_domain=[
            WithoutDomain(project=project, items=count) for project, count in counts.items()
        ],
    )


async def run_get_decision(
    conn: AsyncConnection[TupleRow], access: ReadFilter, arguments: GetDecisionArguments
) -> DecisionChain:
    with decision_errors():
        async with conn.transaction():
            result = await knowledge.get_item(conn, access, arguments.item_id)
            found = await context.load_contexts(
                conn,
                access,
                {each.workspace_id for each in result.chain},
                item_ids=[each.id for each in result.chain],
            )
    return DecisionChain(
        item=DecisionItem.of(result.item, found.context(result.item.id)),
        chain=[DecisionItem.of(each, found.context(each.id)) for each in result.chain],
    )


async def domain_decisions(
    conn: AsyncConnection[TupleRow],
    access: ReadFilter,
    *,
    workspace_id: UUID,
    project: str,
    domain: str,
    limit: int = 20,
) -> tuple[list[DecisionItem], int]:
    """The active items in ``domain`` of ``project`` (its subdomains included), newest first,
    at most ``limit``, and how many there are: what ``get_domain`` shows. In the caller's
    transaction."""
    found = await context.load_contexts(conn, access, [workspace_id])
    wanted = [(project, domain)]
    ids = [
        item_id
        for item_id, facts in found.items.items()
        if facts.status == "active" and found.context(item_id).in_domains(wanted)
    ]
    if not ids:
        return [], 0
    items = await knowledge.list_items(
        conn,
        access,
        knowledge.ItemFilters(
            workspaces=(workspace_id,), item_ids=tuple(ids), limit=min(limit, len(ids))
        ),
    )
    return [DecisionItem.of(item, found.context(item.id)) for item in items], len(ids)
