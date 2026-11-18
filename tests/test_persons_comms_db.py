"""Person resolution over the synthetic comms fixtures (``eval/comms``, memini-dh2) in a real
Postgres. With the cast of ``people.json`` made first, every alias attached (the M2 checks set
"me" up that way), every name the batches hold (mail addresses, chat user ids, display names) must
resolve to its cast member, and ingest must make no person of its own. Without the cast, ingest
makes one person per key or unmatched name, and adding an alias joins a person seen by mail with
the one seen in chat."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, LiteralString, cast

import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from comms_fixtures import (
    WORKSPACE,
    create_sources,
    final_records,
    load,
    me,
    names_of,
    people,
    person_of,
    record_id_of,
)
from memini.auth import WriteAccess
from memini.db import connect
from memini.persons import AliasKind, add_alias, create_person, set_me
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]


@dataclass(frozen=True)
class Env:
    db: Conn
    access: WriteAccess
    sources: dict[str, uuid.UUID]


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with connect(migrated_database_url, autocommit=True) as db:
        owner = await ensure_principal(db, "owner")
        access = WriteAccess.for_owner(owner)
        yield Env(db, access, await create_sources(db, access))


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def make_cast(env: Env) -> dict[str, uuid.UUID]:
    """Every person and mailing list of people.json, with all its aliases; me marked."""
    ids: dict[str, uuid.UUID] = {}
    for person in cast("list[Json]", people()["people"]):
        aliases: list[tuple[AliasKind, str]] = [
            (cast("AliasKind", alias["kind"]), str(alias["value"]))
            for alias in cast("list[Json]", person["aliases"])
        ]
        made = await create_person(env.db, env.access, WORKSPACE, str(person["name"]), aliases)
        ids[str(person["key"])] = made.id
    for group in cast("list[Json]", people()["lists"]):
        address: tuple[AliasKind, str] = ("email", str(group["address"]))
        made = await create_person(env.db, env.access, WORKSPACE, str(group["name"]), [address])
        ids[f"list:{group['key']}"] = made.id
    await set_me(env.db, env.access, WORKSPACE, ids[me()])
    return ids


async def test_every_name_of_the_fixtures_resolves_to_its_cast_member(env: Env) -> None:
    ids = await make_cast(env)
    cast_size = len(ids)

    await load(env.db, env.access)

    assert await rows(env.db, "SELECT count(*) FROM person") == [(cast_size,)]
    for ref, record in final_records().items():
        if ref.kind == "document":
            continue
        names = names_of(ref.kind, record.payload)
        keys = {person_of(name) for name in names}
        assert None not in keys, ref
        expected = {ids[str(key)] for key in keys}
        linked = await rows(
            env.db,
            "SELECT DISTINCT person_root(person_id) FROM record_person WHERE record_id = %s",
            record_id_of(ref, env.sources),
        )
        assert {row[0] for row in linked} == expected, ref
    to_me = await rows(
        env.db,
        "SELECT count(DISTINCT record_id) FROM record_person"
        " WHERE person_id IN (SELECT person_members(person_root(%s)))"
        " AND role IN ('recipient', 'mention', 'participant')",
        ids[me()],
    )
    assert to_me[0][0] > 0


async def test_without_the_cast_an_alias_joins_mail_and_chat(env: Env) -> None:
    await load(env.db, env.access)
    tobias = next(p for p in cast("list[Json]", people()["people"]) if p["key"] == "tobias")
    aliases = {str(a["kind"]): str(a["value"]) for a in cast("list[Json]", tobias["aliases"])}
    by_mail = await rows(
        env.db,
        "SELECT person_id FROM alias WHERE kind = 'email' AND normalized_value = %s",
        aliases["email"],
    )
    # The chat names him by his user id alone: a name of its own until it is joined.
    by_chat = await rows(
        env.db,
        "SELECT person_id FROM alias WHERE kind = 'display_name' AND normalized_value = %s",
        aliases["chat_user_id"].casefold(),
    )
    assert len(by_mail) == len(by_chat) == 1
    assert by_mail != by_chat

    added = await add_alias(
        env.db, env.access, WORKSPACE, by_mail[0][0], "display_name", aliases["chat_user_id"]
    )

    assert added.merged == by_chat[0][0]
    authored = await rows(
        env.db,
        "SELECT count(DISTINCT record_id) FROM record_person"
        " WHERE person_id IN (SELECT person_members(person_root(%s))) AND role = 'author'",
        by_mail[0][0],
    )
    assert authored[0][0] > 1
