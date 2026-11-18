"""``GET /metrics`` against a real Postgres: freshness as the gate sees it, the job queue, the pool,
runs, embeddings, statements; and no name of a workspace, source, stream or record in any of it.

Run with `make test-integration`. Every value is synthetic.
"""

import re
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString

import httpx2
import pytest
from fastapi import FastAPI
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from ingest_helpers import HOUR, iso, thread_payload
from memini.api import create_app
from memini.auth import create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import EmbeddingSpec, FakeEmbedder
from memini.jobs import enqueue
from memini.metrics import EMBEDDING_REQUESTS, EMBEDDING_TEXTS, RUNS
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

METRICS_TOKEN = "synthetic-metrics-" + secrets.token_hex(16)
WORKSPACE = "synthetic-alpha-workspace"
LAGGING = "synthetic-gamma-workspace"
SOURCE = "synthetic-chat-source"
CHANNEL = "synthetic-channel"
THREAD = "synthetic-thread-0001"
OTHER_MODEL = EmbeddingSpec(provider="openai_compatible", model="fake-embedder-two", dimension=8)
_SAMPLE = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)")


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
def app(migrated_database_url: str, pool: Pool) -> FastAPI:
    settings = Settings(
        database_url=SecretStr(migrated_database_url), metrics_token=SecretStr(METRICS_TOKEN)
    )
    return create_app(settings, pool=pool, embedder=FakeEmbedder())


@pytest.fixture
async def admin(db: Conn, owner: uuid.UUID) -> dict[str, str]:
    new = await create_token(
        db, owner_id=owner, name="test client", scopes=("read", "ingest", "admin")
    )
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


def client_for(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://memini.test"
    )


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def scrape(app: FastAPI) -> str:
    async with client_for(app) as client:
        response = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"}
        )
    assert response.status_code == 200, response.text
    return response.text


def value(text: str, name: str, labels: str = "") -> float:
    for line in text.splitlines():
        match = _SAMPLE.fullmatch(line)
        if match and match.group(1) == name and (match.group(2) or "") == labels:
            return float(match.group(3))
    raise AssertionError(f"no sample {name}{{{labels}}}")


async def push_a_thread(app: FastAPI, admin: dict[str, str]) -> None:
    end = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    start = end - 2 * HOUR
    batch = {
        "stream": CHANNEL,
        "window": {"from": iso(start), "to": iso(end), "complete": True},
        "records": [
            {
                "kind": "thread",
                "external_id": THREAD,
                "occurred_at": iso(start + HOUR),
                "payload": thread_payload(start + HOUR),
            }
        ],
    }
    async with client_for(app) as client:
        for request in (
            client.post("/v1/workspaces", json={"name": WORKSPACE}, headers=admin),
            client.post(
                f"/v1/workspaces/{WORKSPACE}/sources",
                json={"kind": "push", "name": SOURCE},
                headers=admin,
            ),
            client.post(
                f"/v1/workspaces/{WORKSPACE}/sources/{SOURCE}/records", json=batch, headers=admin
            ),
        ):
            response = await request
            assert response.status_code in (200, 201), response.text


async def lagging_stream(db: Conn, owner: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A git stream three hours behind its 15-minute schedule, in a workspace of its own."""
    workspace_id = await scalar(
        db, "INSERT INTO workspace (owner_id, name) VALUES (%s, %s) RETURNING id", owner, LAGGING
    )
    source_id = await scalar(
        db,
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, %s, 'git')"
        " RETURNING id",
        workspace_id,
        owner,
        SOURCE,
    )
    stream_id = await scalar(
        db,
        "INSERT INTO stream (workspace_id, owner_id, source_id, key, checked_until, schedule)"
        " VALUES (%s, %s, %s, 'refs/heads/synthetic', now() - interval '3 hours',"
        " interval '15 minutes') RETURNING id",
        workspace_id,
        owner,
        source_id,
    )
    return workspace_id, source_id, stream_id


async def test_metrics_report_freshness_queue_pool_and_work_without_names(
    app: FastAPI, db: Conn, owner: uuid.UUID, admin: dict[str, str]
) -> None:
    runs = RUNS.value(kind="ingest", status="succeeded")
    batches = EMBEDDING_REQUESTS.value(provider="openai_compatible", kind="document", outcome="ok")
    texts = EMBEDDING_TEXTS.value(provider="openai_compatible", kind="document")

    await push_a_thread(app, admin)
    text = await scrape(app)

    assert value(text, "memini_database_reachable") == 1
    assert value(text, "memini_base_current") == 1
    assert value(text, "memini_streams", 'current="unjudged"') == 1
    assert value(text, "memini_records") == 1
    assert value(text, "memini_records_not_indexed") == 0
    assert value(text, "memini_chunks") >= 1
    assert value(text, "memini_workspaces", 'index="ok"') == 1
    assert value(text, "memini_index_state", 'state="ok"') == 1
    assert RUNS.value(kind="ingest", status="succeeded") == runs + 1
    assert (
        EMBEDDING_REQUESTS.value(provider="openai_compatible", kind="document", outcome="ok")
        > batches
    )
    assert EMBEDDING_TEXTS.value(provider="openai_compatible", kind="document") > texts
    assert value(text, "memini_db_pool_max") == 4
    assert value(text, "memini_db_pool_connections") >= 1
    assert value(text, "memini_db_pool_requests_total") > 0
    assert value(text, "memini_db_statement_duration_seconds_count") > 0
    assert value(text, "memini_job_wait_max_seconds") == 0  # nothing queued

    workspace_id, source_id, stream_id = await lagging_stream(db, owner)
    await enqueue(
        db,
        workspace_id=workspace_id,
        owner_id=owner,
        kind="git_sync",
        source_id=source_id,
        stream_id=stream_id,
        dedupe_key=str(stream_id),
    )
    text = await scrape(app)

    assert value(text, "memini_base_current") == 0
    assert value(text, "memini_streams", 'current="false"') == 1
    assert value(text, "memini_streams_behind", 'reason="lag"') == 1
    assert value(text, "memini_streams_behind", 'reason="not_indexed"') == 0
    assert value(text, "memini_stream_lag_max_seconds") >= 3 * 3600
    assert value(text, "memini_stream_lag_schedules_max") >= 12
    assert value(text, "memini_jobs", 'kind="git_sync",state="queued"') == 1
    assert value(text, "memini_jobs_due", 'kind="git_sync"') == 1
    assert value(text, "memini_job_wait_max_seconds") >= 0

    # Labels carry templates, kinds and states: no name, key or id from the base.
    for private in (WORKSPACE, LAGGING, SOURCE, CHANNEL, THREAD, "refs/heads/synthetic"):
        assert private not in text
    for found in (workspace_id, source_id, stream_id):
        assert str(found) not in text
    assert METRICS_TOKEN not in text
    assert 'route="/v1/workspaces/{workspace}/sources/{source}/records"' in text


async def test_the_gauges_follow_an_index_that_is_behind(
    app: FastAPI, migrated_database_url: str, pool: Pool, admin: dict[str, str]
) -> None:
    await push_a_thread(app, admin)
    settings = Settings(
        database_url=SecretStr(migrated_database_url), metrics_token=SecretStr(METRICS_TOKEN)
    )
    changed = create_app(settings, pool=pool, embedder=FakeEmbedder(OTHER_MODEL))

    text = await scrape(changed)

    assert value(text, "memini_base_current") == 0
    assert value(text, "memini_workspaces", 'index="stale"') == 1
    assert value(text, "memini_workspaces_index_behind") == 1
    assert value(text, "memini_index_state", 'state="stale"') == 1
    assert value(text, "memini_streams_behind", 'reason="index_stale"') == 1
