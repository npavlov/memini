"""Scoped decision journal reads shared by MCP and HTTP."""

from typing import Annotated
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from memini import knowledge
from memini.auth import ReadFilter
from memini.decisions.model import DecisionItem, Name
from memini.decisions.service import decision_errors
from memini.provenance import Labelled
from memini.search.model import MAX_FILTER_VALUES, MAX_QUERY_CHARS


class ListDecisionsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaces: Annotated[list[Name], Field(max_length=MAX_FILTER_VALUES)] | None = None
    person_ids: Annotated[list[UUID], Field(max_length=MAX_FILTER_VALUES)] | None = None
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


class DecisionList(Labelled):
    items: list[DecisionItem]


class DecisionChain(Labelled):
    item: DecisionItem
    chain: list[DecisionItem] = Field(description="The full supersession chain, oldest first.")


async def run_list_decisions(
    conn: AsyncConnection[TupleRow], access: ReadFilter, arguments: ListDecisionsArguments
) -> DecisionList:
    with decision_errors():
        items = await knowledge.list_items(
            conn,
            access,
            knowledge.ItemFilters(
                workspaces=None if arguments.workspaces is None else tuple(arguments.workspaces),
                person_ids=None if arguments.person_ids is None else tuple(arguments.person_ids),
                statuses=knowledge.ALL_STATUSES if arguments.history else ("active",),
                query=arguments.query,
                decided_from=arguments.since,
                decided_until=arguments.until,
                limit=arguments.limit,
            ),
        )
    return DecisionList(items=[DecisionItem.of(item) for item in items])


async def run_get_decision(
    conn: AsyncConnection[TupleRow], access: ReadFilter, arguments: GetDecisionArguments
) -> DecisionChain:
    with decision_errors():
        result = await knowledge.get_item(conn, access, arguments.item_id)
    return DecisionChain(
        item=DecisionItem.of(result.item), chain=[DecisionItem.of(i) for i in result.chain]
    )
