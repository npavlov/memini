"""Stream health against a real Postgres: failing runs, records stored but not indexed, a stale
index, history, git pins, and ``GET /v1/status`` beside the MCP tool.

Run with `make test-integration`. Everything is synthetic: names, records, errors, commits.
The definitions are in :mod:`memini.status`.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString

import httpx2
import pytest
from fastapi import FastAPI
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from ingest_helpers import HOUR, iso, thread_payload
from memini.api import create_app
from memini.auth import Principal, Scope, authorize_read, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import EmbeddingSpec, FakeEmbedder
from memini.index import default_raw_content, reindex
from memini.jobs import Job, execute_job
from memini.status import ServiceStatus, read_status, service_fingerprint
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

RECORDS = "/v1/workspaces/alpha/sources/chat/records"
COMMIT = "c0ffee" + "0" * 34
OTHER_MODEL = EmbeddingSpec(provider="openai_compatible", model="fake-embedder-two", dimension=8)


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def owner(db: Conn) -> uuid.UUID:
    return await ensure_principal(db, "test-owner")


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool:
        yield pool


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings(database_url=SecretStr(migrated_database_url))


async def bearer(
    db: Conn, owner: uuid.UUID, *scopes: Scope, workspace_id: uuid.UUID | None = None
) -> dict[str, str]:
    new = await create_token(
        db, owner_id=owner, name="test client", scopes=scopes, workspace_id=workspace_id
    )
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


def client_for(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://memini.test"
    )


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def status_of(app: FastAPI, headers: dict[str, str]) -> Json:
    async with client_for(app) as client:
        return ok(await client.get("/v1/status", headers=headers))


def streams_of(status: Json, workspace: str = "alpha") -> dict[str, Json]:
    (found,) = (w for w in status["workspaces"] if w["name"] == workspace)
    return {stream["key"]: stream for stream in found["streams"]}


def index_of(status: Json, workspace: str = "alpha") -> Json:
    (found,) = (w for w in status["workspaces"] if w["name"] == workspace)
    index: Json = found["index"]
    return index


def recent_batch(*external_ids: str, stream: str = "chat/general") -> Json:
    """Threads in a complete window that ends a minute ago, so the checkpoint is recent."""
    end = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    start = end - 2 * HOUR
    return {
        "stream": stream,
        "window": {"from": iso(start), "to": iso(end), "complete": True},
        "records": [
            {
                "kind": "thread",
                "external_id": external_id,
                "occurred_at": iso(start + HOUR),
                "payload": thread_payload(start + HOUR),
            }
            for external_id in external_ids
        ],
    }


async def push_setup(app: FastAPI, admin: dict[str, str]) -> None:
    """Workspaces alpha and beta, with a push source chat in alpha."""
    async with client_for(app) as client:
        for name in ("alpha", "beta"):
            ok(await client.post("/v1/workspaces", json={"name": name}, headers=admin), 201)
        ok(
            await client.post(
                "/v1/workspaces/alpha/sources",
                json={"kind": "push", "name": "chat"},
                headers=admin,
            ),
            201,
        )


async def push(app: FastAPI, headers: dict[str, str], batch: Json) -> Json:
    async with client_for(app) as client:
        return ok(await client.post(RECORDS, json=batch, headers=headers))


@pytest.fixture
async def admin(db: Conn, owner: uuid.UUID) -> dict[str, str]:
    return await bearer(db, owner, "read", "ingest", "admin")


# --- A failing stream -----------------------------------------------------------------------------


async def insert_git_stream(
    db: Conn, owner: uuid.UUID, workspace: str, *, checked_ago: str, cursor: Json | None
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A git source ``code`` with one stream refs/heads/main, scheduled every 15 minutes."""
    workspace_id = await scalar(
        db,
        "INSERT INTO workspace (owner_id, name) VALUES (%s, %s) RETURNING id",
        owner,
        workspace,
    )
    source_id = await scalar(
        db,
        "INSERT INTO source (workspace_id, owner_id, name, kind)"
        " VALUES (%s, %s, 'code', 'repository')"
        " RETURNING id",
        workspace_id,
        owner,
    )
    stream_id = await scalar(
        db,
        "INSERT INTO stream (workspace_id, owner_id, source_id, key, checked_until, schedule,"
        " cursor) VALUES (%s, %s, %s, 'refs/heads/main', now() - %s::interval,"
        " interval '15 minutes', %s) RETURNING id",
        workspace_id,
        owner,
        source_id,
        checked_ago,
        None if cursor is None else Jsonb(cursor),
    )
    return workspace_id, source_id, stream_id


def sync_job(
    owner: uuid.UUID, workspace_id: uuid.UUID, source_id: uuid.UUID, stream_id: uuid.UUID
) -> Job:
    return Job(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        owner_id=owner,
        source_id=source_id,
        stream_id=stream_id,
        kind="git_sync",
        args={},
        attempt=1,
        max_attempts=5,
        worker="test/0",
    )


def reader(owner: uuid.UUID, workspace_id: uuid.UUID | None = None) -> Principal:
    return Principal(
        principal_id=owner,
        token_id=uuid.uuid4(),
        scopes=frozenset({"read"}),
        workspace_id=workspace_id,
    )


async def test_a_failing_stream_shows_its_error_failures_and_lag(
    db: Conn, owner: uuid.UUID, pool: Pool
) -> None:
    workspace_id, source_id, stream_id = await insert_git_stream(
        db,
        owner,
        "alpha",
        checked_ago="3 hours",
        cursor={"commit": COMMIT, "rules": "r" * 64, "partial": True},
    )
    job = sync_job(owner, workspace_id, source_id, stream_id)

    async def failing(_job: Job) -> None:
        # Two lines: status shows the first only.
        raise RuntimeError("the remote hung up\nDETAIL: a synthetic second line")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await execute_job(job, {"git_sync": failing}, pool)

    access = authorize_read(reader(owner))
    status = await read_status(pool, access, fingerprint=None)

    assert status.current is False
    (stream,) = status.workspaces[0].streams
    assert stream.current is False
    assert stream.behind == ["lag"]
    assert stream.lag_seconds is not None
    assert stream.lag_seconds >= 3 * 3600
    assert stream.failures == 2
    assert stream.last_run is not None
    assert stream.last_failure is not None
    assert stream.last_run.status == "failed"
    assert stream.last_run.kind == "git_sync"
    assert stream.last_run.error == "RuntimeError: the remote hung up"
    assert stream.last_failure.id == stream.last_run.id
    assert stream.git is not None
    assert (stream.git.commit, stream.git.partial) == (COMMIT, True)
    assert stream.missing is False
    # The whole error stays on the run.
    stored = await scalar(db, "SELECT error FROM run WHERE id = %s", stream.last_run.id)
    assert "DETAIL: a synthetic second line" in stored

    async def succeeding(_job: Job) -> None:
        await db.execute(
            "UPDATE stream SET checked_until = now(), cursor = %s WHERE id = %s",
            (Jsonb({"commit": COMMIT, "rules": "r" * 64}), stream_id),
        )

    await execute_job(job, {"git_sync": succeeding}, pool)
    status = await read_status(pool, access, fingerprint=None)

    (stream,) = status.workspaces[0].streams
    assert status.current is True
    assert (stream.current, stream.behind, stream.failures) == (True, [], 0)
    assert stream.last_failure is None
    assert stream.last_run is not None
    assert (stream.last_run.status, stream.last_run.error) == ("succeeded", None)
    assert stream.git is not None
    assert stream.git.partial is False


async def test_a_partial_run_shows_its_reason_and_ends_the_failures(
    db: Conn, owner: uuid.UUID, pool: Pool
) -> None:
    workspace_id, source_id, stream_id = await insert_git_stream(
        db, owner, "alpha", checked_ago="1 minute", cursor=None
    )
    for status, error in (("failed", "OSError: no space"), ("partial", "a window is incomplete")):
        await db.execute(
            "INSERT INTO run (workspace_id, owner_id, source_id, stream_id, kind, status,"
            " started_at, finished_at, error) VALUES (%s, %s, %s, %s, 'git_sync', %s,"
            " clock_timestamp(), clock_timestamp(), %s)",
            (workspace_id, owner, source_id, stream_id, status, error),
        )

    status = await read_status(pool, authorize_read(reader(owner)), fingerprint=None)

    (stream,) = status.workspaces[0].streams
    assert stream.last_run is not None
    assert (stream.last_run.status, stream.last_run.error) == ("partial", "a window is incomplete")
    assert (stream.failures, stream.last_failure) == (0, None)
    assert stream.git is not None
    assert stream.git.commit is None


# --- Records stored but not indexed ---------------------------------------------------------------


async def test_records_stored_while_embeddings_were_off_are_behind(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, admin: dict[str, str]
) -> None:
    off = create_app(settings, pool=pool)  # no embedding provider in the settings
    await push_setup(off, admin)
    stored = await push(off, admin, recent_batch("general/0001", "general/0002", "general/0003"))
    assert stored["index"]["indexed"] is False

    # Without a provider the records are counted, not judged: nothing is searched either way.
    status = await status_of(off, admin)
    assert status["index"]["state"] == "off"
    assert index_of(status)["state"] == "off"
    assert index_of(status)["records_not_indexed"] == 3
    assert index_of(status)["behind"] is False
    stream = streams_of(status)["chat/general"]
    assert (stream["records"], stream["records_not_indexed"]) == (3, 3)
    assert stream["behind"] == []
    assert stream["current"] is None  # a push stream has no schedule
    assert status["current"] is True

    # A provider is set now: those records are behind until a reindex.
    embedder = FakeEmbedder()
    on = create_app(settings, pool=pool, embedder=embedder)
    status = await status_of(on, admin)

    assert status["current"] is False
    assert status["index"]["state"] == "behind"
    assert status["index"]["configured"] == embedder.spec.describe()
    assert "`memini reindex --workspace alpha`" in status["index"]["note"]
    index = index_of(status)
    assert index["state"] == "none"  # nothing was ever indexed in alpha
    assert (index["records"], index["records_not_indexed"], index["chunks"]) == (3, 3, 0)
    assert index["behind"] is True
    assert index["reindex"] == "memini reindex --workspace alpha"
    stream = streams_of(status)["chat/general"]
    assert stream["behind"] == ["not_indexed"]
    assert stream["current"] is False
    beta = index_of(status, "beta")
    assert (beta["state"], beta["behind"], beta["records"]) == ("none", False, 0)

    workspace_id = await scalar(db, "SELECT id FROM workspace WHERE name = 'alpha'")
    async with connect(settings.database_url.get_secret_value(), autocommit=True) as conn:
        await reindex(conn, embedder, workspace_id=workspace_id, raw=default_raw_content(conn))
    status = await status_of(on, admin)

    assert status["current"] is True
    assert status["index"]["state"] == "ok"
    index = index_of(status)
    assert (index["state"], index["records_not_indexed"], index["behind"]) == ("ok", 0, False)
    assert index["chunks"] >= 3
    assert index["reindex"] is None
    assert index["built_with"] == embedder.spec.describe()
    stream = streams_of(status)["chat/general"]
    assert (stream["behind"], stream["current"], stream["records_not_indexed"]) == ([], None, 0)


async def test_records_indexed_at_ingest_are_not_behind_and_a_scheduled_stream_is_current(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, admin: dict[str, str]
) -> None:
    on = create_app(settings, pool=pool, embedder=FakeEmbedder())
    await push_setup(on, admin)
    stored = await push(on, admin, recent_batch("general/0001", "general/0002"))
    assert stored["index"]["indexed"] is True
    await db.execute("UPDATE stream SET schedule = interval '1 hour'")
    await db.execute("UPDATE record SET deleted_at = now() WHERE external_id = 'general/0002'")

    status = await status_of(on, admin)

    assert status["current"] is True
    assert status["index"]["state"] == "ok"
    stream = streams_of(status)["chat/general"]
    assert stream["current"] is True
    assert stream["behind"] == []
    assert 0 <= stream["lag_seconds"] < 3600
    assert (stream["records"], stream["records_not_indexed"]) == (1, 0)  # a tombstone is not live
    assert stream["last_run"]["kind"] == "ingest"
    assert stream["last_run"]["status"] == "succeeded"
    assert stream["git"] is None
    assert stream["checked_from"] is not None
    assert stream["history_start"] is None  # nobody plans a push stream
    assert stream["history_loaded"] is None
    index = index_of(status)
    assert (index["state"], index["behind"], index["records"]) == ("ok", False, 1)


# --- A stale index --------------------------------------------------------------------------------


async def test_a_stale_fingerprint_marks_the_workspace_behind_and_names_reindex(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, admin: dict[str, str]
) -> None:
    built = create_app(settings, pool=pool, embedder=FakeEmbedder())
    await push_setup(built, admin)
    await push(built, admin, recent_batch("general/0001"))
    assert (await status_of(built, admin))["current"] is True

    other = FakeEmbedder(OTHER_MODEL)
    changed = create_app(settings, pool=pool, embedder=other)
    status = await status_of(changed, admin)

    assert status["current"] is False
    assert status["index"]["state"] == "stale"
    assert "`memini reindex --workspace alpha`" in status["index"]["note"]
    index = index_of(status)
    assert index["state"] == "stale"
    assert index["changed"] == ["model"]
    assert index["built_with"] == FakeEmbedder().spec.describe()
    assert index["behind"] is True
    assert index["reindex"] == "memini reindex --workspace alpha"
    assert index["records_not_indexed"] == 0
    stream = streams_of(status)["chat/general"]
    assert stream["behind"] == ["index_stale"]
    assert stream["current"] is False
    # beta has no index, so nothing in it is stale.
    assert index_of(status, "beta")["behind"] is False

    # The route answers what the library reads with the service's embedder.
    access = authorize_read(reader(owner))
    library = await read_status(pool, access, fingerprint=service_fingerprint(other))
    answered = ServiceStatus.model_validate(status)
    assert answered.index == library.index
    assert [w.index for w in answered.workspaces] == [w.index for w in library.workspaces]


# --- History and git ------------------------------------------------------------------------------


async def test_history_is_loaded_back_to_the_start_the_planner_recorded(
    db: Conn, owner: uuid.UUID, pool: Pool
) -> None:
    workspace_id = await scalar(
        db, "INSERT INTO workspace (owner_id, name) VALUES (%s, 'alpha') RETURNING id", owner
    )
    source_id = await scalar(
        db,
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, 'mail', 'mcp')"
        " RETURNING id",
        workspace_id,
        owner,
    )
    stream_id = await scalar(
        db,
        "INSERT INTO stream (workspace_id, owner_id, source_id, key, checked_from, checked_until,"
        " schedule) VALUES (%s, %s, %s, 'inbox', now() - interval '10 days',"
        " now() - interval '1 minute', interval '1 hour') RETURNING id",
        workspace_id,
        owner,
        source_id,
    )
    history_start = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0)
    report: Json = {
        "version": 1,
        "duration_ms": 10.0,
        "steps": [],
        "steps_omitted": 0,
        "counts": {},
        "llm": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_model": {}},
        "details": {"plan": {"history_start": iso(history_start), "windows": []}},
    }
    plans: list[Json | None] = [report, None]
    for plan in plans:
        # The newest run recorded no plan: the plan before it still counts.
        await db.execute(
            "INSERT INTO run (workspace_id, owner_id, source_id, stream_id, kind, status,"
            " started_at, finished_at, report) VALUES (%s, %s, %s, %s, 'mcp_sync',"
            " 'succeeded', clock_timestamp(), clock_timestamp(), %s)",
            (workspace_id, owner, source_id, stream_id, Jsonb(plan or {})),
        )
    access = authorize_read(reader(owner))

    (stream,) = (await read_status(pool, access, fingerprint=None)).workspaces[0].streams
    assert stream.history_start == history_start
    assert stream.history_loaded is False
    assert stream.checked_from is not None
    assert stream.git is None
    assert stream.current is True  # history still loading is not behind

    await db.execute(
        "UPDATE stream SET checked_from = now() - interval '40 days' WHERE id = %s", (stream_id,)
    )
    (stream,) = (await read_status(pool, access, fingerprint=None)).workspaces[0].streams
    assert stream.history_loaded is True


async def test_a_missing_ref_is_reported_without_a_pin(
    db: Conn, owner: uuid.UUID, pool: Pool
) -> None:
    await insert_git_stream(
        db,
        owner,
        "alpha",
        checked_ago="1 minute",
        cursor={"missing": True, "last_commit": COMMIT},
    )

    status = await read_status(pool, authorize_read(reader(owner)), fingerprint=None)

    (stream,) = status.workspaces[0].streams
    assert stream.missing is True
    assert stream.git is not None
    assert (stream.git.commit, stream.git.partial) == (None, False)
    assert stream.current is True


# --- The route ------------------------------------------------------------------------------------


async def test_the_route_reads_through_the_token_s_workspace_limit(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, admin: dict[str, str]
) -> None:
    off = create_app(settings, pool=pool)
    await push_setup(off, admin)
    await push(off, admin, recent_batch("general/0001", "general/0002"))
    beta = await scalar(db, "SELECT id FROM workspace WHERE name = 'beta'")
    on = create_app(settings, pool=pool, embedder=FakeEmbedder())

    limited = await status_of(on, await bearer(db, owner, "read", workspace_id=beta))

    # alpha's records are behind, but this token cannot see alpha: its base is current.
    assert [w["name"] for w in limited["workspaces"]] == ["beta"]
    assert limited["current"] is True
    assert index_of(limited, "beta")["records"] == 0
    assert limited["index"]["state"] == "none"

    async with client_for(on) as client:
        refused = await client.get("/v1/status", headers=await bearer(db, owner, "ingest", "admin"))
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "insufficient_scope"


async def test_the_route_says_whether_the_mcp_endpoint_is_served(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings
) -> None:
    headers = await bearer(db, owner, "read")
    served = create_app(settings, pool=pool)
    switched_off = create_app(settings.model_copy(update={"mcp_enabled": False}), pool=pool)

    assert (await status_of(served, headers))["mcp_enabled"] is True
    answer = await status_of(switched_off, headers)

    # The base is read as before; only the endpoint is gone, for a valid token too.
    assert answer["mcp_enabled"] is False
    assert answer["database"]["reachable"] is True
    assert answer["current"] is True
    async with client_for(switched_off) as client:
        refused = await client.post("/mcp", headers=headers, json={})
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "mcp_disabled"


# --- The gate -------------------------------------------------------------------------------------


async def gate_of(app: FastAPI, headers: dict[str, str]) -> httpx2.Response:
    async with client_for(app) as client:
        return await client.get("/v1/status", params={"gate": "true"}, headers=headers)


async def test_the_gate_answers_503_for_a_lagging_stream_and_for_an_index_behind(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, admin: dict[str, str]
) -> None:
    on = create_app(settings, pool=pool, embedder=FakeEmbedder())
    await push_setup(on, admin)
    await push(on, admin, recent_batch("general/0001"))

    current = await gate_of(on, admin)
    assert current.status_code == 200
    assert current.json()["current"] is True

    # A scheduled stream three hours behind a 15-minute schedule.
    *_, stream_id = await insert_git_stream(
        db, owner, "gamma", checked_ago="3 hours", cursor={"commit": COMMIT, "rules": "r" * 64}
    )
    lagging = await gate_of(on, admin)
    assert lagging.status_code == 503
    assert lagging.json()["current"] is False
    assert streams_of(lagging.json(), "gamma")["refs/heads/main"]["behind"] == ["lag"]

    await db.execute("UPDATE stream SET checked_until = now() WHERE id = %s", (stream_id,))
    assert (await gate_of(on, admin)).status_code == 200

    # The same records, searched with another model: alpha's index is stale, so behind.
    changed = create_app(settings, pool=pool, embedder=FakeEmbedder(OTHER_MODEL))
    stale = await gate_of(changed, admin)
    assert stale.status_code == 503
    assert index_of(stale.json())["behind"] is True
    assert index_of(stale.json())["reindex"] == "memini reindex --workspace alpha"
    assert streams_of(stale.json())["chat/general"]["behind"] == ["index_stale"]
