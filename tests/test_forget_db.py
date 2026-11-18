"""Forget (memini.removal.forget_record) against a real Postgres: the hard delete of one record and
everything derived from it, the audit entry, evidence that stays, and a forgotten record that does
not come back through ingest or a git sync; the HTTP route.

Acceptance of memini-2fl: after forget no table contains the record's text (every text, bytea,
json, tsvector and array column of every table is scanned); the audit entry exists. Every value is
synthetic (tests/ingest_helpers.py, tests/git_helpers.py).
"""

import shutil
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString

import anyio
import httpx2
import pytest
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow, dict_row
from pydantic import SecretStr

from git_helpers import SourceRepo
from ingest_helpers import T0, document, thread, tombstone
from memini.api import create_app
from memini.auth import Principal, ReadFilter, Scope, WriteAccess, authorize_read, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, git_sync_handler
from memini.ids import record_id
from memini.ingest import Batch, IngestResult, PayloadIndexer, RecordIn, ingest
from memini.jobs import Job, execute_job, source_lock_key
from memini.knowledge import EvidenceRef, NewItem, get_item, record_item
from memini.removal import AmbiguousRecordError, ForgetTarget, forget_record
from memini.runs import wait_for_run_writes
from memini.sources import (
    NewGitSource,
    NewPushSource,
    SourceBusyError,
    SourceUpdate,
    create_git_source,
    create_push_source,
    update_source,
)
from memini.workspaces import NotFoundError, create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

NEEDLE = "qx7731zephyr"
"""The distinctive text of the record that is forgotten; nothing else holds it."""
SECRET_TEXT = f"The vault code is {NEEDLE}, keep it to yourself."


@dataclass(frozen=True)
class Env:
    url: str
    db: Conn
    pool: Pool
    owner: uuid.UUID
    access: WriteAccess
    acme: uuid.UUID
    other: uuid.UUID
    chat: uuid.UUID
    """A push source in acme."""
    embedder: FakeEmbedder


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        open_pool(migrated_database_url, min_size=1, max_size=6, timeout=5) as pool,
    ):
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        other = await create_workspace(db, owner_id=owner, name="other")
        access = WriteAccess.for_owner(owner)
        chat = await create_push_source(db, access, "acme", NewPushSource(name="chat"))
        await create_push_source(db, access, "other", NewPushSource(name="chat"))
        yield Env(
            migrated_database_url,
            db,
            pool,
            owner,
            access,
            acme.id,
            other.id,
            chat.id,
            FakeEmbedder(),
        )


async def send(env: Env, *records: RecordIn, conn: Conn | None = None) -> IngestResult:
    result = await ingest(
        conn or env.db,
        env.access,
        "acme",
        "chat",
        Batch(records=records),
        runs=env.pool,
        indexer=PayloadIndexer(env.embedder),
    )
    await wait_for_run_writes()
    return result


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    found = await rows(db, query, *params)
    assert len(found) == 1
    return found[0][0]


def rid(env: Env, external_id: str, kind: str = "thread") -> str:
    return record_id(env.chat, kind, external_id)  # pyright: ignore[reportArgumentType]


# Every column a text could hide in: text of any length, bytes, JSON, a lexical vector, an array.
_SCANNED_TYPES = frozenset(
    {"text", "character varying", "character", "bytea", "json", "jsonb", "tsvector", "ARRAY", "xml"}
)


async def holders(db: Conn, needle: str) -> dict[str, int]:
    """``table.column`` -> rows that hold ``needle``, over every scanned column of every table of
    the public schema, as information_schema lists them: a table added later is scanned too."""
    cur = await db.execute(
        "SELECT c.table_name, c.column_name, c.data_type"
        " FROM information_schema.columns c"
        " JOIN information_schema.tables t"
        "   ON t.table_schema = c.table_schema AND t.table_name = c.table_name"
        " WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"
        " ORDER BY 1, 2"
    )
    columns = [tuple(row) for row in await cur.fetchall()]
    assert any(table == "record" for table, _, _ in columns)
    found: dict[str, int] = {}
    for table, column, data_type in columns:
        if data_type not in _SCANNED_TYPES:
            continue
        if data_type == "bytea":
            condition = sql.SQL("position(convert_to(%s, 'UTF8') in {}) > 0")
        else:
            condition = sql.SQL("position(%s in {}::text) > 0")
        query = sql.SQL("SELECT count(*) FROM {} WHERE {}").format(
            sql.Identifier(table), condition.format(sql.Identifier(column))
        )
        count = (await (await db.execute(query, (needle,))).fetchone() or (0,))[0]
        if count:
            found[f"{table}.{column}"] = int(count)
    return found


async def cite(env: Env, record: str, statement: str = "The release moves to Friday.") -> uuid.UUID:
    item = await record_item(
        env.db,
        env.access,
        "acme",
        NewItem(
            kind="decision",
            statement=statement,
            decided_at=T0,
            evidence=[EvidenceRef(record_id=record, anchor="msg-1")],
        ),
    )
    return item.id


def reader(env: Env) -> ReadFilter:
    principal = Principal(principal_id=env.owner, token_id=uuid.uuid4(), scopes=frozenset({"read"}))
    return authorize_read(principal)


# --- The acceptance ------------------------------------------------------------------------------


async def test_after_forget_no_table_holds_the_text_and_the_audit_entry_exists(env: Env) -> None:
    await send(
        env,
        thread("garden/100", text=SECRET_TEXT),
        thread("garden/200", text="Water the tomatoes at six."),
    )
    secret = rid(env, "garden/100")
    item = await cite(env, secret)
    chunks = await scalar(env.db, "SELECT count(*) FROM chunk WHERE record_id = %s", secret)
    assert chunks > 0

    # The scan finds the text where it is: the raw payload, its plain text, the chunk, its
    # lexical vector. Nothing else holds it.
    before = await holders(env.db, NEEDLE)
    assert {"record.payload", "record.normalized_text", "chunk.content", "chunk.lexical"} <= set(
        before
    )

    forgotten = await forget_record(env.db, env.access, "acme", ForgetTarget(record_id=secret))

    assert await holders(env.db, NEEDLE) == {}
    assert (forgotten.record_id, forgotten.source, forgotten.record_kind) == (
        secret,
        "chat",
        "thread",
    )
    assert forgotten.removed.records == 1
    assert forgotten.removed.chunks == chunks
    assert forgotten.citing_items == 1

    # The audit entry: who and when, the record's id, source and kind; no external id.
    audit = await rows(
        env.db,
        "SELECT id, workspace_id, owner_id, token_id, record_id, source_id, record_kind,"
        " forgotten_at FROM forgotten_record",
    )
    assert audit == [
        (
            forgotten.id,
            env.acme,
            env.owner,
            None,
            secret,
            env.chat,
            "thread",
            forgotten.forgotten_at,
        )
    ]
    columns = await rows(
        env.db,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'forgotten_record'",
    )
    assert "external_id" not in {column for (column,) in columns}

    # The record and its index are gone; the other record is untouched.
    assert await rows(env.db, "SELECT id FROM record WHERE source_id = %s", env.chat) == [
        (rid(env, "garden/200"),)
    ]
    assert await scalar(env.db, "SELECT count(*) FROM chunk WHERE record_id = %s", secret) == 0

    # The knowledge item stays, its evidence with the reference, read as forgotten.
    chain = await get_item(env.db, reader(env), item)
    evidence = chain.item.evidence[0]
    assert chain.item.status == "active"
    assert (evidence.record_state, evidence.external_id, evidence.source) == (
        "forgotten",
        "garden/100",
        "chat",
    )


async def test_a_forgotten_record_is_not_stored_again(env: Env) -> None:
    await send(env, thread("garden/100", text=SECRET_TEXT))
    await forget_record(env.db, env.access, "acme", ForgetTarget(record_id=rid(env, "garden/100")))

    again = await send(
        env, thread("garden/100", text=SECRET_TEXT), thread("garden/300", text="Fresh news.")
    )

    assert (again.inserted, again.forgotten, again.unchanged) == (1, 1, 0)
    assert await holders(env.db, NEEDLE) == {}
    report = await scalar(
        env.db, "SELECT report FROM run WHERE kind = 'ingest' ORDER BY started_at DESC LIMIT 1"
    )
    assert report["counts"]["records_forgotten"] == 1
    assert report["counts"]["records_inserted"] == 1

    # A tombstone of it changes nothing either.
    gone = await send(env, tombstone("garden/100"))
    assert (gone.deleted, gone.unchanged, gone.forgotten) == (0, 1, 0)


async def test_an_ingest_waiting_for_the_row_sees_the_forget_it_waited_for(env: Env) -> None:
    await send(env, thread("garden/100", text=SECRET_TEXT))
    result: dict[str, IngestResult] = {}

    async with (
        connect(env.url, autocommit=True) as forgetting,
        connect(env.url, autocommit=True) as sending,
    ):

        async def send_again() -> None:
            result["batch"] = await send(env, thread("garden/100", text=SECRET_TEXT), conn=sending)

        async with anyio.create_task_group() as group, forgetting.transaction():
            await forget_record(
                forgetting, env.access, "acme", ForgetTarget(record_id=rid(env, "garden/100"))
            )
            group.start_soon(send_again)
            # The batch waits for the row the uncommitted forget deleted.
            with anyio.fail_after(10):
                while True:
                    state = await scalar(
                        env.db,
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                        sending.info.backend_pid,
                    )
                    if state == "Lock":
                        break
                    await anyio.sleep(0.02)

    assert (result["batch"].inserted, result["batch"].forgotten) == (0, 1)
    assert await holders(env.db, NEEDLE) == {}


# --- Naming the record ---------------------------------------------------------------------------


async def test_forget_by_source_and_external_id_and_kind(env: Env) -> None:
    await send(
        env,
        thread("shared-id", text=SECRET_TEXT),
        document("shared-id", "# Handbook\n\nWater at six."),
    )

    with pytest.raises(AmbiguousRecordError):
        await forget_record(
            env.db, env.access, "acme", ForgetTarget(source="chat", external_id="shared-id")
        )

    forgotten = await forget_record(
        env.db,
        env.access,
        "acme",
        ForgetTarget(source="chat", external_id="shared-id", kind="thread"),
    )

    assert forgotten.record_id == rid(env, "shared-id")
    assert await rows(env.db, "SELECT kind FROM record WHERE source_id = %s", env.chat) == [
        ("document",)
    ]
    # The document is now the only record under that id: no kind needed.
    await forget_record(
        env.db, env.access, "acme", ForgetTarget(source="chat", external_id="shared-id")
    )
    assert await rows(env.db, "SELECT kind FROM record WHERE source_id = %s", env.chat) == []


async def test_a_tombstone_is_forgotten_with_its_payload(env: Env) -> None:
    await send(env, thread("garden/100", text=SECRET_TEXT))
    await send(env, tombstone("garden/100"))
    assert "record.payload" in await holders(env.db, NEEDLE)  # a tombstone keeps its payload

    await forget_record(env.db, env.access, "acme", ForgetTarget(record_id=rid(env, "garden/100")))

    assert await holders(env.db, NEEDLE) == {}


async def test_what_cannot_be_found_or_changed_is_not_found(env: Env) -> None:
    await send(env, thread("garden/100", text=SECRET_TEXT))
    secret = rid(env, "garden/100")
    in_other = WriteAccess(owner_id=env.owner, workspace_ids=frozenset({env.other}))

    cases: list[tuple[WriteAccess, str, ForgetTarget]] = [
        (env.access, "acme", ForgetTarget(record_id="ab" * 32)),
        (env.access, "acme", ForgetTarget(source="chat", external_id="garden/999")),
        (env.access, "acme", ForgetTarget(source="nope", external_id="garden/100")),
        (env.access, "other", ForgetTarget(record_id=secret)),  # in another workspace
        (env.access, "nowhere", ForgetTarget(record_id=secret)),
        (in_other, "acme", ForgetTarget(record_id=secret)),  # a token limited to another one
    ]
    for access, workspace, target in cases:
        with pytest.raises(NotFoundError):
            await forget_record(env.db, access, workspace, target)

    assert "record.payload" in await holders(env.db, NEEDLE)
    assert await rows(env.db, "SELECT id FROM forgotten_record") == []


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"source": "chat"},
        {"external_id": "garden/100"},
        {"record_id": "ab" * 32, "source": "chat", "external_id": "garden/100"},
        {"record_id": "AB" * 32},
        {"record_id": "ab" * 31},
        {"record_id": "ab" * 32, "kind": "thread"},
    ],
)
def test_a_target_names_its_record_one_way(fields: Mapping[str, Any]) -> None:
    with pytest.raises(ValueError, match="record"):
        ForgetTarget(**fields)


async def test_forget_waits_for_no_running_job_of_the_source(env: Env) -> None:
    await send(env, thread("garden/100", text=SECRET_TEXT))

    async with connect(env.url, autocommit=True) as worker:
        await worker.execute("SELECT pg_advisory_lock(%s, %s)", source_lock_key(env.chat))
        with pytest.raises(SourceBusyError):
            await forget_record(
                env.db, env.access, "acme", ForgetTarget(record_id=rid(env, "garden/100"))
            )

    assert "record.payload" in await holders(env.db, NEEDLE)
    assert await rows(env.db, "SELECT id FROM forgotten_record") == []


# --- Over HTTP -----------------------------------------------------------------------------------


async def bearer(
    env: Env, *scopes: Scope, workspace_id: uuid.UUID | None = None
) -> tuple[dict[str, str], uuid.UUID]:
    new = await create_token(
        env.db, owner_id=env.owner, name="test agent", scopes=scopes, workspace_id=workspace_id
    )
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}, new.info.id


async def test_the_forget_route(env: Env) -> None:
    await send(
        env,
        thread("garden/100", text=SECRET_TEXT),
        thread("dup", text="A thread."),
        document("dup", "# Handbook\n\nA document."),
    )
    secret = rid(env, "garden/100")
    await cite(env, secret)
    admin, admin_id = await bearer(env, "admin")
    reader_only, _ = await bearer(env, "read", "ingest")
    other_admin, _ = await bearer(env, "admin", workspace_id=env.other)
    app = create_app(Settings(database_url=SecretStr(env.url)), pool=env.pool)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:

        async def post(body: Mapping[str, Any], headers: Mapping[str, str]) -> httpx2.Response:
            return await client.post("/v1/workspaces/acme/forget", json=body, headers=headers)

        denied = await post({"record_id": secret}, reader_only)
        limited = await post({"record_id": secret}, other_admin)
        both = await post({"record_id": secret, "source": "chat"}, admin)
        ambiguous = await post({"source": "chat", "external_id": "dup"}, admin)
        async with connect(env.url, autocommit=True) as worker:
            await worker.execute("SELECT pg_advisory_lock(%s, %s)", source_lock_key(env.chat))
            busy = await post({"record_id": secret}, admin)
        done = await post({"source": "chat", "external_id": "garden/100"}, admin)
        again = await post({"record_id": secret}, admin)

    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "insufficient_scope")
    assert (limited.status_code, limited.json()["error"]["code"]) == (404, "not_found")
    assert (both.status_code, both.json()["error"]["code"]) == (422, "validation_error")
    assert (ambiguous.status_code, ambiguous.json()["error"]["code"]) == (422, "ambiguous_record")
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "source_busy")
    assert (again.status_code, again.json()["error"]["code"]) == (404, "not_found")

    assert done.status_code == 200, done.text
    body = done.json()
    assert {key: body[key] for key in ("workspace", "record_id", "source", "record_kind")} == {
        "workspace": "acme",
        "record_id": secret,
        "source": "chat",
        "record_kind": "thread",
    }
    assert body["removed"]["chunks"] > 0
    assert body["removed"]["raw_responses"] == 0
    assert body["citing_items"] == 1
    assert NEEDLE not in done.text
    assert await rows(env.db, "SELECT id, token_id FROM forgotten_record") == [
        (uuid.UUID(body["id"]), admin_id)
    ]
    assert await holders(env.db, NEEDLE) == {}


# --- A git source --------------------------------------------------------------------------------

MAIN = "refs/heads/main"
CODE = '''def vault_code():
    """The code of the vault."""
    return "qx7731zephyr"
'''
OTHER = '''def water(plants):
    """Water the plants."""
    return [plant for plant in plants]
'''


@pytest.mark.git
@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable")
async def test_a_forgotten_file_is_not_synced_again(env: Env, tmp_path: Path) -> None:
    repos = tmp_path / "sources"
    repo = SourceRepo(repos / "shop")
    repo.commit({"src/vault.py": CODE, "src/water.py": OTHER})
    options = GitOptions(
        mirror_dir=tmp_path / "mirrors", local_roots=(repos,), fetch_timeout=30, command_timeout=30
    )
    source = await create_git_source(
        env.db,
        env.access,
        "acme",
        NewGitSource(name="shop", location=str(repo.path), refs=["main"]),
        options=options,
    )
    stream = await scalar(env.db, "SELECT id FROM stream WHERE source_id = %s", source.id)
    handler = git_sync_handler(env.pool, MirrorManager(options), env.embedder)

    async def sync() -> Mapping[str, Any]:
        job = Job(
            id=uuid.uuid4(),
            workspace_id=env.acme,
            owner_id=env.owner,
            source_id=source.id,
            stream_id=stream,
            kind=GIT_SYNC,
            args={},
            attempt=1,
            max_attempts=5,
            worker="test",
        )
        await execute_job(job, {GIT_SYNC: handler}, env.pool)
        await wait_for_run_writes()
        async with env.db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM run WHERE stream_id = %s ORDER BY started_at DESC LIMIT 1",
                (stream,),
            )
            run = await cur.fetchone()
        assert run is not None
        assert run["status"] == "succeeded", run
        counts: Mapping[str, Any] = run["report"]["counts"]
        return counts

    async def files() -> list[str]:
        found = await rows(
            env.db,
            "SELECT external_id FROM record WHERE source_id = %s AND deleted_at IS NULL"
            " ORDER BY external_id",
            source.id,
        )
        return [external_id for (external_id,) in found]

    await sync()
    assert "chunk.content" in await holders(env.db, NEEDLE)

    await forget_record(
        env.db,
        env.access,
        "acme",
        ForgetTarget(source="shop", external_id=f"{MAIN}:src/vault.py", kind="code_file"),
    )
    assert await holders(env.db, NEEDLE) == {}

    # A commit that changes the file: the diff names it, and the sync skips it.
    repo.commit({"src/vault.py": CODE + "\n# rotated\n"})
    diffed = await sync()
    # Changed rules: the ref is listed in full, and the sync skips it again.
    await update_source(env.db, env.access, "acme", "shop", SourceUpdate(exclude=["docs/**"]))
    listed = await sync()

    assert diffed["files_skipped_forgotten"] == 1
    assert listed["files_skipped_forgotten"] == 1
    assert await files() == [f"{MAIN}:src/water.py"]
    assert await holders(env.db, NEEDLE) == {}


async def test_forget_keeps_working_after_its_source_is_gone(env: Env) -> None:
    """The audit entry outlives the source; the evidence then reads as forgotten still."""
    await send(env, thread("garden/100", text=SECRET_TEXT))
    secret = rid(env, "garden/100")
    item = await cite(env, secret)
    await forget_record(env.db, env.access, "acme", ForgetTarget(record_id=secret))

    await env.db.execute("DELETE FROM source WHERE id = %s", (env.chat,))

    assert await scalar(env.db, "SELECT count(*) FROM forgotten_record") == 1
    evidence = (await get_item(env.db, reader(env), item)).item.evidence[0]
    assert (evidence.record_state, evidence.source) == ("forgotten", None)


async def test_forgetting_twice_finds_nothing_the_second_time(env: Env) -> None:
    await send(env, thread("garden/100", text=SECRET_TEXT))
    target = ForgetTarget(record_id=rid(env, "garden/100"))
    await forget_record(env.db, env.access, "acme", target)

    with pytest.raises(NotFoundError):
        await forget_record(env.db, env.access, "acme", target)

    assert await scalar(env.db, "SELECT count(*) FROM forgotten_record") == 1
