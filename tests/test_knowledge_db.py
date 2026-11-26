"""The knowledge layer against a real Postgres: the append-only rules the database enforces, the
supersession links, extraction idempotency, evidence that outlives its record, and isolation.

Run with `make test-integration`. Every record, person and statement here is synthetic.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString
from uuid import UUID

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow

from memini.auth import Principal, ReadFilter, WriteAccess, authorize_read
from memini.db import connect
from memini.ids import RecordKind, record_id
from memini.knowledge import (
    ALL_STATUSES,
    MANUAL,
    EvidenceRef,
    InvalidItemError,
    Item,
    ItemFilters,
    ItemKind,
    NewItem,
    NotActiveError,
    NotFoundError,
    SupersedesRef,
    SupersessionError,
    close_item,
    get_item,
    list_items,
    record_extraction,
    record_item,
    retract_item,
    supersede,
)
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

TOKEN_ID = UUID("00000000-0000-7000-8000-00000000c0de")
MONDAY = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
COMMIT = "4f" * 20
EXTRACTOR = "test-model@decisions-v1"


@dataclass(frozen=True)
class World:
    """Workspaces alpha and beta of one owner, with records and persons in each."""

    owner: UUID
    alpha: UUID
    beta: UUID
    chat: UUID  # push source in alpha
    repo: UUID  # git source in alpha
    thread_a: str  # a thread in alpha where something is decided
    thread_b: str  # a later thread in alpha that reverses it
    meeting: str
    code: str
    document: str
    thread_beta: str
    me: UUID
    colleague: UUID
    stranger: UUID  # a person of beta

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)

    @property
    def reader(self) -> ReadFilter:
        return authorize_read(principal(self.owner))


def principal(owner: UUID, workspace_id: UUID | None = None) -> Principal:
    return Principal(
        principal_id=owner,
        token_id=TOKEN_ID,
        scopes=frozenset({"read", "ingest"}),
        workspace_id=workspace_id,
    )


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    """A connection to a migrated database, in autocommit: each statement is committed."""
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


async def one(conn: Conn, query: Any, params: Sequence[Any] = ()) -> Any:
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def add_source(conn: Conn, owner: UUID, workspace: UUID, name: str, kind: str) -> UUID:
    return await one(
        conn,
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        (workspace, owner, name, kind),
    )


async def add_record(
    conn: Conn,
    owner: UUID,
    workspace: UUID,
    source: UUID,
    kind: RecordKind,
    external_id: str,
) -> str:
    rid = record_id(source, kind, external_id)
    await conn.execute(
        "INSERT INTO record (id, workspace_id, owner_id, source_id, kind, external_id,"
        " external_url, content_hash, occurred_at, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            rid,
            workspace,
            owner,
            source,
            kind,
            external_id,
            f"https://chat.example.test/{external_id}",
            "c0" * 32,
            MONDAY,
            None if kind == "code_file" else b'{"text": "synthetic"}',
        ),
    )
    return rid


async def add_person(conn: Conn, owner: UUID, workspace: UUID, name: str, **extra: Any) -> UUID:
    return await one(
        conn,
        "INSERT INTO person (workspace_id, owner_id, display_name, principal_id)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (workspace, owner, name, extra.get("principal_id")),
    )


@pytest.fixture
async def world(db: Conn) -> World:
    owner = await ensure_principal(db, "test-owner")
    alpha = (await create_workspace(db, owner_id=owner, name="alpha")).id
    beta = (await create_workspace(db, owner_id=owner, name="beta")).id
    chat = await add_source(db, owner, alpha, "chat", "push")
    repo = await add_source(db, owner, alpha, "repo", "repository")
    chat_beta = await add_source(db, owner, beta, "chat", "push")
    return World(
        owner=owner,
        alpha=alpha,
        beta=beta,
        chat=chat,
        repo=repo,
        thread_a=await add_record(db, owner, alpha, chat, "thread", "garden/1700000000.000100"),
        thread_b=await add_record(db, owner, alpha, chat, "thread", "garden/1700500000.000200"),
        meeting=await add_record(db, owner, alpha, chat, "meeting", "meeting-weekly-07"),
        code=await add_record(db, owner, alpha, repo, "code_file", "refs/heads/main:src/pump.py"),
        document=await add_record(db, owner, alpha, chat, "document", "notes/pump.md"),
        thread_beta=await add_record(db, owner, beta, chat_beta, "thread", "kiln/1700000000.1"),
        me=await add_person(db, owner, alpha, "Person One", principal_id=owner),
        colleague=await add_person(db, owner, alpha, "Person Two"),
        stranger=await add_person(db, owner, beta, "Person Three"),
    )


def decision(
    statement: str,
    record: str,
    anchor: str | None = "m1",
    *,
    days: int = 0,
    kind: ItemKind = "decision",
    participants: Sequence[UUID] = (),
    supersedes: Sequence[SupersedesRef] = (),
) -> NewItem:
    return NewItem(
        kind=kind,
        statement=statement,
        decided_at=MONDAY + timedelta(days=days),
        evidence=(EvidenceRef(record, anchor),),
        participants=participants,
        supersedes=supersedes,
    )


async def record(db: Conn, w: World, new: NewItem) -> Item:
    return await record_item(db, w.access, w.alpha, new)


async def insert_raw(
    db: Conn,
    w: World,
    record: str,
    anchor: str | None,
    *,
    extraction: tuple[str, UUID] | None = None,
) -> UUID:
    """An item and one piece of evidence written in SQL, as any client could, in one transaction.
    ``extraction``: the extractor and the extraction the item names."""
    extractor, extraction_id = (MANUAL, None) if extraction is None else extraction
    async with db.transaction():
        item_id = await one(
            db,
            "INSERT INTO knowledge_item (workspace_id, owner_id, kind, statement, decided_at,"
            " extractor, extraction_id)"
            " VALUES (%s, %s, 'decision', 'Water at six.', %s, %s, %s) RETURNING id",
            (w.alpha, w.owner, MONDAY, extractor, extraction_id),
        )
        await db.execute(
            "INSERT INTO knowledge_evidence (workspace_id, owner_id, item_id, record_id, anchor)"
            " VALUES (%s, %s, %s, %s, %s)",
            (w.alpha, w.owner, item_id, record, anchor),
        )
    return item_id


async def row_without_status(db: Conn, item_id: UUID) -> Any:
    return await one(
        db,
        "SELECT to_jsonb(i) - 'status' - 'invalidated_at' FROM knowledge_item i WHERE id = %s",
        (item_id,),
    )


async def count(db: Conn, table: str, workspace: UUID) -> int:
    queries: dict[str, LiteralString] = {
        "knowledge_item": "SELECT count(*) FROM knowledge_item WHERE workspace_id = %s",
        "knowledge_evidence": "SELECT count(*) FROM knowledge_evidence WHERE workspace_id = %s",
        "knowledge_participant": (
            "SELECT count(*) FROM knowledge_participant WHERE workspace_id = %s"
        ),
        "knowledge_supersession": (
            "SELECT count(*) FROM knowledge_supersession WHERE workspace_id = %s"
        ),
        "knowledge_extraction": (
            "SELECT count(*) FROM knowledge_extraction WHERE workspace_id = %s"
        ),
    }
    return int(await one(db, queries[table], (workspace,)))


KNOWLEDGE_TABLES = (
    "knowledge_item",
    "knowledge_evidence",
    "knowledge_participant",
    "knowledge_supersession",
    "knowledge_extraction",
)


# --- The database refuses edits ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("statement", "The pump runs at night."),
        ("kind", "open_question"),
        ("decided_at", datetime(2020, 1, 1, tzinfo=UTC)),
        ("extractor", "another-model@v9"),
        ("recorded_at", datetime(2020, 1, 1, tzinfo=UTC)),
        ("owner_id", None),
    ],
)
async def test_a_stored_item_cannot_be_edited(
    db: Conn, world: World, column: str, value: object
) -> None:
    item = await record(db, world, decision("The pump runs in the morning.", world.thread_a))
    queries: dict[str, LiteralString] = {
        "statement": "UPDATE knowledge_item SET statement = %s WHERE id = %s",
        "kind": "UPDATE knowledge_item SET kind = %s WHERE id = %s",
        "decided_at": "UPDATE knowledge_item SET decided_at = %s WHERE id = %s",
        "extractor": "UPDATE knowledge_item SET extractor = %s WHERE id = %s",
        "recorded_at": "UPDATE knowledge_item SET recorded_at = %s WHERE id = %s",
        "owner_id": "UPDATE knowledge_item SET owner_id = coalesce(%s, owner_id) WHERE id = %s",
    }

    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(queries[column], (value, item.id))

    # owner_id set to itself is no change of content but still an update that is not a transition.
    expected = (
        "knowledge_item_status_transition" if column == "owner_id" else "knowledge_item_append_only"
    )
    assert refused.value.diag.constraint_name == expected
    stored = await get_item(db, world.reader, item.id)
    assert stored.item.statement == "The pump runs in the morning."
    assert stored.item.status == "active"


async def test_an_item_is_never_deleted_or_truncated(db: Conn, world: World) -> None:
    item = await record(db, world, decision("The pump runs in the morning.", world.thread_a))

    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute("DELETE FROM knowledge_item WHERE id = %s", (item.id,))
    assert refused.value.diag.constraint_name == "knowledge_item_append_only"

    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute("TRUNCATE knowledge_item CASCADE")
    assert (refused.value.diag.constraint_name or "").endswith("_append_only")

    assert await count(db, "knowledge_item", world.alpha) == 1


async def test_evidence_participants_links_and_extractions_never_change(
    db: Conn, world: World
) -> None:
    older = await record(
        db, world, decision("Water at six.", world.thread_a, participants=[world.me])
    )
    await record(
        db,
        world,
        decision("Water at seven.", world.thread_b, days=3, supersedes=[SupersedesRef(older.id)]),
    )
    await record_extraction(
        db,
        world.access,
        record_id=world.meeting,
        content_hash="c0" * 32,
        extractor=EXTRACTOR,
        items=[],
    )
    changes: dict[str, LiteralString] = {
        "knowledge_evidence": "UPDATE knowledge_evidence SET anchor = 'm9'",
        "knowledge_participant": "UPDATE knowledge_participant SET created_at = now()",
        "knowledge_supersession": "UPDATE knowledge_supersession SET relation = 'answers'",
        "knowledge_extraction": "UPDATE knowledge_extraction SET content_hash = 'other'",
    }
    for table, update in changes.items():
        with pytest.raises(errors.CheckViolation) as refused:
            await db.execute(update)
        assert refused.value.diag.constraint_name == f"{table}_append_only", table

    deletes: dict[str, LiteralString] = {
        "knowledge_evidence": "DELETE FROM knowledge_evidence",
        "knowledge_participant": "DELETE FROM knowledge_participant",
        "knowledge_supersession": "DELETE FROM knowledge_supersession",
        "knowledge_extraction": "DELETE FROM knowledge_extraction",
    }
    for table, delete in deletes.items():
        with pytest.raises(errors.CheckViolation) as refused:
            await db.execute(delete)
        assert refused.value.diag.constraint_name == f"{table}_append_only", table

    for table in KNOWLEDGE_TABLES:
        with pytest.raises(errors.CheckViolation) as refused:
            await db.execute(sql.SQL("TRUNCATE {} CASCADE").format(sql.Identifier(table)))
        assert (refused.value.diag.constraint_name or "").endswith("_append_only"), table


async def test_evidence_and_participants_are_written_with_their_item(
    db: Conn, world: World
) -> None:
    item = await record(db, world, decision("Water at six.", world.thread_a))

    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(
            "INSERT INTO knowledge_evidence (workspace_id, owner_id, item_id, record_id, anchor)"
            " VALUES (%s, %s, %s, %s, 'm2')",
            (world.alpha, world.owner, item.id, world.thread_b),
        )
    assert refused.value.diag.constraint_name == "knowledge_evidence_append_only"

    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(
            "INSERT INTO knowledge_participant (workspace_id, owner_id, item_id, person_id)"
            " VALUES (%s, %s, %s, %s)",
            (world.alpha, world.owner, item.id, world.colleague),
        )
    assert refused.value.diag.constraint_name == "knowledge_participant_append_only"


async def test_an_item_without_evidence_is_not_stored(db: Conn, world: World) -> None:
    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(
            "INSERT INTO knowledge_item (workspace_id, owner_id, kind, statement, decided_at)"
            " VALUES (%s, %s, 'decision', 'Water at six.', %s)",
            (world.alpha, world.owner, MONDAY),
        )
    assert refused.value.diag.constraint_name == "knowledge_item_evidence_check"

    with pytest.raises(InvalidItemError) as invalid:
        await record(db, world, NewItem("decision", "Water at six.", MONDAY, evidence=()))
    assert invalid.value.field == "evidence"
    assert await count(db, "knowledge_item", world.alpha) == 0


async def test_the_database_keeps_the_times(db: Conn, world: World) -> None:
    async with db.transaction():
        now = await one(db, "SELECT now()")
        item_id = await one(
            db,
            "INSERT INTO knowledge_item"
            " (workspace_id, owner_id, kind, statement, decided_at, recorded_at)"
            " VALUES (%s, %s, 'decision', 'Water at six.', %s, '2000-01-01') RETURNING id",
            (world.alpha, world.owner, MONDAY),
        )
        await db.execute(
            "INSERT INTO knowledge_evidence (workspace_id, owner_id, item_id, record_id, anchor)"
            " VALUES (%s, %s, %s, %s, 'm1')",
            (world.alpha, world.owner, item_id, world.thread_a),
        )
    assert await one(db, "SELECT recorded_at FROM knowledge_item WHERE id = %s", (item_id,)) == now

    async with db.transaction():
        now = await one(db, "SELECT now()")
        await db.execute(
            "UPDATE knowledge_item SET status = 'retracted', invalidated_at = '2000-01-01'"
            " WHERE id = %s",
            (item_id,),
        )
    assert (
        await one(db, "SELECT invalidated_at FROM knowledge_item WHERE id = %s", (item_id,)) == now
    )


async def test_status_changes_once_and_only_from_active(db: Conn, world: World) -> None:
    item = await record(db, world, decision("Water at six.", world.thread_a))

    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(
            "INSERT INTO knowledge_item (workspace_id, owner_id, kind, statement, decided_at,"
            " status, invalidated_at) VALUES (%s, %s, 'decision', 'Water.', %s, 'closed', now())",
            (world.alpha, world.owner, MONDAY),
        )
    assert refused.value.diag.constraint_name == "knowledge_item_status_transition"

    # Superseded only through a link from the item that replaces it.
    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(
            "UPDATE knowledge_item SET status = 'superseded' WHERE id = %s", (item.id,)
        )
    assert refused.value.diag.constraint_name == "knowledge_item_status_transition"

    closed = await close_item(db, world.access, item.id)
    assert closed.status == "closed"
    assert closed.invalidated_at is not None

    for status in ("active", "retracted", "superseded"):
        with pytest.raises(errors.CheckViolation) as refused:
            await db.execute(
                "UPDATE knowledge_item SET status = %s WHERE id = %s", (status, item.id)
            )
        assert refused.value.diag.constraint_name == "knowledge_item_status_transition"
    with pytest.raises(NotActiveError) as not_active:
        await retract_item(db, world.access, item.id)
    assert (not_active.value.item_id, not_active.value.status) == (item.id, "closed")


async def test_retract_and_close(db: Conn, world: World) -> None:
    wrong = await record(db, world, decision("The pump is red.", world.thread_a))
    question = await record(
        db, world, decision("Which pump do we buy?", world.thread_a, "m2", kind="open_question")
    )

    retracted = await retract_item(db, world.access, wrong.id)
    closed = await close_item(db, world.access, question.id)

    assert (retracted.status, closed.status) == ("retracted", "closed")
    assert retracted.invalidated_at is not None
    assert retracted.invalidated_at >= wrong.recorded_at
    assert await list_items(db, world.reader) == []
    history = await list_items(db, world.reader, ItemFilters(statuses=ALL_STATUSES))
    assert {item.id: item.status for item in history} == {
        wrong.id: "retracted",
        question.id: "closed",
    }
    with pytest.raises(NotFoundError):
        await close_item(db, world.access, UUID(int=1))


# --- Supersession --------------------------------------------------------------------------------


async def test_supersede_invalidates_the_old_item_and_links_from_the_new_one(
    db: Conn, world: World
) -> None:
    old = await record(
        db, world, decision("Water at six.", world.thread_a, participants=[world.me])
    )
    before = await row_without_status(db, old.id)

    new = await record(
        db,
        world,
        decision(
            "Water at seven instead.",
            world.thread_b,
            "m4",
            days=3,
            participants=[world.me, world.colleague],
            supersedes=[SupersedesRef(old.id)],
        ),
    )

    stored = (await get_item(db, world.reader, old.id)).item
    assert stored.status == "superseded"
    assert stored.invalidated_at == new.recorded_at  # one transaction: the new item's time
    # The old item is untouched but for its status: the link lives on the new item only.
    assert await row_without_status(db, old.id) == before
    assert stored.supersedes == ()
    assert new.status == "active"
    assert [(link.item_id, link.supersedes_id, link.relation) for link in new.supersedes] == [
        (new.id, old.id, "replaces")
    ]
    assert stored.superseded_by == new.supersedes[0]
    links = await (
        await db.execute("SELECT item_id, supersedes_id, relation FROM knowledge_supersession")
    ).fetchall()
    assert links == [(new.id, old.id, "replaces")]


async def test_supersede_links_two_stored_items(db: Conn, world: World) -> None:
    old = await record(db, world, decision("Water at six.", world.thread_a))
    new = await record(db, world, decision("Water at seven.", world.thread_b, days=3))

    linked = await supersede(db, world.access, new.id, [SupersedesRef(old.id)])

    stored = (await get_item(db, world.reader, old.id)).item
    assert stored.status == "superseded"
    assert stored.invalidated_at is not None
    assert stored.invalidated_at >= new.recorded_at
    assert [link.supersedes_id for link in linked.supersedes] == [old.id]


async def test_cycles_are_refused(db: Conn, world: World) -> None:
    a = await record(db, world, decision("Water at six.", world.thread_a))
    b = await record(db, world, decision("Water at six, said again.", world.thread_a, "m2"))

    with pytest.raises(SupersessionError) as refused:
        await supersede(db, world.access, a.id, [SupersedesRef(a.id)])
    assert refused.value.rule == "cycle"

    await supersede(db, world.access, b.id, [SupersedesRef(a.id)])
    with pytest.raises(NotActiveError) as not_active:
        await supersede(db, world.access, a.id, [SupersedesRef(b.id)])
    assert (not_active.value.item_id, not_active.value.status) == (a.id, "superseded")


async def test_the_database_refuses_a_cycle_in_one_statement(db: Conn, world: World) -> None:
    a = await record(db, world, decision("Water at six.", world.thread_a))
    b = await record(db, world, decision("Water at six, said again.", world.thread_a, "m2"))

    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(
            "INSERT INTO knowledge_supersession"
            " (workspace_id, owner_id, item_id, supersedes_id, relation)"
            " VALUES (%(ws)s, %(owner)s, %(a)s, %(b)s, 'replaces'),"
            " (%(ws)s, %(owner)s, %(b)s, %(a)s, 'replaces')",
            {"ws": world.alpha, "owner": world.owner, "a": a.id, "b": b.id},
        )
    assert refused.value.diag.constraint_name == "knowledge_supersession_cycle_check"
    assert {item.status for item in await list_items(db, world.reader)} == {"active"}


async def test_only_active_items_take_part_in_a_supersession(db: Conn, world: World) -> None:
    wrong = await record(db, world, decision("The pump is red.", world.thread_a))
    old = await record(db, world, decision("Water at six.", world.thread_a, "m2"))
    new = await record(db, world, decision("Water at seven.", world.thread_b, days=3))
    newest = await record(db, world, decision("Water at eight.", world.thread_b, "m5", days=5))
    await retract_item(db, world.access, wrong.id)
    await supersede(db, world.access, new.id, [SupersedesRef(old.id)])

    with pytest.raises(NotActiveError) as refused:
        await supersede(db, world.access, newest.id, [SupersedesRef(wrong.id)])
    assert refused.value.status == "retracted"
    with pytest.raises(NotActiveError) as refused:
        await supersede(db, world.access, newest.id, [SupersedesRef(old.id)])
    assert refused.value.status == "superseded"

    # Directly in SQL too: an item is superseded at most once.
    with pytest.raises(errors.CheckViolation) as violated:
        await db.execute(
            "INSERT INTO knowledge_supersession"
            " (workspace_id, owner_id, item_id, supersedes_id, relation)"
            " VALUES (%s, %s, %s, %s, 'replaces')",
            (world.alpha, world.owner, newest.id, old.id),
        )
    assert violated.value.diag.constraint_name == "knowledge_supersession_active_check"


async def test_a_decision_answers_an_open_question(db: Conn, world: World) -> None:
    question = await record(
        db, world, decision("Which pump do we buy?", world.thread_a, kind="open_question")
    )
    other = await record(db, world, decision("Water at six.", world.thread_a, "m2"))

    answer = await record(
        db,
        world,
        decision(
            "We buy the quiet pump.",
            world.meeting,
            "00:12:30",
            days=2,
            supersedes=[SupersedesRef(question.id, "answers")],
        ),
    )

    closed = (await get_item(db, world.reader, question.id)).item
    assert closed.status == "closed"
    assert closed.superseded_by is not None
    assert closed.superseded_by.item_id == answer.id
    with pytest.raises(SupersessionError) as refused:
        await supersede(db, world.access, answer.id, [SupersedesRef(other.id, "answers")])
    assert refused.value.rule == "relation"


async def test_an_item_supersedes_only_earlier_ones(db: Conn, world: World) -> None:
    later = await record(db, world, decision("Water at seven.", world.thread_b, days=3))
    earlier = await record(db, world, decision("Water at six.", world.thread_a))

    with pytest.raises(SupersessionError) as refused:
        await supersede(db, world.access, earlier.id, [SupersedesRef(later.id)])
    assert refused.value.rule == "order"

    # Learned in the other order, the later item still supersedes the earlier one.
    await supersede(db, world.access, later.id, [SupersedesRef(earlier.id)])
    assert (await get_item(db, world.reader, earlier.id)).item.status == "superseded"


async def test_extraction_never_supersedes_a_manual_item(db: Conn, world: World) -> None:
    manual = await record(db, world, decision("Water at six.", world.thread_a))

    with pytest.raises(SupersessionError) as refused:
        await record_extraction(
            db,
            world.access,
            record_id=world.thread_b,
            content_hash="c0" * 32,
            extractor=EXTRACTOR,
            items=[
                decision(
                    "Water at seven.", world.thread_b, days=3, supersedes=[SupersedesRef(manual.id)]
                )
            ],
        )
    assert refused.value.rule == "manual"
    assert await count(db, "knowledge_extraction", world.alpha) == 0  # nothing was written
    assert (await get_item(db, world.reader, manual.id)).item.status == "active"

    # A person may supersede an extracted item.
    extraction = await record_extraction(
        db,
        world.access,
        record_id=world.thread_b,
        content_hash="c0" * 32,
        extractor=EXTRACTOR,
        items=[decision("Water at seven.", world.thread_b, days=3)],
    )
    correction = await record(
        db,
        world,
        decision(
            "Water at half past seven.",
            world.thread_b,
            "m7",
            days=4,
            supersedes=[SupersedesRef(extraction.item_ids[0])],
        ),
    )
    assert correction.manual
    assert [link.supersedes_id for link in correction.supersedes] == list(extraction.item_ids)


async def test_concurrent_supersessions_of_one_item_let_one_win(
    migrated_database_url: str, db: Conn, world: World
) -> None:
    old = await record(db, world, decision("Water at six.", world.thread_a))
    first = await record(db, world, decision("Water at seven.", world.thread_b, days=3))
    second = await record(db, world, decision("Water at eight.", world.thread_b, "m5", days=4))

    async with (
        connect(migrated_database_url, autocommit=True) as one_conn,
        connect(migrated_database_url, autocommit=True) as other_conn,
    ):
        async with one_conn.transaction():
            await supersede(one_conn, world.access, first.id, [SupersedesRef(old.id)])
            racing = asyncio.create_task(
                supersede(other_conn, world.access, second.id, [SupersedesRef(old.id)])
            )
            await asyncio.sleep(0.3)
            assert not racing.done()  # waits for the first transaction's locks
        with pytest.raises(NotActiveError) as refused:
            await racing

    assert (refused.value.item_id, refused.value.status) == (old.id, "superseded")
    stored = (await get_item(db, world.reader, old.id)).item
    assert stored.superseded_by is not None
    assert stored.superseded_by.item_id == first.id


# --- Reading: the default view, the history, the chain --------------------------------------------


async def test_thread_b_reverses_thread_a(db: Conn, world: World) -> None:
    """The shape of the M4 check: the default list shows only B; the history shows the chain with
    evidence links."""
    a = await record(
        db, world, decision("The pump runs at night.", world.thread_a, "1700000000.000100")
    )
    b = await record(
        db,
        world,
        decision(
            "The pump runs in the morning, not at night.",
            world.thread_b,
            "1700500000.000200",
            days=6,
            supersedes=[SupersedesRef(a.id)],
        ),
    )

    assert [item.id for item in await list_items(db, world.reader)] == [b.id]

    history = await list_items(db, world.reader, ItemFilters(statuses=ALL_STATUSES))
    assert [(item.id, item.status) for item in history] == [(b.id, "active"), (a.id, "superseded")]
    assert history[1].superseded_by is not None
    assert history[1].superseded_by.item_id == b.id

    for start in (a.id, b.id):
        chain = await get_item(db, world.reader, start)
        assert chain.item.id == start
        assert [item.id for item in chain.chain] == [a.id, b.id]
        evidence = [
            (e.record_id, e.anchor, e.record_state) for i in chain.chain for e in i.evidence
        ]
        assert evidence == [
            (world.thread_a, "1700000000.000100", "stored"),
            (world.thread_b, "1700500000.000200", "stored"),
        ]
        first = chain.chain[0].evidence[0]
        assert (first.source, first.record_kind, first.external_id, first.external_url) == (
            "chat",
            "thread",
            "garden/1700000000.000100",
            "https://chat.example.test/garden/1700000000.000100",
        )


async def test_the_chain_reaches_both_directions_and_siblings(db: Conn, world: World) -> None:
    a = await record(db, world, decision("Water at six.", world.thread_a))
    b = await record(db, world, decision("Fertilize weekly.", world.thread_a, "m2"))
    c = await record(
        db,
        world,
        decision(
            "Water at seven and fertilize monthly.",
            world.thread_b,
            days=2,
            supersedes=[SupersedesRef(a.id), SupersedesRef(b.id)],
        ),
    )
    d = await record(
        db,
        world,
        decision(
            "Water at eight.", world.meeting, "summary", days=4, supersedes=[SupersedesRef(c.id)]
        ),
    )
    unrelated = await record(db, world, decision("Paint the shed.", world.document, None))

    chain = await get_item(db, world.reader, a.id)

    assert [item.id for item in chain.chain] == [a.id, b.id, c.id, d.id]
    assert {item.id for item in chain.chain if item.status == "active"} == {d.id}
    assert len((await get_item(db, world.reader, unrelated.id)).chain) == 1


async def test_lists_filter_by_kind_person_record_time_and_workspace(
    db: Conn, world: World
) -> None:
    water = await record(
        db, world, decision("Water at six.", world.thread_a, participants=[world.me])
    )
    question = await record(
        db,
        world,
        decision(
            "Which pump?",
            world.meeting,
            "summary",
            days=2,
            kind="open_question",
            participants=[world.colleague],
        ),
    )
    code = await record(
        db, world, decision("Pump speed is 3.", world.code, f"{COMMIT}#Pump.speed", days=4)
    )
    beta = await record_item(
        db, world.access, "beta", decision("Fire the kiln twice.", world.thread_beta)
    )

    async def ids(**filters: Any) -> list[UUID]:
        return [item.id for item in await list_items(db, world.reader, ItemFilters(**filters))]

    assert await ids() == [code.id, question.id, beta.id, water.id]
    assert await ids(kinds=("open_question",)) == [question.id]
    assert await ids(person_ids=(world.me,)) == [water.id]
    assert await ids(record_ids=(world.code,)) == [code.id]
    assert await ids(decided_from=MONDAY + timedelta(days=1)) == [code.id, question.id]
    assert await ids(decided_until=MONDAY + timedelta(days=1)) == [beta.id, water.id]
    assert await ids(workspaces=("beta",)) == [beta.id]
    assert await ids(workspaces=(world.alpha,)) == [code.id, question.id, water.id]
    assert await ids(limit=1) == [code.id]
    assert question.participants[0].display_name == "Person Two"


# --- Extraction: idempotent per record content and extractor -----------------------------------


async def test_an_extraction_is_stored_once(db: Conn, world: World) -> None:
    async def extract(content_hash: str, extractor: str, *items: NewItem) -> Any:
        return await record_extraction(
            db,
            world.access,
            record_id=world.thread_a,
            content_hash=content_hash,
            extractor=extractor,
            items=items,
        )

    found = (
        decision("Water at six.", world.thread_a, participants=[world.me]),
        decision("Which pump?", world.thread_a, "m3", kind="open_question"),
    )
    first = await extract("aa" * 32, EXTRACTOR, *found)
    again = await extract("aa" * 32, EXTRACTOR, *found)

    assert first.created
    assert not again.created
    assert again.id == first.id
    assert again.item_ids == first.item_ids
    assert len(first.item_ids) == 2
    assert await count(db, "knowledge_item", world.alpha) == 2
    assert await count(db, "knowledge_evidence", world.alpha) == 2
    stored = (await get_item(db, world.reader, first.item_ids[0])).item
    assert (stored.extractor, stored.extraction_id, stored.manual) == (EXTRACTOR, first.id, False)

    # Nothing found is remembered too.
    empty = await extract("bb" * 32, EXTRACTOR)
    assert empty.created
    assert empty.item_ids == ()
    assert not (await extract("bb" * 32, EXTRACTOR)).created

    # New content, or a new extractor version, is a new extraction.
    assert (await extract("cc" * 32, EXTRACTOR)).created
    assert (await extract("aa" * 32, "test-model@decisions-v2")).created
    assert await count(db, "knowledge_extraction", world.alpha) == 4


async def test_an_extracted_item_cites_its_record(db: Conn, world: World) -> None:
    with pytest.raises(InvalidItemError) as invalid:
        await record_extraction(
            db,
            world.access,
            record_id=world.thread_a,
            content_hash="c0" * 32,
            extractor=EXTRACTOR,
            items=[decision("Water at six.", world.thread_b)],
        )
    assert invalid.value.field == "evidence"

    with pytest.raises(InvalidItemError) as invalid:
        await record_extraction(
            db,
            world.access,
            record_id=world.thread_a,
            content_hash="c0" * 32,
            extractor=MANUAL,
            items=[],
        )
    assert invalid.value.field == "extractor"

    # The database refuses it too.
    extraction = await record_extraction(
        db,
        world.access,
        record_id=world.thread_a,
        content_hash="c0" * 32,
        extractor=EXTRACTOR,
        items=[],
    )
    with pytest.raises(errors.CheckViolation) as refused:
        await insert_raw(db, world, world.thread_b, "m1", extraction=(EXTRACTOR, extraction.id))
    assert refused.value.diag.constraint_name == "knowledge_evidence_extraction_check"

    # A manual item names no extraction, and an extracted one names its extractor's extraction.
    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(
            "INSERT INTO knowledge_item (workspace_id, owner_id, kind, statement, decided_at,"
            " extraction_id) VALUES (%s, %s, 'decision', 'Water.', %s, %s)",
            (world.alpha, world.owner, MONDAY, extraction.id),
        )
    assert refused.value.diag.constraint_name == "knowledge_item_extraction_check"
    with pytest.raises(errors.ForeignKeyViolation) as missing:
        await db.execute(
            "INSERT INTO knowledge_item (workspace_id, owner_id, kind, statement, decided_at,"
            " extractor, extraction_id) VALUES (%s, %s, 'decision', 'Water.', %s, 'other-v1', %s)",
            (world.alpha, world.owner, MONDAY, extraction.id),
        )
    assert missing.value.diag.constraint_name == "knowledge_item_extraction_fkey"


async def test_reextraction_leaves_manual_items_alone(db: Conn, world: World) -> None:
    manual = await record(
        db, world, decision("Water at six.", world.thread_a, participants=[world.me])
    )
    before = await row_without_status(db, manual.id)

    for version in ("v1", "v2"):
        await record_extraction(
            db,
            world.access,
            record_id=world.thread_a,
            content_hash="c0" * 32,
            extractor=f"test-model@decisions-{version}",
            items=[decision("Water at six o'clock.", world.thread_a)],
        )

    stored = (await get_item(db, world.reader, manual.id)).item
    assert (stored.status, stored.extractor, stored.extraction_id) == ("active", MANUAL, None)
    assert await row_without_status(db, manual.id) == before


# --- Evidence ------------------------------------------------------------------------------------


async def test_evidence_takes_its_reference_from_the_record(db: Conn, world: World) -> None:
    async with db.transaction():
        item_id = await one(
            db,
            "INSERT INTO knowledge_item (workspace_id, owner_id, kind, statement, decided_at)"
            " VALUES (%s, %s, 'decision', 'Water at six.', %s) RETURNING id",
            (world.alpha, world.owner, MONDAY),
        )
        await db.execute(
            "INSERT INTO knowledge_evidence (workspace_id, owner_id, item_id, record_id, anchor,"
            " source_id, record_kind, external_id, external_url)"
            " VALUES (%s, %s, %s, %s, 'm1', %s, 'document', 'made-up', 'https://elsewhere.test')",
            (world.alpha, world.owner, item_id, world.thread_a, world.repo),
        )

    evidence = (await get_item(db, world.reader, item_id)).item.evidence
    assert [(e.source_id, e.record_kind, e.external_id) for e in evidence] == [
        (world.chat, "thread", "garden/1700000000.000100")
    ]

    with pytest.raises(errors.ForeignKeyViolation) as missing:
        await insert_raw(db, world, world.thread_beta, "m1")
    assert missing.value.diag.constraint_name == "knowledge_evidence_record_id_fkey"


async def test_anchors_fit_the_kind_of_record(db: Conn, world: World) -> None:
    refused_anchors: list[tuple[str, str | None]] = [
        (world.thread_a, None),
        (world.meeting, None),
        (world.code, None),
        (world.code, "Pump.speed"),
        (world.code, "abc123#Pump.speed"),
    ]
    for rid, anchor in refused_anchors:
        with pytest.raises(InvalidItemError) as invalid:
            await record(db, world, decision("Pump speed is 3.", rid, anchor))
        assert invalid.value.field == "evidence"

    accepted = [
        (world.code, COMMIT),
        (world.code, f"{COMMIT}#Pump.speed"),
        (world.code, f"{'ab' * 32}#L12-L30"),
        (world.document, None),
        (world.document, "Pump > Speed"),
        (world.meeting, "action_items"),
    ]
    for rid, anchor in accepted:
        await record(db, world, decision("Pump speed is 3.", rid, anchor))

    # The database refuses a thread without a message anchor too.
    with pytest.raises(errors.CheckViolation) as refused:
        await insert_raw(db, world, world.thread_a, None)
    assert refused.value.diag.constraint_name == "knowledge_evidence_message_anchor_check"


async def test_evidence_outlives_its_record(db: Conn, world: World) -> None:
    item = await record(db, world, decision("Water at six.", world.thread_a, "1700000000.000100"))

    async def state() -> tuple[str, str | None, str, str | None]:
        evidence = (await get_item(db, world.reader, item.id)).item.evidence[0]
        return (evidence.record_state, evidence.source, evidence.external_id, evidence.external_url)

    assert (await state())[0] == "stored"

    await db.execute("UPDATE record SET deleted_at = now() WHERE id = %s", (world.thread_a,))
    assert (await state())[0] == "deleted"

    # Retention or forget: the record row goes; the item and its reference stay.
    await db.execute("DELETE FROM record WHERE id = %s", (world.thread_a,))
    assert await state() == (
        "gone",
        "chat",
        "garden/1700000000.000100",
        "https://chat.example.test/garden/1700000000.000100",
    )

    # The same record ingested again has the same id, and the evidence finds it.
    assert (
        await add_record(
            db, world.owner, world.alpha, world.chat, "thread", "garden/1700000000.000100"
        )
        == world.thread_a
    )
    assert (await state())[0] == "stored"

    # Deleting the source deletes its records; the item stays, its reference without a source.
    await db.execute("DELETE FROM source WHERE id = %s", (world.chat,))
    assert await state() == (
        "gone",
        None,
        "garden/1700000000.000100",
        "https://chat.example.test/garden/1700000000.000100",
    )
    assert (await get_item(db, world.reader, item.id)).item.status == "active"


async def test_a_cited_person_cannot_be_deleted(db: Conn, world: World) -> None:
    await record(
        db, world, decision("Water at six.", world.thread_a, participants=[world.colleague])
    )

    with pytest.raises(errors.ForeignKeyViolation) as refused:
        await db.execute("DELETE FROM person WHERE id = %s", (world.colleague,))
    assert refused.value.diag.constraint_name == "knowledge_participant_person_fkey"


# --- Isolation -----------------------------------------------------------------------------------


async def test_items_stay_inside_their_workspace(db: Conn, world: World) -> None:
    alpha_item = await record(db, world, decision("Water at six.", world.thread_a))
    beta_item = await record_item(
        db, world.access, world.beta, decision("Fire the kiln twice.", world.thread_beta)
    )

    with pytest.raises(NotFoundError) as missing:
        await record(db, world, decision("Water at six.", world.thread_beta))
    assert missing.value.what == "record"
    with pytest.raises(NotFoundError) as missing:
        await record(db, world, decision("Water.", world.thread_a, participants=[world.stranger]))
    assert missing.value.what == "person"
    with pytest.raises(NotFoundError) as missing:
        await supersede(db, world.access, beta_item.id, [SupersedesRef(alpha_item.id)])
    assert missing.value.what == "knowledge item"
    with pytest.raises(errors.ForeignKeyViolation) as crossing:
        await db.execute(
            "INSERT INTO knowledge_supersession"
            " (workspace_id, owner_id, item_id, supersedes_id, relation)"
            " VALUES (%s, %s, %s, %s, 'replaces')",
            (world.beta, world.owner, beta_item.id, alpha_item.id),
        )
    assert crossing.value.diag.constraint_name == "knowledge_supersession_supersedes_fkey"

    # A token limited to beta reads and changes beta only.
    beta_reader = authorize_read(principal(world.owner, world.beta))
    beta_writer = WriteAccess(owner_id=world.owner, workspace_ids=frozenset({world.beta}))
    assert [item.id for item in await list_items(db, beta_reader)] == [beta_item.id]
    assert [
        item.id for item in await list_items(db, beta_reader, ItemFilters(workspaces=("alpha",)))
    ] == []
    with pytest.raises(NotFoundError):
        await get_item(db, beta_reader, alpha_item.id)
    with pytest.raises(NotFoundError):
        await retract_item(db, beta_writer, alpha_item.id)
    with pytest.raises(NotFoundError) as missing:
        await record_item(db, beta_writer, "alpha", decision("Water.", world.thread_a))
    assert missing.value.what == "workspace"
    with pytest.raises(NotFoundError) as missing:
        await record_extraction(
            db,
            beta_writer,
            record_id=world.thread_a,
            content_hash="c0" * 32,
            extractor=EXTRACTOR,
            items=[],
        )
    assert missing.value.what == "record"
    assert (await get_item(db, world.reader, alpha_item.id)).item.status == "active"


async def test_deleting_a_workspace_deletes_its_knowledge(db: Conn, world: World) -> None:
    old = await record(
        db, world, decision("Water at six.", world.thread_a, participants=[world.me])
    )
    await record(
        db,
        world,
        decision("Water at seven.", world.thread_b, days=2, supersedes=[SupersedesRef(old.id)]),
    )
    await record_extraction(
        db,
        world.access,
        record_id=world.meeting,
        content_hash="c0" * 32,
        extractor=EXTRACTOR,
        items=[decision("Buy the quiet pump.", world.meeting, "summary")],
    )
    kept = await record_item(
        db, world.access, world.beta, decision("Fire the kiln twice.", world.thread_beta)
    )

    await db.execute("DELETE FROM workspace WHERE id = %s", (world.alpha,))

    for table in KNOWLEDGE_TABLES:
        assert await count(db, table, world.alpha) == 0, table
    assert (await get_item(db, world.reader, kept.id)).item.status == "active"


# --- Schema --------------------------------------------------------------------------------------

# (lookup, table, index columns in order, unique)
LOOKUPS: list[tuple[str, str, tuple[str, ...], bool]] = [
    (
        "list_decisions: items of a workspace by status and time",
        "knowledge_item",
        ("workspace_id", "status", "decided_at"),
        False,
    ),
    ("items of an extraction", "knowledge_item", ("extraction_id",), False),
    (
        "extraction is idempotent per record content and extractor",
        "knowledge_extraction",
        ("workspace_id", "record_id", "content_hash", "extractor"),
        True,
    ),
    ("evidence of an item", "knowledge_evidence", ("item_id", "record_id", "anchor"), True),
    ("items a record is evidence of", "knowledge_evidence", ("record_id",), False),
    ("participants of an item", "knowledge_participant", ("item_id", "person_id"), True),
    ("items a person took part in", "knowledge_participant", ("person_id",), False),
    ("links stored on an item", "knowledge_supersession", ("item_id", "supersedes_id"), True),
    ("what replaced an item, at most one", "knowledge_supersession", ("supersedes_id",), True),
]


async def test_indexes_serve_the_knowledge_lookups(db: Conn) -> None:
    cur = await db.execute(
        "SELECT t.relname, ix.indisunique, array("
        "   SELECT a.attname FROM unnest(ix.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)"
        "   JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = k.attnum"
        "   ORDER BY k.ord)"
        " FROM pg_index ix JOIN pg_class t ON t.oid = ix.indrelid"
        " WHERE t.relname LIKE 'knowledge%%' AND ix.indpred IS NULL AND ix.indexprs IS NULL"
    )
    indexes = {(table, tuple(columns), unique) for table, unique, columns in await cur.fetchall()}

    missing = [
        f"{lookup}: {table} ({', '.join(columns)})"
        for lookup, table, columns, unique in LOOKUPS
        if (table, columns, unique) not in indexes
    ]
    assert missing == []
