"""Workspace, source, stream and run administration through the HTTP API, against a real Postgres.

Run with `make test-integration`. Every name, URL and repository is synthetic. Remote URLs are only
checked, never fetched; the one repository that is fetched is made by the test in a temporary
directory listed in git_local_roots.
"""

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, LiteralString

import httpx2
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from hearth_helpers import HEARTH_RECIPE_PATH, hearth_parser
from memini.api import create_app
from memini.auth import Scope, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.git import GitOptions, MirrorManager
from memini.jobs import enqueue_due_streams, source_lock_key
from memini.recipes import register_parser, unregister_parser
from memini.runs import record_run
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

SECRET = "fake-password-for-tests-only-91ac"  # noqa: S105 - fake, for leak checks
REMOTE = "https://git.example.invalid/team/site.git"
MCP_HOST = "hearth.example"


@pytest.fixture
def repos(tmp_path: Path) -> Path:
    path = tmp_path / "repos"
    (path / "app").mkdir(parents=True)
    return path


@pytest.fixture
def git_settings(migrated_database_url: str, tmp_path: Path, repos: Path) -> Settings:
    return Settings(
        database_url=SecretStr(migrated_database_url),
        git_mirror_dir=tmp_path / "mirrors",
        git_local_roots=(repos,),
        mcp_allowed_endpoints=(MCP_HOST,),
    )


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    """A connection to the test database in autocommit: each statement is committed."""
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
async def client(git_settings: Settings, pool: Pool) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_app(git_settings, pool=pool))
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
        yield client


async def bearer(
    db: Conn, owner: uuid.UUID, *scopes: Scope, workspace_id: uuid.UUID | None = None
) -> dict[str, str]:
    new = await create_token(
        db, owner_id=owner, name="test agent", scopes=scopes, workspace_id=workspace_id
    )
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


@pytest.fixture
async def admin(db: Conn, owner: uuid.UUID) -> dict[str, str]:
    return await bearer(db, owner, "read", "admin")


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    if status_code == 204:
        return {}
    body: Json = response.json()
    return body


def refused(response: httpx2.Response, status_code: int, code: str) -> Json:
    assert response.status_code == status_code, response.text
    error: Json = response.json()["error"]
    assert error["code"] == code
    return error


async def new_workspace(client: httpx2.AsyncClient, headers: dict[str, str], name: str) -> Json:
    return ok(await client.post("/v1/workspaces", json={"name": name}, headers=headers), 201)


async def new_source(
    client: httpx2.AsyncClient, headers: dict[str, str], workspace: str, **body: Any
) -> httpx2.Response:
    payload: Json = {"kind": "git", "name": "site", "location": REMOTE, "refs": ["main"], **body}
    return await client.post(f"/v1/workspaces/{workspace}/sources", json=payload, headers=headers)


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return list(await cur.fetchall())


# --- Workspaces and sources ----------------------------------------------------------------------


async def test_an_admin_creates_two_workspaces_and_a_git_source_in_each(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, admin: dict[str, str], repos: Path
) -> None:
    alpha = await new_workspace(client, admin, "alpha")
    beta = await new_workspace(client, admin, "beta")
    assert alpha["name"] == "alpha"
    assert {
        w["name"] for w in ok(await client.get("/v1/workspaces", headers=admin))["workspaces"]
    } == {
        "alpha",
        "beta",
    }

    link = repos / "app-link"
    link.symlink_to(repos / "app")
    local = ok(
        await new_source(
            client,
            admin,
            "alpha",
            name="app",
            location=str(link),
            refs=["main", "refs/tags/v1", "refs/heads/main"],
            schedule_seconds=3600,
            include=["src/**"],
            exclude=["**/*.min.js"],
        ),
        201,
    )
    assert local["workspace"] == "alpha"
    assert local["kind"] == "git"
    assert local["location"] == os.path.realpath(repos / "app")
    assert local["refs"] == ["refs/heads/main", "refs/tags/v1"]
    assert local["schedule_seconds"] == 3600
    assert local["include"] == ["src/**"]
    assert local["exclude"] == ["**/*.min.js"]
    assert (local["visibility"], local["retention_days"], local["llm_policy"]) == (
        "private",
        None,
        "local",
    )

    remote = ok(await new_source(client, admin, "beta", llm_policy="none"), 201)
    assert remote["location"] == REMOTE
    assert remote["refs"] == ["refs/heads/main"]
    assert remote["schedule_seconds"] is None
    assert remote["llm_policy"] == "none"

    listed = ok(await client.get("/v1/workspaces/alpha/sources", headers=admin))["sources"]
    assert [source["name"] for source in listed] == ["app"]
    assert ok(await client.get("/v1/workspaces/beta/sources/site", headers=admin)) == remote

    stored = await rows(
        db,
        "SELECT s.config, s.owner_id, s.workspace_id, st.key, st.schedule, st.next_run_at,"
        " st.owner_id FROM source s JOIN stream st ON st.source_id = s.id"
        " WHERE s.id = %s ORDER BY st.key",
        uuid.UUID(local["id"]),
    )
    assert [row[3] for row in stored] == ["refs/heads/main", "refs/tags/v1"]
    config, source_owner, workspace_id = stored[0][:3]
    assert config == {
        "location": os.path.realpath(repos / "app"),
        "include": ["src/**"],
        "exclude": ["**/*.min.js"],
    }
    assert source_owner == owner
    assert workspace_id == uuid.UUID(alpha["id"])
    assert all(row[4].total_seconds() == 3600 and row[5] is None for row in stored)
    assert all(row[6] == owner for row in stored)
    assert uuid.UUID(beta["id"]) != workspace_id

    streams = ok(await client.get("/v1/workspaces/alpha/sources/app/streams", headers=admin))
    assert [stream["key"] for stream in streams["streams"]] == ["refs/heads/main", "refs/tags/v1"]
    first = streams["streams"][0]
    assert first["schedule_seconds"] == 3600
    assert first["next_run_at"] is None
    assert first["checked_until"] is None
    assert first["lag_seconds"] is None
    assert first["current"] is False  # scheduled, never synced
    assert first["last_run"] is None
    assert first["pending_sync"] is None


async def test_names_are_unique_and_handles(
    client: httpx2.AsyncClient, admin: dict[str, str]
) -> None:
    await new_workspace(client, admin, "alpha")
    await new_workspace(client, admin, "beta")
    refused(
        await client.post("/v1/workspaces", json={"name": "alpha"}, headers=admin), 409, "conflict"
    )
    refused(
        await client.post("/v1/workspaces", json={"name": "Alpha!"}, headers=admin),
        422,
        "validation_error",
    )

    ok(await new_source(client, admin, "alpha"), 201)
    refused(await new_source(client, admin, "alpha"), 409, "conflict")
    ok(await new_source(client, admin, "beta"), 201)  # the same name in another workspace
    refused(await new_source(client, admin, "alpha", name="Bad Name"), 422, "validation_error")
    refused(await new_source(client, admin, "missing"), 404, "not_found")
    refused(
        await new_source(client, admin, "alpha", name="other", kind="push"), 422, "validation_error"
    )


async def test_a_read_token_reads_but_does_not_write(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, admin: dict[str, str]
) -> None:
    await new_workspace(client, admin, "alpha")
    ok(await new_source(client, admin, "alpha"), 201)
    reader = await bearer(db, owner, "read")

    for path in (
        "/v1/workspaces/alpha",
        "/v1/workspaces/alpha/sources",
        "/v1/workspaces/alpha/sources/site",
        "/v1/workspaces/alpha/sources/site/streams",
        "/v1/workspaces/alpha/runs",
    ):
        ok(await client.get(path, headers=reader))

    writes = [
        client.post("/v1/workspaces", json={"name": "gamma"}, headers=reader),
        new_source(client, reader, "alpha", name="other"),
        client.patch(
            "/v1/workspaces/alpha/sources/site", json={"schedule_seconds": 600}, headers=reader
        ),
        client.delete("/v1/workspaces/alpha/sources/site", headers=reader),
        client.post("/v1/workspaces/alpha/sources/site/sync", headers=reader),
    ]
    for write in writes:
        refused(await write, 403, "insufficient_scope")
    assert await scalar(db, "SELECT count(*) FROM workspace") == 1
    assert await scalar(db, "SELECT count(*) FROM source") == 1
    assert await scalar(db, "SELECT count(*) FROM stream WHERE schedule IS NOT NULL") == 0
    assert await scalar(db, "SELECT count(*) FROM job") == 0

    # Scopes do not imply each other: an admin-only token does not read.
    admin_only = await bearer(db, owner, "admin")
    refused(
        await client.get("/v1/workspaces/alpha/sources", headers=admin_only),
        403,
        "insufficient_scope",
    )


async def test_a_workspace_limited_admin_token_stays_in_its_workspace(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, admin: dict[str, str]
) -> None:
    alpha = await new_workspace(client, admin, "alpha")
    await new_workspace(client, admin, "beta")
    ok(await new_source(client, admin, "beta"), 201)
    limited = await bearer(db, owner, "read", "admin", workspace_id=uuid.UUID(alpha["id"]))

    refused(
        await client.post("/v1/workspaces", json={"name": "gamma"}, headers=limited),
        403,
        "workspace_limited",
    )
    for response in (
        await new_source(client, limited, "beta", name="intruder"),
        await client.patch(
            "/v1/workspaces/beta/sources/site", json={"schedule_seconds": 600}, headers=limited
        ),
        await client.delete("/v1/workspaces/beta/sources/site", headers=limited),
        await client.post("/v1/workspaces/beta/sources/site/sync", headers=limited),
        await client.get("/v1/workspaces/beta/sources", headers=limited),
        await client.get("/v1/workspaces/beta/sources/site/streams", headers=limited),
        await client.get("/v1/workspaces/beta/runs", headers=limited),
    ):
        error = refused(response, 404, "not_found")
        assert error["message"] == "No such workspace."  # beta is not even named as existing

    ok(await new_source(client, limited, "alpha"), 201)
    assert await scalar(db, "SELECT count(*) FROM workspace WHERE name = 'gamma'") == 0
    assert await scalar(db, "SELECT count(*) FROM source WHERE name = 'intruder'") == 0
    assert await scalar(db, "SELECT count(*) FROM stream WHERE schedule IS NOT NULL") == 0
    assert await scalar(db, "SELECT count(*) FROM job") == 0
    assert ok(await client.get("/v1/workspaces/beta/sources/site", headers=admin))["name"] == "site"


@pytest.mark.parametrize(
    "location",
    [
        "http://git.example.invalid/team/app.git",
        "ext::sh -c touch% /tmp/x",
        f"https://deploy:{SECRET}@git.example.invalid/team/app.git",
        f"https://{SECRET}@git.example.invalid/team/app.git",
        "git://git.example.invalid/team/app.git",
        "/nonexistent-root/repo",
        "file:///nonexistent-root/repo",
        "relative/repo",
        "ssh://git@git.example.invalid/team/app.git",
    ],
)
async def test_an_invalid_location_is_422_and_never_echoed(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str], location: str
) -> None:
    await new_workspace(client, admin, "alpha")
    response = await new_source(client, admin, "alpha", location=location)
    error = refused(response, 422, "invalid_source")
    assert error["message"].startswith("The source setting location is not valid: ")
    assert SECRET not in response.text
    assert location not in response.text
    assert await scalar(db, "SELECT count(*) FROM source") == 0


async def test_invalid_settings_are_refused_without_echoing_them(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str]
) -> None:
    await new_workspace(client, admin, "alpha")
    too_long = f"https://git.example.invalid/{SECRET}/" + "x" * 2100
    response = await new_source(client, admin, "alpha", location=too_long)
    error = refused(response, 422, "validation_error")
    # The body is one of the kinds of source, so the kind is part of the location.
    assert error["details"] == [
        {"location": ["body", "git", "location"], "type": "string_too_long"}
    ]
    assert SECRET not in response.text

    for body, setting in (
        ({"refs": ["main~1"]}, "refs"),
        ({"include": ["/etc/**"]}, "include"),
        ({"exclude": ["a/../b"]}, "exclude"),
    ):
        error = refused(await new_source(client, admin, "alpha", **body), 422, "invalid_source")
        assert error["message"].startswith(f"The source setting {setting} is not valid")
    refused(await new_source(client, admin, "alpha", refs=[]), 422, "validation_error")
    refused(await new_source(client, admin, "alpha", schedule_seconds=5), 422, "validation_error")
    assert await scalar(db, "SELECT count(*) FROM source") == 0


# --- Changing a source ---------------------------------------------------------------------------


async def test_a_new_schedule_starts_now(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str]
) -> None:
    await new_workspace(client, admin, "alpha")
    source = ok(
        await new_source(client, admin, "alpha", refs=["main", "dev"], schedule_seconds=3600), 201
    )
    path = "/v1/workspaces/alpha/sources/site"
    later = "UPDATE stream SET next_run_at = now() + interval '50 minutes' WHERE source_id = %s"
    pending = "SELECT count(*) FROM stream WHERE source_id = %s AND next_run_at IS NOT NULL"
    source_id = uuid.UUID(source["id"])

    await db.execute(later, (source_id,))
    same = ok(await client.patch(path, json={"schedule_seconds": 3600}, headers=admin))
    assert same["schedule_seconds"] == 3600
    assert await scalar(db, pending, source_id) == 2  # the same schedule keeps its turn

    changed = ok(await client.patch(path, json={"schedule_seconds": 600}, headers=admin))
    assert changed["schedule_seconds"] == 600
    assert await scalar(db, pending, source_id) == 0
    schedules = await rows(db, "SELECT schedule FROM stream WHERE source_id = %s", source_id)
    assert {row[0].total_seconds() for row in schedules} == {600}

    await db.execute(later, (source_id,))
    manual = ok(await client.patch(path, json={"schedule_seconds": None}, headers=admin))
    assert manual["schedule_seconds"] is None
    assert await scalar(db, pending, source_id) == 0
    assert (
        await scalar(
            db, "SELECT count(*) FROM stream WHERE source_id = %s AND schedule IS NULL", source_id
        )
        == 2
    )


async def test_a_patch_changes_refs_name_and_settings(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, admin: dict[str, str], pool: Pool
) -> None:
    await new_workspace(client, admin, "alpha")
    source = ok(
        await new_source(client, admin, "alpha", refs=["main", "dev"], schedule_seconds=900), 201
    )
    source_id = uuid.UUID(source["id"])
    path = "/v1/workspaces/alpha/sources/site"
    keys = "SELECT key, id FROM stream WHERE source_id = %s ORDER BY key"
    before = dict(await rows(db, keys, source_id))
    workspace_id = await scalar(db, "SELECT workspace_id FROM source WHERE id = %s", source_id)
    ok(await client.post(f"{path}/sync", headers=admin), 202)
    async with record_run(
        pool,
        workspace_id=workspace_id,
        owner_id=owner,
        kind="git_sync",
        source_id=source_id,
        stream_id=before["refs/heads/dev"],
    ):
        pass

    changed = ok(await client.patch(path, json={"refs": ["main", "release"]}, headers=admin))
    assert changed["refs"] == ["refs/heads/main", "refs/heads/release"]
    after = dict(await rows(db, keys, source_id))
    assert after["refs/heads/main"] == before["refs/heads/main"]  # kept, not recreated
    assert "refs/heads/dev" not in after
    assert await scalar(
        db, "SELECT schedule FROM stream WHERE id = %s", after["refs/heads/release"]
    ) == await scalar(db, "SELECT schedule FROM stream WHERE id = %s", after["refs/heads/main"])
    # The dev stream went with its run and its queued sync.
    assert await scalar(db, "SELECT count(*) FROM run WHERE source_id = %s", source_id) == 0
    assert await scalar(db, "SELECT count(*) FROM job WHERE source_id = %s", source_id) == 1

    settings = {
        "include": ["src/**"],
        "exclude": ["**/test/**"],
        "visibility": "workspace",
        "retention_days": 30,
        "llm_policy": "cloud",
    }
    changed = ok(await client.patch(path, json=settings, headers=admin))
    assert {key: changed[key] for key in settings} == settings
    assert changed["location"] == REMOTE
    assert await scalar(db, "SELECT config FROM source WHERE id = %s", source_id) == {
        "location": REMOTE,
        "include": ["src/**"],
        "exclude": ["**/test/**"],
    }
    assert changed["updated_at"] > source["updated_at"]

    renamed = ok(await client.patch(path, json={"name": "site2"}, headers=admin))
    assert renamed["id"] == source["id"]
    refused(await client.get(path, headers=admin), 404, "not_found")
    ok(await client.get("/v1/workspaces/alpha/sources/site2", headers=admin))
    ok(await new_source(client, admin, "alpha", name="other"), 201)
    refused(
        await client.patch(
            "/v1/workspaces/alpha/sources/other", json={"name": "site2"}, headers=admin
        ),
        409,
        "conflict",
    )

    path = "/v1/workspaces/alpha/sources/site2"
    error = refused(
        await client.patch(path, json={"location": f"https://{SECRET}@x.invalid/a"}, headers=admin),
        422,
        "validation_error",
    )
    assert error["details"] == [{"location": ["body", "location"], "type": "extra_forbidden"}]
    assert SECRET not in str(error)
    invalid: list[Json] = [{"name": None}, {"refs": []}, {"refs": None}, {"visibility": "public"}]
    for body in invalid:
        refused(await client.patch(path, json=body, headers=admin), 422, "validation_error")
    refused(
        await client.patch(path, json={"include": ["../up"]}, headers=admin), 422, "invalid_source"
    )
    refused(
        await client.patch("/v1/workspaces/alpha/sources/missing", json={}, headers=admin),
        404,
        "not_found",
    )
    assert await scalar(db, "SELECT name FROM source WHERE id = %s", source_id) == "site2"


async def test_the_builtin_excludes_are_switched_off_and_on_per_source(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str]
) -> None:
    await new_workspace(client, admin, "alpha")
    config = "SELECT config FROM source WHERE name = %s"
    kept = ok(await new_source(client, admin, "alpha", name="kept"), 201)
    assert kept["builtin_excludes"] is True
    assert "builtin_excludes" not in await scalar(db, config, "kept")  # the default is not stored

    source = ok(await new_source(client, admin, "alpha", builtin_excludes=False), 201)
    assert source["builtin_excludes"] is False
    assert (await scalar(db, config, "site"))["builtin_excludes"] is False
    path = "/v1/workspaces/alpha/sources/site"

    # Another rule changes; the switch stays as it was.
    changed = ok(await client.patch(path, json={"include": ["src/**"]}, headers=admin))
    assert (changed["include"], changed["builtin_excludes"]) == (["src/**"], False)
    assert (await scalar(db, config, "site"))["builtin_excludes"] is False

    changed = ok(await client.patch(path, json={"builtin_excludes": True}, headers=admin))
    assert (changed["include"], changed["builtin_excludes"]) == (["src/**"], True)
    assert await scalar(db, config, "site") == {
        "location": REMOTE,
        "include": ["src/**"],
        "exclude": [],
    }
    for body in ({"builtin_excludes": None}, {"builtin_excludes": "no"}, {"builtin_excludes": 0}):
        refused(await client.patch(path, json=body, headers=admin), 422, "validation_error")
    refused(
        await new_source(client, admin, "alpha", name="third", builtin_excludes="false"),
        422,
        "validation_error",
    )


async def test_a_push_source_is_not_synced_and_has_no_refs(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, admin: dict[str, str]
) -> None:
    alpha = await new_workspace(client, admin, "alpha")
    await db.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, 'inbox', 'push')",
        (uuid.UUID(alpha["id"]), owner),
    )
    path = "/v1/workspaces/alpha/sources/inbox"
    shown = ok(await client.get(path, headers=admin))
    assert (shown["kind"], shown["location"], shown["refs"]) == ("push", None, [])
    assert shown["builtin_excludes"] is None
    refused(await client.post(f"{path}/sync", headers=admin), 409, "not_syncable")
    refused(await client.patch(path, json={"refs": ["main"]}, headers=admin), 422, "invalid_source")
    refused(
        await client.patch(path, json={"builtin_excludes": False}, headers=admin),
        422,
        "invalid_source",
    )
    assert (
        ok(await client.patch(path, json={"retention_days": 7}, headers=admin))["retention_days"]
        == 7
    )
    ok(await client.delete(path, headers=admin), 204)


# --- Syncs and runs ------------------------------------------------------------------------------


async def test_a_sync_enqueues_one_job_per_stream_and_merges_with_the_next(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str]
) -> None:
    await new_workspace(client, admin, "alpha")
    source = ok(
        await new_source(client, admin, "alpha", refs=["main", "dev"], schedule_seconds=600), 201
    )
    source_id = uuid.UUID(source["id"])
    path = "/v1/workspaces/alpha/sources/site"

    first = ok(await client.post(f"{path}/sync", headers=admin), 202)["jobs"]
    assert [job["stream"] for job in first] == ["refs/heads/dev", "refs/heads/main"]
    assert all(job["created"] for job in first)
    stored = await rows(
        db,
        "SELECT j.id, j.kind, j.status, j.dedupe_key, st.key, j.stream_id::text"
        " FROM job j JOIN stream st ON st.id = j.stream_id WHERE j.source_id = %s ORDER BY st.key",
        source_id,
    )
    assert [str(row[0]) for row in stored] == [job["job_id"] for job in first]
    assert all(row[1] == "git_sync" and row[2] == "queued" for row in stored)
    assert all(row[3] == row[5] for row in stored)  # the stream's dedupe key

    again = ok(await client.post(f"{path}/sync", headers=admin), 202)["jobs"]
    assert [job["job_id"] for job in again] == [job["job_id"] for job in first]
    assert not any(job["created"] for job in again)
    one = ok(
        await client.post(f"{path}/sync", json={"streams": ["refs/heads/main"]}, headers=admin),
        202,
    )["jobs"]
    assert [(job["stream"], job["job_id"], job["created"]) for job in one] == [
        ("refs/heads/main", first[1]["job_id"], False)
    ]
    refused(
        await client.post(f"{path}/sync", json={"streams": ["refs/heads/nope"]}, headers=admin),
        404,
        "not_found",
    )

    # The scheduler finds both streams due (next_run_at is NULL) and merges with the manual syncs.
    fired = await enqueue_due_streams(db)
    assert (fired.streams, fired.jobs) == (2, 0)
    assert await scalar(db, "SELECT count(*) FROM job WHERE source_id = %s", source_id) == 2

    streams = ok(await client.get(f"{path}/streams", headers=admin))["streams"]
    assert [stream["pending_sync"]["id"] for stream in streams] == [job["job_id"] for job in first]
    assert all(stream["pending_sync"]["status"] == "queued" for stream in streams)
    assert all(stream["next_run_at"] is not None for stream in streams)


async def test_runs_are_listed_newest_first_and_read_with_their_report(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, admin: dict[str, str], pool: Pool
) -> None:
    alpha = await new_workspace(client, admin, "alpha")
    beta = await new_workspace(client, admin, "beta")
    app = ok(await new_source(client, admin, "alpha", name="app", refs=["main", "dev"]), 201)
    site = ok(await new_source(client, admin, "beta"), 201)
    alpha_id, beta_id = uuid.UUID(alpha["id"]), uuid.UUID(beta["id"])
    app_id = uuid.UUID(app["id"])
    streams = dict(await rows(db, "SELECT key, id FROM stream WHERE source_id = %s", app_id))

    async def run(workspace: uuid.UUID, kind: str, **scope: Any) -> uuid.UUID:
        async with (
            record_run(pool, workspace_id=workspace, owner_id=owner, kind=kind, **scope) as r,
            r.step("fetch"),
        ):
            r.count("files_indexed", 3)
        return r.id

    main_run = await run(
        alpha_id, "git_sync", source_id=app_id, stream_id=streams["refs/heads/main"]
    )
    dev_run = await run(alpha_id, "git_sync", source_id=app_id, stream_id=streams["refs/heads/dev"])
    reindex_run = await run(alpha_id, "reindex")
    beta_run = await run(beta_id, "git_sync", source_id=uuid.UUID(site["id"]))

    listed = ok(await client.get("/v1/workspaces/alpha/runs", headers=admin))
    assert [r["id"] for r in listed["runs"]] == [str(reindex_run), str(dev_run), str(main_run)]
    assert listed["next_before"] is None
    newest = listed["runs"][1]
    assert (newest["kind"], newest["status"], newest["source"], newest["stream"]) == (
        "git_sync",
        "succeeded",
        "app",
        "refs/heads/dev",
    )
    assert listed["runs"][0]["source"] is None

    of_source = ok(await client.get("/v1/workspaces/alpha/runs?source=app", headers=admin))
    assert [r["id"] for r in of_source["runs"]] == [str(dev_run), str(main_run)]
    of_stream = ok(
        await client.get(
            "/v1/workspaces/alpha/runs",
            params={"source": "app", "stream": "refs/heads/main"},
            headers=admin,
        )
    )
    assert [r["id"] for r in of_stream["runs"]] == [str(main_run)]
    refused(
        await client.get("/v1/workspaces/alpha/runs?stream=refs/heads/main", headers=admin),
        422,
        "invalid_filter",
    )
    refused(
        await client.get("/v1/workspaces/alpha/runs?source=nope", headers=admin), 404, "not_found"
    )
    refused(
        await client.get(
            "/v1/workspaces/alpha/runs",
            params={"source": "app", "stream": "refs/heads/nope"},
            headers=admin,
        ),
        404,
        "not_found",
    )

    pages: list[str] = []
    params: dict[str, Any] = {"limit": 2}
    while True:
        page = ok(await client.get("/v1/workspaces/alpha/runs", params=params, headers=admin))
        pages.append(",".join(r["id"] for r in page["runs"]))
        if page["next_before"] is None:
            break
        params["before"] = page["next_before"]
    assert pages == [f"{reindex_run},{dev_run}", str(main_run)]
    refused(
        await client.get("/v1/workspaces/alpha/runs?before=not-a-cursor", headers=admin),
        422,
        "invalid_cursor",
    )
    refused(
        await client.get("/v1/workspaces/alpha/runs?limit=0", headers=admin),
        422,
        "validation_error",
    )

    detail = ok(await client.get(f"/v1/runs/{main_run}", headers=admin))
    assert (detail["workspace"], detail["source"], detail["stream"]) == (
        "alpha",
        "app",
        "refs/heads/main",
    )
    assert detail["report"]["version"] == 1
    assert detail["report"]["counts"] == {"files_indexed": 3}
    assert [step["name"] for step in detail["report"]["steps"]] == ["fetch"]
    assert detail["finished_at"] is not None

    shown = ok(await client.get("/v1/workspaces/alpha/sources/app/streams", headers=admin))
    last_runs = {s["key"]: s["last_run"]["id"] for s in shown["streams"]}
    assert last_runs == {"refs/heads/dev": str(dev_run), "refs/heads/main": str(main_run)}

    limited = await bearer(db, owner, "read", workspace_id=beta_id)
    refused(await client.get(f"/v1/runs/{main_run}", headers=limited), 404, "not_found")
    ok(await client.get(f"/v1/runs/{beta_run}", headers=limited))
    refused(await client.get(f"/v1/runs/{uuid.uuid4()}", headers=admin), 404, "not_found")


# --- Deleting a source ---------------------------------------------------------------------------


def make_repository(path: Path, files: dict[str, str] | None = None) -> None:
    """A repository with one commit on main of ``files`` (a README by default); synthetic content
    and identity."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T12:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T12:00:00+00:00",
    }
    for name, text in (files or {"README.md": "# Fixture\n"}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    for args in (
        ["init", "--quiet", "--initial-branch=main"],
        ["add", "--all"],
        ["commit", "--quiet", "-m", "first"],
    ):
        subprocess.run(["git", *args], cwd=path, env=env, check=True, capture_output=True)  # noqa: S603, S607


@pytest.mark.git
@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable")
async def test_deleting_a_source_removes_its_streams_jobs_runs_and_mirror(
    client: httpx2.AsyncClient,
    db: Conn,
    owner: uuid.UUID,
    admin: dict[str, str],
    pool: Pool,
    repos: Path,
    git_settings: Settings,
) -> None:
    make_repository(repos / "app")
    await new_workspace(client, admin, "alpha")
    kept = ok(await new_source(client, admin, "alpha", name="kept"), 201)
    source = ok(
        await new_source(client, admin, "alpha", name="app", location=str(repos / "app")), 201
    )
    source_id = uuid.UUID(source["id"])
    workspace_id = await scalar(db, "SELECT workspace_id FROM source WHERE id = %s", source_id)
    stream_id = await scalar(db, "SELECT id FROM stream WHERE source_id = %s", source_id)

    mirrors = MirrorManager(GitOptions.from_settings(git_settings))
    synced = await mirrors.sync(source_id, source["location"])
    assert synced.created
    assert mirrors.find(source_id) is not None
    ok(await client.post("/v1/workspaces/alpha/sources/app/sync", headers=admin), 202)
    ok(await client.post("/v1/workspaces/alpha/sources/kept/sync", headers=admin), 202)
    async with record_run(
        pool,
        workspace_id=workspace_id,
        owner_id=owner,
        kind="git_sync",
        source_id=source_id,
        stream_id=stream_id,
    ):
        pass

    ok(await client.delete("/v1/workspaces/alpha/sources/app", headers=admin), 204)

    remaining: tuple[LiteralString, ...] = (
        "SELECT count(*) FROM source WHERE id = %s",
        "SELECT count(*) FROM stream WHERE source_id = %s",
        "SELECT count(*) FROM job WHERE source_id = %s",
        "SELECT count(*) FROM run WHERE source_id = %s",
    )
    for query in remaining:
        assert await scalar(db, query, source_id) == 0, query
    assert not mirrors.path(source_id).exists()
    assert mirrors.find(source_id) is None
    refused(await client.get("/v1/workspaces/alpha/sources/app", headers=admin), 404, "not_found")
    refused(
        await client.delete("/v1/workspaces/alpha/sources/app", headers=admin), 404, "not_found"
    )
    # The other source of the workspace is untouched.
    assert (
        await scalar(db, "SELECT count(*) FROM job WHERE source_id = %s", uuid.UUID(kept["id"]))
        == 1
    )
    assert (repos / "app" / "README.md").read_text() == "# Fixture\n"


async def test_a_source_is_not_deleted_while_a_job_of_it_runs(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str]
) -> None:
    await new_workspace(client, admin, "alpha")
    source = ok(await new_source(client, admin, "alpha"), 201)
    source_id = uuid.UUID(source["id"])
    path = "/v1/workspaces/alpha/sources/site"

    # What a worker holds while it runs a job of the source.
    await db.execute("SELECT pg_advisory_lock(%s, %s)", source_lock_key(source_id))
    try:
        refused(await client.delete(path, headers=admin), 409, "source_busy")
        assert await scalar(db, "SELECT count(*) FROM source WHERE id = %s", source_id) == 1
        assert await scalar(db, "SELECT count(*) FROM stream WHERE source_id = %s", source_id) == 1
    finally:
        await db.execute("SELECT pg_advisory_unlock(%s, %s)", source_lock_key(source_id))

    ok(await client.delete(path, headers=admin), 204)
    assert await scalar(db, "SELECT count(*) FROM source WHERE id = %s", source_id) == 0


# --- mcp sources ---------------------------------------------------------------------------------


@pytest.fixture
def hearth_parser_registered() -> Iterator[None]:
    register_parser("hearth.thread", hearth_parser(), replace=True)
    yield
    unregister_parser("hearth.thread")


def mcp_body(**changes: Any) -> Json:
    body: Json = {
        "kind": "mcp",
        "name": "chat",
        "endpoint": {"url": f"https://{MCP_HOST}/mcp"},
        "recipe": {"toml": HEARTH_RECIPE_PATH.read_text(encoding="utf-8")},
        "streams": ["C0042"],
        "schedule_seconds": 900,
        "window": {"initial": "3d"},
        "filters": {"exclude_authors": ["*bot*"]},
    }
    return body | changes


async def test_an_mcp_source_is_added_shown_changed_and_synced_over_http(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str], hearth_parser_registered: None
) -> None:
    await new_workspace(client, admin, "alpha")
    path = "/v1/workspaces/alpha/sources"
    created = ok(await client.post(path, json=mcp_body(), headers=admin), 201)

    assert (created["kind"], created["refs"], created["schedule_seconds"]) == (
        "mcp",
        ["C0042"],
        900,
    )
    assert created["mcp"]["endpoint"] == {
        "url": f"https://{MCP_HOST}/mcp",
        "command": None,
        "args": [],
        "credential_env": None,
    }
    recipe = created["mcp"]["recipe"]
    assert (recipe["name"], recipe["bundled"], len(recipe["digest"])) == ("hearth", False, 64)
    assert created["mcp"]["window"] == {"initial": "3d"}
    assert created["filters"]["exclude_authors"] == ["*bot*"]
    assert "toml" not in json.dumps(created)  # the recipe's text is not sent back
    assert ok(await client.get(f"{path}/chat", headers=admin)) == created

    changed = ok(
        await client.patch(
            f"{path}/chat",
            json={"streams": ["C0042", "C0043"], "window": {}, "filters": {}},
            headers=admin,
        )
    )
    assert changed["refs"] == ["C0042", "C0043"]
    assert changed["mcp"]["window"] == {}
    assert changed["filters"]["exclude_authors"] == []
    assert changed["mcp"]["recipe"] == recipe

    accepted = ok(await client.post(f"{path}/chat/sync", headers=admin), 202)
    assert [job["stream"] for job in accepted["jobs"]] == ["C0042", "C0043"]
    kinds = await db.execute("SELECT DISTINCT kind FROM job")
    assert [row[0] for row in await kinds.fetchall()] == ["mcp_sync"]


async def test_an_mcp_source_is_refused_without_echoing_what_was_sent(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str], hearth_parser_registered: None
) -> None:
    await new_workspace(client, admin, "alpha")
    path = "/v1/workspaces/alpha/sources"
    for body, setting in (
        ({"endpoint": {"url": f"https://{SECRET}.example/mcp"}}, "endpoint.url"),
        ({"endpoint": {"url": f"http://{MCP_HOST}/mcp"}}, "endpoint.url"),
        ({"endpoint": {"url": f"https://user:{SECRET}@{MCP_HOST}/mcp"}}, "endpoint.url"),
        ({"endpoint": {"command": "/bin/sh", "args": [SECRET]}}, "endpoint.command"),
        ({"recipe": {"toml": f"format = 1\nname = '{SECRET}'\n"}}, "recipe"),
        ({"recipe": {"bundled": "hearth"}}, "recipe"),
        ({"streams": [SECRET]}, "streams"),
        ({"window": {"initial": SECRET}}, "window.initial"),
    ):
        response = await client.post(path, json=mcp_body(**body), headers=admin)
        error = refused(response, 422, "invalid_source")
        assert error["message"].startswith(f"The source setting {setting} is not valid"), body
        assert SECRET not in response.text
    malformed: list[Json] = [
        {"endpoint": {"url": f"https://{MCP_HOST}/mcp", "command": "/bin/true"}},
        {"recipe": {}},
        {"streams": []},
    ]
    for change in malformed:
        refused(
            await client.post(path, json=mcp_body(**change), headers=admin),
            422,
            "validation_error",
        )
    assert await scalar(db, "SELECT count(*) FROM source") == 0


# --- Previewing what a source indexes ------------------------------------------------------------


@pytest.mark.git
@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable")
async def test_a_preview_counts_what_the_rules_keep_by_extension_and_folder(
    client: httpx2.AsyncClient, admin: dict[str, str], repos: Path, git_settings: Settings
) -> None:
    make_repository(
        repos / "app",
        {
            "README.md": "# Harbour\n",
            "berths/berth.go": "package berths\n",
            "berths/berth_test.go": "package berths\n",
            "web/plan.ts": "export const plan = 1;\n",
            "web/plan.test.ts": "test();\n",
            "fixtures/ships.json": "{}\n",
            "web/package-lock.json": "{}\n",
        },
    )
    await new_workspace(client, admin, "alpha")
    source = ok(
        await new_source(
            client, admin, "alpha", name="app", location=str(repos / "app"), exclude=["**/*.json"]
        ),
        201,
    )
    url = "/v1/workspaces/alpha/sources/app/preview"
    refused(await client.post(url, headers=admin), 409, "not_fetched")
    mirrors = MirrorManager(GitOptions.from_settings(git_settings))
    await mirrors.sync(uuid.UUID(source["id"]), source["location"])

    own = ok(await client.post(url, headers=admin))

    assert own["ref"] == "refs/heads/main"
    assert (own["tracked"], own["indexed"]) == (7, 5)
    assert own["skipped"] == {"excluded": 2}
    by_extension = {g["key"]: (g["tracked"], g["indexed"]) for g in own["by_extension"]}
    assert by_extension == {".go": (2, 2), ".ts": (2, 2), ".json": (2, 0), ".md": (1, 1)}
    assert own["by_folder"][0] == {"key": "web", "tracked": 3, "indexed": 2}

    tried = ok(
        await client.post(
            url,
            json={"exclude": ["**/*_test.go", "**/*.test.ts"], "builtin_excludes": True},
            headers=admin,
        )
    )

    # The rules tried replace the source's exclude: the JSON fixture is back, the lock file stays
    # out by the built-in exclusions, and the tests are left out. Nothing is saved.
    assert (tried["tracked"], tried["indexed"]) == (7, 4)
    by_folder = {group["key"]: group["indexed"] for group in tried["by_folder"]}
    assert by_folder == {"web": 1, "berths": 1, "fixtures": 1, "(root)": 1}
    saved = ok(await client.get("/v1/workspaces/alpha/sources/app", headers=admin))
    assert saved["exclude"] == ["**/*.json"]

    missing = {"ref": "refs/heads/nowhere"}
    refused(await client.post(url, json=missing, headers=admin), 404, "not_found")
    option = {"ref": "--upload-pack=x"}
    assert (await client.post(url, json=option, headers=admin)).status_code == 422
    absolute = {"exclude": ["/srv/**"]}
    refused(await client.post(url, json=absolute, headers=admin), 422, "invalid_source")
