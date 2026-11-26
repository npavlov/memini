"""Reading the knowledge layer: one item with its supersession chain, and lists of items.

Every read takes the caller's :class:`~memini.auth.ReadFilter` and puts it into the SQL of each
table it reads. An item the caller may not read raises :class:`~memini.workspaces.NotFoundError`,
as one that does not exist does. The functions here also serve the writes in
:mod:`memini.knowledge.journal`, which read back what they wrote with the caller's
:class:`~memini.auth.WriteAccess`.
"""

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow

from memini.auth import ReadFilter, WriteAccess
from memini.knowledge.model import (
    Evidence,
    Item,
    ItemChain,
    ItemFilters,
    Link,
    Participant,
)
from memini.search.patterns import lexical_terms
from memini.search.retrieval import Params, lexical_statement
from memini.sensitivity import hidden_items
from memini.workspaces import NotFoundError

__all__ = ["get_item", "list_items"]

type Conn = AsyncConnection[TupleRow]
type Access = ReadFilter | WriteAccess

_ITEMS: Final = sql.SQL("""
SELECT i.id, i.workspace_id, w.name, i.kind, i.statement, i.decided_at, i.status,
       i.recorded_at, i.invalidated_at, i.extractor, i.extraction_id
FROM knowledge_item i
JOIN workspace w ON w.id = i.workspace_id
WHERE {access} AND {shown} AND {condition}
ORDER BY {order}
{limit}
""")

# The record a piece of evidence names may be gone (retention, forget, a deleted source); its
# source too. Both are joined only for what they add: the state of the record, the source's name.
# Without the record, the entries of forget and retention (0013) say why it is gone.
_EVIDENCE: Final = sql.SQL("""
SELECT e.item_id, e.record_id, e.source_id, s.name, e.record_kind, e.external_id,
       e.external_url, e.anchor,
       CASE WHEN r.id IS NOT NULL AND r.deleted_at IS NOT NULL THEN 'deleted'
            WHEN r.id IS NOT NULL AND r.sensitive AND NOT {include_sensitive} THEN 'sensitive'
            WHEN r.id IS NOT NULL THEN 'stored'
            WHEN f.record_id IS NOT NULL THEN 'forgotten'
            WHEN x.record_id IS NOT NULL THEN 'expired'
            ELSE 'gone' END
FROM knowledge_evidence e
LEFT JOIN record r ON r.id = e.record_id AND r.workspace_id = e.workspace_id AND {record_access}
LEFT JOIN source s ON s.id = e.source_id AND s.workspace_id = e.workspace_id AND {source_access}
LEFT JOIN forgotten_record f
    ON f.record_id = e.record_id AND f.workspace_id = e.workspace_id AND {forgotten_access}
LEFT JOIN expired_record x
    ON x.record_id = e.record_id AND x.workspace_id = e.workspace_id AND {expired_access}
WHERE e.item_id = ANY(%s) AND {access}
ORDER BY e.item_id, e.id
""")

_PARTICIPANTS: Final = sql.SQL("""
SELECT p.item_id, p.person_id, pe.display_name
FROM knowledge_participant p
JOIN person pe ON pe.id = p.person_id
WHERE p.item_id = ANY(%s) AND {access} AND {person_access}
ORDER BY p.item_id, pe.display_name, p.person_id
""")

_LINKS: Final = sql.SQL("""
SELECT s.item_id, s.supersedes_id, s.relation, s.created_at
FROM knowledge_supersession s
WHERE (s.item_id = ANY(%s) OR s.supersedes_id = ANY(%s)) AND {access}
ORDER BY s.created_at, s.item_id, s.supersedes_id
""")

# Every item linked to the start, in either direction. UNION drops an item seen before, so the
# walk ends; there are no cycles anyway (the database refuses them).
_CHAIN: Final = sql.SQL("""
WITH RECURSIVE chain (id) AS (
    SELECT %s::uuid
    UNION
    SELECT CASE WHEN s.item_id = c.id THEN s.supersedes_id ELSE s.item_id END
    FROM chain c
    JOIN knowledge_supersession s ON (s.item_id = c.id OR s.supersedes_id = c.id) AND {access}
)
SELECT id FROM chain
""")


def _scope(access: Access, alias: str, *, source: str | None = None) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
        source_id=None if source is None else sql.Identifier(alias, source),
    )


@asynccontextmanager
async def _snapshot(conn: Conn) -> AsyncGenerator[None]:
    """One snapshot for the statements of a read, when the caller has no transaction open: a
    read-only transaction at ``REPEATABLE READ``. Inside the caller's transaction, its own."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        yield
        return
    async with conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield


@dataclass(slots=True)
class _Parts:
    evidence: list[Evidence] = field(default_factory=list[Evidence])
    participants: list[Participant] = field(default_factory=list[Participant])
    supersedes: list[Link] = field(default_factory=list[Link])
    superseded_by: Link | None = None


async def select_items(
    conn: Conn,
    access: Access,
    condition: sql.Composable,
    params: Mapping[str, Any],
    *,
    order: sql.Composable,
    limit: int | None = None,
) -> list[Item]:
    """The items that ``access`` may read and that meet ``condition`` (over the alias ``i`` of
    knowledge_item and ``w`` of workspace, with named parameters), complete with their evidence,
    participants and links."""
    query = _ITEMS.format(
        access=_scope(access, "i"),
        # An item whose every piece of evidence is a sensitive record is left out.
        shown=hidden_items(access),
        condition=condition,
        order=order,
        limit=sql.SQL("") if limit is None else sql.SQL("LIMIT {}").format(sql.Literal(limit)),
    )
    cur = await conn.execute(query, params)
    rows = await cur.fetchall()
    if not rows:
        return []
    ids = [row[0] for row in rows]
    parts = {item_id: _Parts() for item_id in ids}

    cur = await conn.execute(
        _EVIDENCE.format(
            include_sensitive=sql.Literal(
                not isinstance(access, ReadFilter) or access.include_sensitive
            ),
            record_access=_scope(access, "r", source="source_id"),
            source_access=_scope(access, "s", source="id"),
            forgotten_access=_scope(access, "f", source="source_id"),
            expired_access=_scope(access, "x", source="source_id"),
            access=_scope(access, "e", source="source_id"),
        ),
        (ids,),
    )
    for row in await cur.fetchall():
        parts[row[0]].evidence.append(
            Evidence(
                record_id=row[1],
                source_id=row[2],
                source=row[3],
                record_kind=row[4],
                external_id=row[5],
                external_url=row[6],
                anchor=row[7],
                record_state=row[8],
            )
        )

    cur = await conn.execute(
        _PARTICIPANTS.format(access=_scope(access, "p"), person_access=_scope(access, "pe")),
        (ids,),
    )
    for item_id, person_id, display_name in await cur.fetchall():
        parts[item_id].participants.append(Participant(person_id, display_name))

    cur = await conn.execute(_LINKS.format(access=_scope(access, "s")), (ids, ids))
    for item_id, supersedes_id, relation, created_at in await cur.fetchall():
        link = Link(item_id, supersedes_id, relation, created_at)
        if item_id in parts:
            parts[item_id].supersedes.append(link)
        if supersedes_id in parts:
            parts[supersedes_id].superseded_by = link

    return [_item(row, parts[row[0]]) for row in rows]


def _item(row: Sequence[Any], parts: _Parts) -> Item:
    return Item(
        id=row[0],
        workspace_id=row[1],
        workspace=row[2],
        kind=row[3],
        statement=row[4],
        decided_at=row[5],
        status=row[6],
        recorded_at=row[7],
        invalidated_at=row[8],
        extractor=row[9],
        extraction_id=row[10],
        evidence=tuple(parts.evidence),
        participants=tuple(parts.participants),
        supersedes=tuple(parts.supersedes),
        superseded_by=parts.superseded_by,
    )


_OLDEST_FIRST: Final = sql.SQL("i.decided_at, i.recorded_at, i.id")
_NEWEST_FIRST: Final = sql.SQL("i.decided_at DESC, i.recorded_at DESC, i.id DESC")


async def load_item(conn: Conn, access: Access, item_id: UUID) -> Item:
    """The item ``item_id`` if ``access`` may read it.

    Raises:
        NotFoundError: there is no such item, or ``access`` may not read it.
    """
    items = await select_items(
        conn, access, sql.SQL("i.id = %(id)s"), {"id": item_id}, order=_OLDEST_FIRST
    )
    if not items:
        raise NotFoundError("knowledge item")
    return items[0]


async def get_item(conn: Conn, access: ReadFilter, item_id: UUID) -> ItemChain:
    """The item ``item_id`` with its evidence, participants and supersession chain: every item
    linked to it directly or through others, in either direction, oldest first.

    Evidence whose record is no longer stored keeps its reference and says so
    (:attr:`~memini.knowledge.Evidence.record_state`).

    Raises:
        NotFoundError: there is no such item, or ``access`` may not read it.
    """
    async with _snapshot(conn):
        item = await load_item(conn, access, item_id)
        cur = await conn.execute(_CHAIN.format(access=_scope(access, "s")), (item_id,))
        chain_ids = [row[0] for row in await cur.fetchall()]
        if len(chain_ids) == 1:
            return ItemChain(item=item, chain=(item,))
        chain = await select_items(
            conn, access, sql.SQL("i.id = ANY(%(ids)s)"), {"ids": chain_ids}, order=_OLDEST_FIRST
        )
    current = next((each for each in chain if each.id == item_id), item)
    return ItemChain(item=current, chain=tuple(chain))


async def list_items(
    conn: Conn, access: ReadFilter, filters: ItemFilters | None = None
) -> list[Item]:
    """The items ``access`` may read that meet ``filters``, newest first (by ``decided_at``).

    The default is the active items of every workspace the caller may read; history is
    ``ItemFilters(statuses=ALL_STATUSES)``. Each item comes with its evidence, participants and
    links, so a superseded item in the history names the item that replaced it.
    """
    chosen = ItemFilters() if filters is None else filters
    conditions: list[sql.Composable] = [sql.SQL("i.status = ANY(%(statuses)s)")]
    params: dict[str, Any] = {"statuses": list(chosen.statuses)}
    order = _NEWEST_FIRST
    if chosen.query is not None:
        if not lexical_terms(chosen.query):
            return []
        terms = Params()
        document = lexical_statement.document(sql.SQL("i.statement"))
        query = lexical_statement.tsquery(chosen.query, terms)
        conditions.append(sql.SQL("{} @@ {}").format(document, query))
        order = sql.SQL("ts_rank({}, {}) DESC, {}").format(document, query, order)
        params.update(terms.values)
    if chosen.workspaces is not None:
        conditions.append(sql.SQL("(w.name = ANY(%(names)s) OR w.id = ANY(%(ids)s))"))
        params["names"] = [each for each in chosen.workspaces if isinstance(each, str)]
        params["ids"] = [each for each in chosen.workspaces if isinstance(each, UUID)]
    if chosen.kinds is not None:
        conditions.append(sql.SQL("i.kind = ANY(%(kinds)s)"))
        params["kinds"] = list(chosen.kinds)
    if chosen.sources is not None or chosen.repositories is not None:
        source_conditions = [
            _scope(access, "s", source="id"),
            _scope(access, "e", source="source_id"),
        ]
        if chosen.sources is not None:
            source_conditions.append(sql.SQL("s.name = ANY(%(sources)s)"))
            params["sources"] = list(chosen.sources)
        if chosen.repositories is not None:
            source_conditions.append(
                sql.SQL("s.kind = 'repository' AND s.name = ANY(%(repositories)s)")
            )
            params["repositories"] = list(chosen.repositories)
        conditions.append(
            sql.SQL(
                "EXISTS (SELECT 1 FROM knowledge_evidence e JOIN source s ON s.id = e.source_id "
                "WHERE e.item_id = i.id AND {})"
            ).format(sql.SQL(" AND ").join(source_conditions))
        )
    if chosen.person_ids is not None:
        # A person stands for everyone merged with it (memini.persons): its participations and
        # theirs.
        conditions.append(
            sql.SQL(
                "EXISTS (SELECT 1 FROM knowledge_participant p"
                " WHERE p.item_id = i.id AND p.person_id IN ("
                "   SELECT person_members(person_root(x.id))"
                "   FROM unnest(%(persons)s::uuid[]) AS x (id)))"
            )
        )
        params["persons"] = list(chosen.person_ids)
    if chosen.record_ids is not None:
        conditions.append(
            sql.SQL(
                "EXISTS (SELECT 1 FROM knowledge_evidence e"
                " WHERE e.item_id = i.id AND e.record_id = ANY(%(records)s))"
            )
        )
        params["records"] = list(chosen.record_ids)
    if chosen.item_ids is not None:
        conditions.append(sql.SQL("i.id = ANY(%(item_ids)s)"))
        params["item_ids"] = list(chosen.item_ids)
    if chosen.decided_from is not None:
        conditions.append(sql.SQL("i.decided_at >= %(decided_from)s"))
        params["decided_from"] = chosen.decided_from
    if chosen.decided_until is not None:
        conditions.append(sql.SQL("i.decided_at < %(decided_until)s"))
        params["decided_until"] = chosen.decided_until
    async with _snapshot(conn):
        return await select_items(
            conn,
            access,
            sql.SQL(" AND ").join(conditions),
            params,
            order=order,
            limit=chosen.limit,
        )
