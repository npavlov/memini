"""Source filters at ingest and prune against a real Postgres. Acceptance of memini-hbo: an
excluded author's messages are not stored; after a rule is added, prune removes the matching
stored records and their chunks, and a prune run again changes nothing. Every value is synthetic:
names are made up, addresses are under example.test."""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, LiteralString

import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from ingest_helpers import HOUR, T0, iso
from memini.auth import WriteAccess
from memini.db import connect
from memini.embeddings import FakeEmbedder
from memini.filters import SourceFilters
from memini.ingest import Batch, IngestResult, PayloadIndexer, RecordIn, WindowClaim, ingest
from memini.prune import NotPrunableError, PruneResult, prune_source
from memini.sources import (
    InvalidSourceError,
    NewPushSource,
    Source,
    SourceUpdate,
    create_push_source,
    update_source,
)
from memini.workspaces import NotFoundError, create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

BOT = "Deploy Bot <deploy-bot@example.test>"
ANN = "Ann Example <ann@example.test>"
BO = "Bo Sample <bo@example.test>"
BOT_TEXT = "Deployed build 4711 to staging."


@dataclass(frozen=True)
class Env:
    db: Conn
    runs: Conn
    access: WriteAccess
    source_id: uuid.UUID
    embedder: FakeEmbedder


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        connect(migrated_database_url, autocommit=True) as runs,
    ):
        owner = await ensure_principal(db, "owner")
        await create_workspace(db, owner_id=owner, name="acme")
        access = WriteAccess.for_owner(owner)
        chat = await create_push_source(db, access, "acme", NewPushSource(name="chat"))
        yield Env(db, runs, access, chat.id, FakeEmbedder())


def message(author: str, minute: int, text: str) -> dict[str, Any]:
    return {"author": author, "sent_at": iso(T0 + minute * HOUR / 60), "text": text}


def thread(
    external_id: str, *messages: dict[str, Any], channel: str = "release", **fields: Any
) -> RecordIn:
    return RecordIn(
        kind="thread",
        external_id=external_id,
        occurred_at=T0 + HOUR,
        payload={"channel": channel, **fields, "messages": list(messages)},
    )


def mixed(external_id: str = "t-mixed") -> RecordIn:
    return thread(
        external_id,
        message(ANN, 1, "Is the release candidate on staging?"),
        message(BOT, 2, BOT_TEXT),
        message(BO, 3, "Yes, I checked it this morning."),
    )


def bot_only(external_id: str = "t-bot") -> RecordIn:
    return thread(external_id, message(BOT, 5, BOT_TEXT), message(BOT, 6, "Rollback done."))


def human(external_id: str = "t-human", channel: str = "release") -> RecordIn:
    return thread(external_id, message(ANN, 7, "Lunch at noon?"), channel=channel)


def standup() -> RecordIn:
    return RecordIn(
        kind="meeting",
        external_id="m-standup",
        occurred_at=T0 + 9 * HOUR,
        payload={
            "title": "Daily stand-up",
            "started_at": iso(T0 + 9 * HOUR),
            "summary": "Nothing blocks the release.",
        },
    )


async def send(env: Env, *records: RecordIn, **kwargs: Any) -> IngestResult:
    return await ingest(
        env.db,
        env.access,
        "acme",
        "chat",
        Batch(records=list(records), **kwargs),
        runs=env.runs,
        indexer=PayloadIndexer(env.embedder),
    )


async def prune(env: Env, *, indexer: bool = True) -> PruneResult:
    return await prune_source(
        env.db,
        env.access,
        "acme",
        "chat",
        runs=env.runs,
        indexer=PayloadIndexer(env.embedder) if indexer else None,
        page_size=2,  # several pages, so that paging is part of every test
    )


async def set_filters(env: Env, filters: SourceFilters) -> Source:
    return await update_source(env.db, env.access, "acme", "chat", SourceUpdate(filters=filters))


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def records(env: Env) -> dict[str, tuple[bool, dict[str, Any]]]:
    """Every record of the source by external id: whether it is a tombstone, and its payload."""
    found = await rows(
        env.db,
        "SELECT external_id, deleted_at IS NOT NULL, payload FROM record WHERE source_id = %s",
        env.source_id,
    )
    return {external_id: (deleted, json.loads(payload)) for external_id, deleted, payload in found}


async def authors(env: Env, external_id: str) -> list[str]:
    _, payload = (await records(env))[external_id]
    return [m["author"] for m in payload["messages"]]


async def chunks(env: Env) -> dict[str, list[str]]:
    """The chunk texts of the source, by the external id of their record."""
    found = await rows(
        env.db,
        "SELECT r.external_id, c.content FROM chunk c JOIN record r ON r.id = c.record_id"
        " WHERE c.source_id = %s ORDER BY r.external_id, c.id",
        env.source_id,
    )
    texts: dict[str, list[str]] = {}
    for external_id, content in found:
        texts.setdefault(external_id, []).append(content)
    return texts


async def report(env: Env, run_id: uuid.UUID | None) -> dict[str, int]:
    found = await rows(env.db, "SELECT report->'counts' FROM run WHERE id = %s", run_id)
    assert len(found) == 1
    counts: dict[str, int] = found[0][0]
    return counts


async def versions(env: Env) -> dict[str, str]:
    """xmin of every record and chunk row: it changes exactly when a row is written."""
    found = await rows(
        env.db,
        "SELECT 'record:' || id, xmin::text FROM record WHERE source_id = %s"
        " UNION ALL SELECT 'chunk:' || id, xmin::text FROM chunk WHERE source_id = %s",
        env.source_id,
        env.source_id,
    )
    return {key: value for key, value in found}


# --- At ingest -----------------------------------------------------------------------------------


async def test_an_excluded_authors_messages_are_not_stored(env: Env) -> None:
    await set_filters(env, SourceFilters(exclude_authors=("*bot*",)))

    result = await send(env, mixed(), bot_only(), human())

    stored = await records(env)
    assert set(stored) == {"t-mixed", "t-human"}
    assert await authors(env, "t-mixed") == [ANN, BO]
    assert (result.inserted, result.unchanged) == (2, 1)  # the bot's thread: a tombstone of nothing
    texts = await chunks(env)
    assert any("staging" in text for text in texts["t-mixed"])
    assert not any(BOT_TEXT in text for found in texts.values() for text in found)
    counts = await report(env, result.run_id)
    assert (counts["records_filtered"], counts["messages_filtered"]) == (1, 3)

    again = await send(env, mixed(), bot_only(), human())

    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 3)


async def test_a_channel_allowlist_admits_only_that_channel(env: Env) -> None:
    await set_filters(env, SourceFilters(allow_channels=("release",)))
    no_channel = RecordIn(
        kind="thread",
        external_id="t-dm",
        occurred_at=T0 + HOUR,
        payload={"messages": [message(ANN, 1, "A direct message.")]},
    )

    await send(env, human("t-release"), human("t-random", channel="random"), no_channel)

    assert set(await records(env)) == {"t-release"}


async def test_a_record_stored_before_a_rule_and_sent_again_is_deleted_by_the_batch(
    env: Env,
) -> None:
    await send(env, bot_only(), human(), stream="chat/release")
    await set_filters(env, SourceFilters(exclude_authors=("*bot*",)))

    result = await send(
        env,
        bot_only(),
        stream="chat/release",
        window=WindowClaim(start=T0, end=T0 + 2 * HOUR, complete=True, snapshot=True),
    )

    stored = await records(env)
    assert stored["t-bot"][0] is True  # a tombstone, its chunks gone
    assert stored["t-human"][0] is True  # left out of the snapshot: deleted at the source
    assert (result.deleted, result.reconciled) == (1, 1)
    assert set(await chunks(env)) == set()


async def test_filters_are_part_of_a_push_source_and_refused_when_not_valid(env: Env) -> None:
    source = await set_filters(
        env, SourceFilters(allow_domains=("Example.TEST",), exclude_automated=True)
    )

    assert source.filters == SourceFilters(allow_domains=("example.test",), exclude_automated=True)
    with pytest.raises(InvalidSourceError) as raised:
        await set_filters(env, SourceFilters(allow_domains=("not a domain",)))
    assert raised.value.setting == "filters.allow_domains"
    cleared = await set_filters(env, SourceFilters())
    assert cleared.filters == SourceFilters()
    found = await rows(env.db, "SELECT config FROM source WHERE id = %s", env.source_id)
    assert found == [({},)]


# --- Prune ---------------------------------------------------------------------------------------


async def test_after_a_rule_is_added_prune_removes_matching_records_and_their_chunks(
    env: Env,
) -> None:
    await send(env, mixed(), bot_only(), human(), standup())
    await send(env, RecordIn(kind="thread", external_id="t-human", deleted=True))
    before = await chunks(env)
    assert any(BOT_TEXT in text for text in before["t-bot"] + before["t-mixed"])
    await set_filters(
        env, SourceFilters(exclude_authors=("*bot*",), exclude_titles=("daily stand-up",))
    )
    env.embedder.reset()

    result = await prune(env)

    stored = await records(env)
    assert set(stored) == {"t-mixed", "t-human"}  # the bot's thread and the stand-up are gone
    assert await authors(env, "t-mixed") == [ANN, BO]
    assert stored["t-human"][0] is True  # a tombstone the rules keep stays one
    after = await chunks(env)
    assert set(after) == {"t-mixed"}
    assert not any(BOT_TEXT in text for text in after["t-mixed"])
    assert any("checked it this morning" in text for text in after["t-mixed"])
    leftover = await rows(
        env.db,
        "SELECT count(*) FROM chunk c WHERE c.source_id = %s"
        " AND NOT EXISTS (SELECT 1 FROM record r WHERE r.id = c.record_id)",
        env.source_id,
    )
    assert leftover == [(0,)]
    assert (result.scanned, result.deleted, result.trimmed, result.unchanged) == (4, 2, 1, 1)
    assert result.messages_removed == 3
    assert result.index.deleted >= len(before["t-bot"]) + len(before["m-standup"])
    counts = await report(env, result.run_id)
    assert (counts["records_pruned"], counts["records_trimmed"], counts["messages_pruned"]) == (
        2,
        1,
        3,
    )
    assert counts["chunks_deleted"] == result.index.deleted
    run = await rows(env.db, "SELECT kind, status, source_id FROM run WHERE id = %s", result.run_id)
    assert run == [("prune", "succeeded", env.source_id)]

    # The trimmed thread is what ingest stores when the same thread is sent under the rule.
    resent = await send(env, mixed())
    assert (resent.updated, resent.unchanged) == (0, 1)


async def test_prune_run_again_is_a_no_op(env: Env) -> None:
    await send(env, mixed(), bot_only(), human())
    await set_filters(env, SourceFilters(exclude_authors=("*bot*",)))
    await prune(env)
    rows_before = await versions(env)
    env.embedder.reset()

    again = await prune(env)

    assert await versions(env) == rows_before
    assert (again.scanned, again.deleted, again.trimmed, again.unchanged) == (2, 0, 0, 2)
    assert env.embedder.texts == []


async def test_prune_without_an_indexer_deletes_the_chunks_of_trimmed_threads(env: Env) -> None:
    await send(env, mixed(), human())
    await set_filters(env, SourceFilters(exclude_authors=("*bot*",)))

    result = await prune(env, indexer=False)

    assert (result.trimmed, result.not_indexed) == (1, 1)
    assert set(await chunks(env)) == {"t-human"}
    assert await authors(env, "t-mixed") == [ANN, BO]


async def test_prune_without_rules_reads_nothing(env: Env) -> None:
    await send(env, mixed())
    rows_before = await versions(env)

    result = await prune(env)

    assert (result.rules, result.scanned) == (False, 0)
    assert await versions(env) == rows_before
    assert await report(env, result.run_id) == {}


async def test_prune_refuses_a_git_source_and_one_it_may_not_change(env: Env) -> None:
    await env.db.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind, config)"
        " SELECT workspace_id, owner_id, 'repo', 'repository', '{}' FROM source WHERE id = %s",
        (env.source_id,),
    )

    with pytest.raises(NotPrunableError):
        await prune_source(env.db, env.access, "acme", "repo")
    with pytest.raises(NotFoundError):
        await prune_source(env.db, env.access, "acme", "nothing")
    elsewhere = WriteAccess(owner_id=env.access.owner_id, workspace_ids=frozenset({uuid.uuid4()}))
    with pytest.raises(NotFoundError):
        await prune_source(env.db, elsewhere, "acme", "chat")
