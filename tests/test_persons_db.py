"""Persons against a real Postgres: resolution at ingest, re-ingest, merge and split, "me",
workspace isolation, and the knowledge items that cite a person. Acceptance of memini-yq0: the same
person seen as a mail address and a chat id becomes one after an alias is added; merge and split
are reversible; "me" is settable. Every name, address and id here is made up."""

import secrets
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, LiteralString

import anyio
import pytest
from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow

from ingest_helpers import HOUR, T0, iso
from memini.auth import Principal, ReadFilter, WriteAccess, authorize_read
from memini.db import connect
from memini.ids import RecordKind, record_id
from memini.ingest import Batch, IngestResult, RecordIn, ingest
from memini.knowledge import EvidenceRef, ItemFilters, NewItem, list_items, record_item
from memini.persons import (
    AliasHeldError,
    InvalidAliasError,
    MergeError,
    NotFoundError,
    add_alias,
    create_person,
    find_person,
    get_me,
    get_person,
    list_persons,
    merge_person,
    remove_alias,
    rename_person,
    set_me,
    split_person,
    unset_me,
)
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True)
class Env:
    db: Conn
    url: str
    owner: uuid.UUID
    access: WriteAccess
    read: ReadFilter
    acme: uuid.UUID
    other: uuid.UUID
    chat: uuid.UUID
    """A push source in acme."""


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with connect(migrated_database_url, autocommit=True) as db:
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        other = await create_workspace(db, owner_id=owner, name="other")
        access = WriteAccess.for_owner(owner)
        chat = await create_push_source(db, access, "acme", NewPushSource(name="chat"))
        await create_push_source(db, access, "acme", NewPushSource(name="mail"))
        await create_push_source(db, access, "other", NewPushSource(name="chat"))
        read = authorize_read(reader(owner))
        yield Env(db, migrated_database_url, owner, access, read, acme.id, other.id, chat.id)


def reader(owner: uuid.UUID, workspace_id: uuid.UUID | None = None) -> Principal:
    return Principal(
        principal_id=owner,
        token_id=uuid.uuid4(),
        scopes=frozenset({"read", "admin", "ingest"}),
        workspace_id=workspace_id,
    )


# --- Synthetic records ---------------------------------------------------------------------------


def mail(
    external_id: str, *, text: str = "The release candidate is ready.", hours: int = 9
) -> RecordIn:
    at = T0 + hours * HOUR
    return RecordIn(
        kind="thread",
        external_id=external_id,
        occurred_at=at,
        payload={
            "subject": "Release",
            "messages": [
                {
                    "id": "<m1@example.test>",
                    "persons": ["Ann Example <ann@example.test>"],
                    "occurred_at": iso(at),
                    "text": text,
                    "recipients": ["Bo Sample <bo@example.test>"],
                }
            ],
        },
    )


CHAT_PEOPLE: tuple[dict[str, str], ...] = (
    {"name": "Ann Example", "chat_user_id": "U-ANN"},
    {"name": "Bo Sample", "chat_user_id": "U-BO"},
)


def chat(
    external_id: str,
    *,
    mentions: Sequence[str] = ("Bo Sample",),
    people: Sequence[dict[str, str]] = (),
    hours: int = 10,
    text: str = "Shall we move it to Friday?",
) -> RecordIn:
    """A chat thread by Ann; the chat's people directory names her and Bo by their user ids."""
    at = T0 + hours * HOUR
    return RecordIn(
        kind="thread",
        external_id=external_id,
        occurred_at=at,
        payload={
            "channel": "general",
            "people": [*CHAT_PEOPLE, *people],
            "messages": [
                {
                    "id": "1772355600.000100",
                    "persons": ["Ann Example"],
                    "occurred_at": iso(at),
                    "text": text,
                    "mentions": list(mentions),
                }
            ],
        },
    )


def meeting(external_id: str, participants: Sequence[str], hours: int = 14) -> RecordIn:
    at = T0 + hours * HOUR
    return RecordIn(
        kind="meeting",
        external_id=external_id,
        occurred_at=at,
        payload={
            "title": "Release planning",
            "started_at": iso(at),
            "ended_at": iso(at + HOUR / 2),
            "participants": list(participants),
        },
    )


async def send(
    env: Env, *records: RecordIn, source: str = "chat", workspace: str = "acme"
) -> IngestResult:
    async with connect(env.url, autocommit=True) as runs:
        return await ingest(
            env.db, env.access, workspace, source, Batch(records=records), runs=runs
        )


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    found = await rows(db, query, *params)
    assert len(found) == 1, found
    return found[0][0]


async def person_of(
    env: Env, kind: str, value: str, workspace: uuid.UUID | None = None
) -> uuid.UUID:
    """The person row that holds the alias (not resolved through merges)."""
    person: uuid.UUID = await scalar(
        env.db,
        "SELECT person_id FROM alias"
        " WHERE workspace_id = %s AND kind = %s AND normalized_value = %s",
        workspace or env.acme,
        kind,
        value,
    )
    return person


async def links(
    env: Env, external_id: str, source: str = "chat", kind: RecordKind = "thread"
) -> set[tuple[Any, ...]]:
    """(person, role, anchor) of a record's links, the person as its canonical one."""
    source_id = await scalar(
        env.db, "SELECT id FROM source WHERE workspace_id = %s AND name = %s", env.acme, source
    )
    rid = record_id(source_id, kind, external_id)
    found = await rows(
        env.db,
        "SELECT person_root(person_id), role, anchor FROM record_person WHERE record_id = %s",
        rid,
    )
    return set(found)


async def records_of(env: Env, person: uuid.UUID, role: str) -> list[str]:
    """The external ids of the records where the canonical person of ``person`` has ``role``, by
    time: the query list_meetings and list_messages make."""
    found = await rows(
        env.db,
        "SELECT r.external_id FROM record_person l JOIN record r ON r.id = l.record_id"
        " WHERE l.person_id IN (SELECT person_members(person_root(%s))) AND l.role = %s"
        " ORDER BY l.occurred_at, r.external_id",
        person,
        role,
    )
    return [row[0] for row in found]


async def snapshot(db: Conn) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of the persons layer, and the knowledge participants, as they are."""
    tables: dict[str, LiteralString] = {
        "persons": ["SELECT id, workspace_id, owner_id, display_name, principal_id, merged_into,"]
        " merged_at, created_at FROM person ORDER BY id",
        "alias": "SELECT id, workspace_id, person_id, kind, value, normalized_value, created_at"
        " FROM alias ORDER BY id",
        "record_person": "SELECT record_id, person_id, role, anchor, occurred_at, created_at,"
        " xmin::text FROM record_person ORDER BY record_id, person_id, role, anchor",
        "knowledge_participant": "SELECT item_id, person_id, created_at, xmin::text"
        " FROM knowledge_participant ORDER BY item_id, person_id",
    }
    return {name: await rows(db, query) for name, query in tables.items()}


async def versions(db: Conn) -> dict[str, str]:
    """xmin of every row of the persons layer: it changes exactly when a row is written."""
    found = await rows(
        db,
        "SELECT 'person:' || id, xmin::text FROM person"
        " UNION ALL SELECT 'alias:' || id, xmin::text FROM alias"
        " UNION ALL SELECT 'link:' || record_id || person_id || role || coalesce(anchor, ''),"
        " xmin::text FROM record_person",
    )
    return {key: value for key, value in found}


# --- Resolution at ingest ------------------------------------------------------------------------


async def test_ingest_resolves_deterministic_keys_and_keeps_names_apart(env: Env) -> None:
    await send(env, mail("release"), source="mail")
    await send(
        env,
        chat("general/1"),
        meeting("m-1", ["Ann Example", "Cy Placeholder", "ann@example.test"]),
    )

    ann_mail = await person_of(env, "email", "ann@example.test")
    bo_mail = await person_of(env, "email", "bo@example.test")
    ann_chat = await person_of(env, "chat_user_id", "U-ANN")
    bo_chat = await person_of(env, "chat_user_id", "U-BO")
    ann_name = await person_of(env, "display_name", "ann example")
    cy_name = await person_of(env, "display_name", "cy placeholder")
    # Six persons: a key is one person, a name without a key is one more, and nothing is guessed.
    assert len({ann_mail, bo_mail, ann_chat, bo_chat, ann_name, cy_name}) == 6
    assert await scalar(env.db, "SELECT count(*) FROM person") == 6
    names = dict(await rows(env.db, "SELECT id, display_name FROM person"))
    assert names[ann_mail] == "Ann Example"  # named by the identity, keyed by the address
    assert names[ann_chat] == "Ann Example"

    assert await links(env, "release", "mail") == {
        (ann_mail, "author", "<m1@example.test>"),
        (bo_mail, "recipient", "<m1@example.test>"),
    }
    assert await links(env, "general/1") == {
        (ann_chat, "author", "1772355600.000100"),
        (bo_chat, "mention", "1772355600.000100"),
    }
    assert await links(env, "m-1", kind="meeting") == {
        (ann_name, "participant", None),
        (cy_name, "participant", None),
        (ann_mail, "participant", None),
    }
    when = await scalar(
        env.db,
        "SELECT occurred_at FROM record_person WHERE person_id = %s AND role = 'participant'",
        cy_name,
    )
    assert when == T0 + 14 * HOUR


async def test_a_key_seen_with_another_joins_its_person_and_a_held_key_stays(env: Env) -> None:
    await send(env, mail("release"), source="mail")
    ann = await person_of(env, "email", "ann@example.test")
    await send(env, chat("general/1", mentions=[]))
    ann_chat = await person_of(env, "chat_user_id", "U-ANN")

    # The address and a new chat id together: the chat id is the address's person's now.
    both = {"name": "Ann E.", "email": "ann@example.test", "chat_user_id": "U-ANN-2"}
    # The address and a chat id another person holds: the address wins, nothing is merged.
    clash = {"name": "A. Example", "email": "ann@example.test", "chat_user_id": "U-ANN"}
    await send(env, chat("general/2", mentions=["Ann E.", "A. Example"], people=[both, clash]))

    assert await person_of(env, "chat_user_id", "U-ANN-2") == ann
    assert await person_of(env, "chat_user_id", "U-ANN") == ann_chat
    assert await scalar(env.db, "SELECT merged_into FROM person WHERE id = %s", ann_chat) is None
    assert (ann, "mention", "1772355600.000100") in await links(env, "general/2")
    # A chat-only identity with the learned id now resolves to the address's person.
    annie = {"name": "Annie", "chat_user_id": "U-ANN-2"}
    await send(env, chat("general/3", mentions=["Annie"], people=[annie]))
    assert (ann, "mention", "1772355600.000100") in await links(env, "general/3")


async def test_re_ingest_changes_no_row_of_persons(env: Env) -> None:
    batch = (chat("general/1"), meeting("m-1", ["Ann Example", "Cy Placeholder"]))
    first = await send(env, *batch)
    before = await versions(env.db)
    report = await scalar(env.db, "SELECT report->'counts' FROM run WHERE id = %s", first.run_id)
    assert report["persons_created"] == 4
    assert report["aliases_created"] == 4
    assert report["person_links_inserted"] == 4

    again = await send(env, *batch)
    assert again.unchanged == 2
    assert await versions(env.db) == before

    # Written again with other text and the same persons: the record changes, no person row does.
    edited = await send(env, chat("general/1", text="Friday it is."))
    assert edited.updated == 1
    assert await versions(env.db) == before
    report = await scalar(env.db, "SELECT report->'counts' FROM run WHERE id = %s", edited.run_id)
    assert report["persons_created"] == 0
    assert report["person_links_inserted"] == 0


async def test_links_follow_what_the_payload_says_when_it_changes(env: Env) -> None:
    await send(env, chat("general/1"))
    bo = await person_of(env, "chat_user_id", "U-BO")
    ann = await person_of(env, "chat_user_id", "U-ANN")

    await send(env, chat("general/1", mentions=[], hours=11))

    assert await links(env, "general/1") == {(ann, "author", "1772355600.000100")}
    when = await scalar(env.db, "SELECT occurred_at FROM record_person WHERE person_id = %s", ann)
    assert when == T0 + 11 * HOUR
    # The person stays, with its alias, though no record names it now.
    assert await person_of(env, "chat_user_id", "U-BO") == bo


async def test_persons_are_read_from_the_redacted_payload(env: Env) -> None:
    # A token pasted where a name goes: redacted at ingest, so no alias or name holds it.
    secret = "ghp_" + secrets.token_hex(18)
    await send(env, chat("general/1", mentions=[f"Bo {secret}"]))

    found = await rows(env.db, "SELECT value FROM alias UNION ALL SELECT display_name FROM person")
    assert found
    assert all(secret not in value for (value,) in found)
    assert any("[REDACTED" in value for (value,) in found)


async def test_links_go_with_their_records(env: Env) -> None:
    await send(env, chat("general/1"))
    ann = await person_of(env, "chat_user_id", "U-ANN")
    await send(env, RecordIn(kind="thread", external_id="general/1", deleted=True))
    # A tombstone keeps its row, and its links: reads leave out deleted records.
    assert await scalar(env.db, "SELECT count(*) FROM record_person") == 2

    # Retention and forget delete the record; deleting the source deletes its records.
    await env.db.execute("DELETE FROM record WHERE source_id = %s", (env.chat,))
    assert await scalar(env.db, "SELECT count(*) FROM record_person") == 0
    assert await person_of(env, "chat_user_id", "U-ANN") == ann


async def test_concurrent_batches_make_one_person_per_key(env: Env) -> None:
    async def push(external_id: str) -> None:
        async with connect(env.url, autocommit=True) as conn:
            await ingest(conn, env.access, "acme", "chat", Batch(records=[chat(external_id)]))

    async with anyio.create_task_group() as group:
        for n in range(6):
            group.start_soon(push, f"general/{n}")

    assert await scalar(env.db, "SELECT count(*) FROM person") == 2
    assert await scalar(env.db, "SELECT count(*) FROM alias") == 2
    assert await scalar(env.db, "SELECT count(*) FROM record_person") == 12


# --- Acceptance: one person after an alias is added ----------------------------------------------


async def test_mail_and_chat_become_one_person_after_an_alias_is_added(env: Env) -> None:
    await send(env, mail("release"), source="mail")
    await send(env, chat("general/1"))
    ann_mail = await person_of(env, "email", "ann@example.test")
    ann_chat = await person_of(env, "chat_user_id", "U-ANN")
    assert ann_mail != ann_chat
    before = await snapshot(env.db)

    added = await add_alias(env.db, env.access, "acme", ann_mail, "chat_user_id", "U-ANN")

    assert added.created is False
    assert added.merged == ann_chat
    assert added.alias.person_id == ann_chat
    assert {(a.kind, a.normalized_value) for a in added.person.aliases} == {
        ("email", "ann@example.test"),
        ("chat_user_id", "U-ANN"),
    }
    by_mail = await find_person(env.db, env.read, "acme", "email", "Ann@Example.test")
    by_chat = await find_person(env.db, env.read, "acme", "chat_user_id", "U-ANN")
    assert by_mail.id == by_chat.id == ann_mail
    assert await records_of(env, ann_chat, "author") == ["release", "general/1"]
    canonical = (await list_persons(env.db, env.read, "acme", query="ann")).persons
    assert [person.id for person in canonical] == [ann_mail]

    # And back.
    await split_person(env.db, env.access, "acme", ann_chat)
    assert await snapshot(env.db) == before
    assert await records_of(env, ann_mail, "author") == ["release"]


async def test_an_alias_added_first_resolves_later_records_to_the_person(env: Env) -> None:
    me = await create_person(
        env.db, env.access, "acme", "  Ann  Example ", [("email", "ann@example.test")]
    )
    assert me.display_name == "Ann Example"
    added = await add_alias(env.db, env.access, "acme", me.id, "chat_user_id", " U-ANN ")
    assert (added.created, added.merged) == (True, None)
    again = await add_alias(env.db, env.access, "acme", me.id, "chat_user_id", "U-ANN")
    assert (again.created, again.merged, again.alias.id) == (False, None, added.alias.id)

    await send(env, mail("release"), source="mail")
    await send(env, chat("general/1"))

    assert await records_of(env, me.id, "author") == ["release", "general/1"]
    assert await scalar(env.db, "SELECT count(*) FROM person") == 3  # me, bo by mail, bo by chat

    with pytest.raises(AliasHeldError) as held:
        await create_person(env.db, env.access, "acme", "Someone", [("email", "ANN@example.test")])
    assert held.value.person_id == me.id
    with pytest.raises(InvalidAliasError):
        await add_alias(env.db, env.access, "acme", me.id, "email", "not an address")


# --- Merge and split -----------------------------------------------------------------------------


async def test_merge_then_split_restores_exactly(env: Env) -> None:
    await send(env, mail("release"), source="mail")
    await send(env, chat("general/1"), meeting("m-1", ["Ann Example"]))
    ann = await person_of(env, "email", "ann@example.test")
    ann_chat = await person_of(env, "chat_user_id", "U-ANN")
    ann_name = await person_of(env, "display_name", "ann example")
    thread = record_id(env.chat, "thread", "general/1")
    item = await record_item(
        env.db,
        env.access,
        "acme",
        NewItem(
            "decision",
            "The release moves to Friday.",
            T0 + 10 * HOUR,
            evidence=(EvidenceRef(thread, "1772355600.000100"),),
            participants=(ann_chat,),
        ),
    )
    # The name is merged into the chat id first, then the chat id into the address: a chain.
    await merge_person(env.db, env.access, "acme", ann_name, ann_chat)
    before = await snapshot(env.db)

    merged = await merge_person(env.db, env.access, "acme", ann_chat, ann)

    assert merged.id == ann
    assert merged.members == (ann_chat,)
    assert {(a.kind, a.normalized_value) for a in merged.aliases} == {
        ("email", "ann@example.test"),
        ("chat_user_id", "U-ANN"),
        ("display_name", "ann example"),
    }
    assert await records_of(env, ann, "participant") == ["m-1"]
    found = await list_items(env.db, env.read, ItemFilters(person_ids=(ann,)))
    assert [each.id for each in found] == [item.id]
    shown = await get_person(env.db, env.read, "acme", ann_name)
    assert (shown.canonical_id, shown.merged_into) == (ann, ann_chat)
    listed = await list_persons(env.db, env.read, "acme")
    assert ann_chat not in {person.id for person in listed.persons}
    every = await list_persons(env.db, env.read, "acme", merged=True)
    assert {ann, ann_chat, ann_name} <= {person.id for person in every.persons}

    split = await split_person(env.db, env.access, "acme", ann_chat)

    assert split.canonical_id == ann_chat
    assert split.members == (ann_name,)
    assert await snapshot(env.db) == before
    assert await list_items(env.db, env.read, ItemFilters(person_ids=(ann,))) == []
    found = await list_items(env.db, env.read, ItemFilters(person_ids=(ann_name,)))
    assert [each.id for each in found] == [item.id]


async def test_a_merge_that_cannot_be_made_is_refused(env: Env) -> None:
    await send(env, chat("general/1"), meeting("m-1", ["Ann Example", "Cy Placeholder"]))
    ann = await person_of(env, "chat_user_id", "U-ANN")
    bo = await person_of(env, "chat_user_id", "U-BO")
    cy = await person_of(env, "display_name", "cy placeholder")
    await merge_person(env.db, env.access, "acme", bo, ann)
    await merge_person(env.db, env.access, "acme", cy, bo)

    for person, into, refusal in [
        (ann, ann, "same_person"),
        (ann, cy, "same_person"),  # cy is merged into bo, which is merged into ann
        (bo, cy, "already_merged"),
    ]:
        with pytest.raises(MergeError) as refused:
            await merge_person(env.db, env.access, "acme", person, into)
        assert refused.value.refusal == refusal
    with pytest.raises(MergeError) as refused:
        await split_person(env.db, env.access, "acme", ann)
    assert refused.value.refusal == "not_merged"
    with pytest.raises(NotFoundError):
        await merge_person(env.db, env.access, "acme", ann, uuid.uuid4())

    # The database refuses a cycle by itself too, and a merge across workspaces.
    with pytest.raises(errors.CheckViolation) as cycle:
        await env.db.execute(
            "UPDATE person SET merged_into = %s, merged_at = now() WHERE id = %s", (cy, ann)
        )
    assert cycle.value.diag.constraint_name == "person_merge_cycle_check"
    await send(env, chat("general/1"), workspace="other")
    stranger = await person_of(env, "chat_user_id", "U-ANN", env.other)
    with pytest.raises(errors.ForeignKeyViolation):
        await env.db.execute(
            "UPDATE person SET merged_into = %s, merged_at = now() WHERE id = %s", (stranger, ann)
        )


async def test_an_alias_is_removed_and_a_person_renamed(env: Env) -> None:
    await send(env, chat("general/1"))
    ann = await person_of(env, "chat_user_id", "U-ANN")
    added = await add_alias(env.db, env.access, "acme", ann, "email", "ann@example.test")

    renamed = await rename_person(env.db, env.access, "acme", ann, " Ann  Example ")
    assert renamed.display_name == "Ann Example"
    left = await remove_alias(env.db, env.access, "acme", ann, added.alias.id)
    assert [(a.kind, a.normalized_value) for a in left.aliases] == [("chat_user_id", "U-ANN")]
    with pytest.raises(NotFoundError) as missing:
        await remove_alias(env.db, env.access, "acme", ann, added.alias.id)
    assert missing.value.what == "alias"
    with pytest.raises(InvalidAliasError):
        await rename_person(env.db, env.access, "acme", ann, " \x07 ")


# --- Me ------------------------------------------------------------------------------------------


async def test_me_is_settable_and_resolves_through_merges(env: Env) -> None:
    await send(env, mail("release"), source="mail")
    await send(env, chat("general/1"))
    ann = await person_of(env, "email", "ann@example.test")
    ann_chat = await person_of(env, "chat_user_id", "U-ANN")
    bo = await person_of(env, "email", "bo@example.test")
    assert await get_me(env.db, env.read, "acme") is None

    async def me_id(workspace: str = "acme") -> uuid.UUID | None:
        me = await get_me(env.db, env.read, workspace)
        if me is not None:
            assert me.is_me
        return None if me is None else me.id

    marked = await set_me(env.db, env.access, "acme", ann_chat)
    assert marked.is_me
    assert await me_id() == ann_chat

    await merge_person(env.db, env.access, "acme", ann_chat, ann)
    assert await me_id() == ann
    assert await records_of(env, ann, "author") == ["release", "general/1"]

    await set_me(env.db, env.access, "acme", bo)
    assert await me_id() == bo
    assert await scalar(env.db, "SELECT count(*) FROM person WHERE principal_id IS NOT NULL") == 1
    assert not (await get_person(env.db, env.read, "acme", ann)).is_me

    await unset_me(env.db, env.access, "acme")
    assert await me_id() is None
    assert await me_id("other") is None


# --- Isolation -----------------------------------------------------------------------------------


async def test_workspaces_do_not_share_persons(env: Env) -> None:
    await send(env, mail("release"), source="mail")
    await send(env, chat("general/1"), workspace="other")
    await send(env, chat("general/1"))
    ann = await person_of(env, "chat_user_id", "U-ANN")
    stranger = await person_of(env, "chat_user_id", "U-ANN", env.other)
    assert ann != stranger

    limited_read = authorize_read(reader(env.owner, env.other))
    limited = WriteAccess(owner_id=env.owner, workspace_ids=frozenset({env.other}))
    with pytest.raises(NotFoundError):
        await get_person(env.db, limited_read, "acme", ann)
    with pytest.raises(NotFoundError):
        await get_person(env.db, env.read, "other", ann)  # a person of acme, asked in other
    with pytest.raises(NotFoundError):
        await find_person(env.db, limited_read, "acme", "chat_user_id", "U-ANN")
    with pytest.raises(NotFoundError):
        await merge_person(env.db, limited, "acme", ann, stranger)
    with pytest.raises(NotFoundError):
        await merge_person(env.db, env.access, "other", stranger, ann)
    with pytest.raises(NotFoundError):
        await set_me(env.db, limited, "acme", ann)
    found = await find_person(env.db, limited_read, "other", "chat_user_id", "U-ANN")
    assert found.id == stranger
    assert [p.id for p in (await list_persons(env.db, limited_read, "other")).persons] == [
        await person_of(env, "chat_user_id", "U-ANN", env.other),
        await person_of(env, "chat_user_id", "U-BO", env.other),
    ]


async def test_a_listing_searches_names_and_aliases_and_pages(env: Env) -> None:
    await send(env, mail("release"), source="mail")
    await send(env, meeting("m-1", ["Cy Placeholder", "Di Stand-in", "Ed Dummy"]))

    by_address = await list_persons(env.db, env.read, "acme", query="BO@EXAMPLE")
    assert [p.display_name for p in by_address.persons] == ["Bo Sample"]
    by_name = await list_persons(env.db, env.read, "acme", query="placeholder")
    assert [p.display_name for p in by_name.persons] == ["Cy Placeholder"]
    assert (await list_persons(env.db, env.read, "acme", query="100%")).persons == ()

    seen: list[str] = []
    page = await list_persons(env.db, env.read, "acme", limit=2)
    while True:
        seen.extend(p.display_name for p in page.persons)
        if page.next is None:
            break
        page = await list_persons(env.db, env.read, "acme", limit=2, after=page.next)
    assert seen == ["Ann Example", "Bo Sample", "Cy Placeholder", "Di Stand-in", "Ed Dummy"]


async def test_the_indexes_serve_the_lookups(env: Env) -> None:
    cur = await env.db.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename IN ('record_person', 'person')"
    )
    definitions = {row[0] for row in await cur.fetchall()}
    assert any("(person_id, role, occurred_at)" in each for each in definitions)
    assert any("(record_id, person_id, role, anchor)" in each for each in definitions)
    assert any("person USING btree (merged_into)" in each for each in definitions)
