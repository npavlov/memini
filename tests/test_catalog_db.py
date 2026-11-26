"""The catalog through the MCP endpoint and the HTTP API, against a real Postgres and real git
mirrors: put_project, get_catalog, get_project, get_domain, catalog_guide, the projects, domains
and services filters of search, and the context of code results.

Acceptance of memini-qpk.8, on the same fixture with two projects (Links between services,
below): a declared link from a service of one project to a service of the other is returned by
get_project of both projects, in the right direction; a link to a service of another workspace is
refused; a link to a service that does not exist is reported by put_project; sending the document
twice changes nothing.

Acceptance of memini-qpk.1, on a synthetic fixture of two repositories (and a third no service is
in): the same document twice changes nothing the second time; a mask that matches nothing is
reported; a search filtered by a subdomain returns only files under its masks, across both
repositories; a hit of a file in two subdomains lists both; get_catalog lists a repository
without a service; a token limited to one workspace sees no other workspace's catalog; changing a
mask changes results without a reindex; HTTP and MCP return the same shapes. Postgres and Python
are asked about the same masks and paths and must agree.

Repositories are made here and synced as the git_sync job syncs them; every file, name and text
is synthetic. Vectors come from the FakeEmbedder, whose vectors carry no meaning, so searches
here find chunks through their words.
"""

import json
import shutil
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent
from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow
from pydantic import SecretStr

from git_helpers import SourceRepo
from memini.api import create_app
from memini.auth import NewToken, Scope, WriteAccess, create_token
from memini.catalog import CATALOG_GUIDE
from memini.cli.export import export_workspace
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, run_git_sync
from memini.jobs import Job
from memini.mcp import MCP_PATH
from memini.mcp.text import TEXT_NOTICE
from memini.provenance import DATA_NOTICE
from memini.search.patterns import directory_regex, glob_regex, globs_regex, path_matcher
from memini.sources import (
    NewGitSource,
    NewPushSource,
    Source,
    create_git_source,
    create_push_source,
    delete_source,
)
from memini.workspaces import create_workspace, ensure_principal
from redaction_helpers import STRIPE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.git,
    pytest.mark.anyio,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

BASE_URL = "http://memini.test"
MAIN = "refs/heads/main"

# --- The fixture: a fee engine, the booking pages, and a tool no service is in --------------------

QUAY: dict[str, str] = {
    "README.md": "# Quay\n\nBerth and pilot fees of a synthetic harbour.\n",
    "quay/fees/berth.py": (
        "def berth_fee(length, hours):\n"
        '    """What a berth costs: the harbour rate by length and hours."""\n'
        '    return harbour_rate("berth") * length * hours\n'
    ),
    "quay/fees/pilot.py": (
        "def pilot_fee(draught):\n"
        '    """What a pilot costs in the harbour, by draught."""\n'
        '    return harbour_rate("pilot") * draught\n'
    ),
    "quay/fees/rates.py": (
        "def harbour_rate(kind):\n"
        '    """The harbour rate of a kind of fee: berth or pilot."""\n'
        '    return {"berth": 3, "pilot": 40}[kind]\n'
    ),
    "quay/tides.py": (
        "def high_water(day):\n"
        '    """The minute of high water in the harbour on a day."""\n'
        "    return (day * 50) % 1440\n"
    ),
}
LANTERN: dict[str, str] = {
    "web/booking/berth.ts": (
        "export function berthQuote(length: number, hours: number): number {\n"
        "  // The harbour quote of a berth, as the booking page shows it.\n"
        "  return 3 * length * hours;\n"
        "}\n"
    ),
    "web/booking/form.ts": (
        "export function bookingForm(): string {\n"
        "  // The harbour booking form.\n"
        '  return "form";\n'
        "}\n"
    ),
    "web/admin/users.ts": (
        "export function listUsers(): string[] {\n  // The harbour staff.\n  return [];\n}\n"
    ),
}
SPARE: dict[str, str] = {
    "tool.py": 'def sound():\n    """The harbour horn."""\n    return "hoot"\n',
}
VAULT: dict[str, str] = {
    "vault.py": 'def unlock(code):\n    """The harbour vault of the other workspace."""\n'
    "    return code == 1234\n",
}

PORT: Json = {
    "title": "Port operations",
    "description": "Fees and bookings of the synthetic harbour.",
    "services": [
        {"name": "quay", "source": "quay", "description": "The fee engine."},
        {
            "name": "booking-web",
            "source": "lantern",
            "root": "web/booking",
            "description": "The booking pages.",
        },
    ],
    "domains": [
        {
            "name": "fees",
            "title": "Fees",
            "description": "What a call at the harbour costs.",
            "subdomains": [
                {
                    "name": "berth",
                    "title": "Berth fees",
                    "code": [
                        {"service": "quay", "paths": ["quay/fees/berth.py", "quay/fees/rates.py"]},
                        {"service": "booking-web", "paths": ["berth.ts"]},
                    ],
                },
                {
                    "name": "pilot",
                    "title": "Pilot fees",
                    "code": [
                        {
                            "service": "quay",
                            "paths": [
                                "quay/fees/pilot.py",
                                "quay/fees/rates.py",
                                "quay/fees/towage.py",
                            ],
                        }
                    ],
                },
            ],
        },
        # Code attached to a domain itself, and a domain that has no code yet.
        {"name": "water", "code": [{"service": "quay", "paths": ["quay/tides.py"]}]},
        {"name": "plans", "title": "Plans"},
    ],
}
"""The project of the harbour workspace. ``quay/fees/rates.py`` is in both subdomains of fees,
``quay/fees/towage.py`` does not exist, and ``web/booking/form.ts``, the README and everything
outside the two services are in no domain."""

SAFE: Json = {
    "title": "Safe",
    "services": [{"name": "vault", "source": "vault"}],
    "domains": [{"name": "locks", "code": [{"service": "vault", "paths": ["**"]}]}],
}
"""The project of the other workspace."""

BERTH = {
    ("quay", "quay/fees/berth.py"),
    ("quay", "quay/fees/rates.py"),
    ("lantern", "web/booking/berth.ts"),
}
PILOT = {("quay", "quay/fees/pilot.py"), ("quay", "quay/fees/rates.py")}
QUAY_CODE = {("quay", path) for path in QUAY if path.endswith(".py")}
BOOKING = {("lantern", "web/booking/berth.ts"), ("lantern", "web/booking/form.ts")}


def changed(document: Json, **fields: Any) -> Json:
    """A deep copy of ``document`` with ``fields`` replaced."""
    copy: Json = json.loads(json.dumps(document))
    copy.update(fields)
    return copy


def with_pilot_paths(paths: list[str]) -> Json:
    """PORT with other masks for the pilot fees."""
    document = changed(PORT)
    document["domains"][0]["subdomains"][1]["code"][0]["paths"] = paths
    return document


@dataclass
class World:
    pool: Pool
    db: Conn
    settings: Settings
    owner: uuid.UUID
    harbor: uuid.UUID
    other: uuid.UUID
    embedder: FakeEmbedder
    pins: dict[str, str]
    """The commit each repository is pinned at, by its name."""

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)

    def app(self) -> FastAPI:
        return create_app(self.settings, pool=self.pool, embedder=self.embedder)

    async def new_token(self, *scopes: Scope, workspace: uuid.UUID | None = None) -> NewToken:
        return await create_token(
            self.db,
            owner_id=self.owner,
            name="test agent",
            scopes=scopes or ("read",),
            workspace_id=workspace,
        )

    async def token(self, *scopes: Scope, workspace: uuid.UUID | None = None) -> str:
        return (await self.new_token(*scopes, workspace=workspace)).secret.get_secret_value()


async def sync(pool: Pool, settings: Settings, embedder: FakeEmbedder, source: Source) -> None:
    """Run the git_sync job of every stream of ``source``, as the worker runs it."""
    job = Job(
        id=uuid.uuid4(),
        workspace_id=source.workspace_id,
        owner_id=source.owner_id,
        source_id=source.id,
        stream_id=None,
        kind=GIT_SYNC,
        args={},
        attempt=1,
        max_attempts=1,
        worker="test",
    )
    await run_git_sync(pool, MirrorManager(GitOptions.from_settings(settings)), embedder, job)


@pytest.fixture
async def world(migrated_database_url: str, tmp_path: Path) -> AsyncIterator[World]:
    """harbor: the repositories quay, lantern and spare, synced. other: the repository vault."""
    repos = tmp_path / "repos"
    settings = Settings(
        database_url=SecretStr(migrated_database_url),
        git_mirror_dir=tmp_path / "mirrors",
        git_local_roots=(repos,),
    )
    embedder = FakeEmbedder()
    placed = (("harbor", "quay", QUAY), ("harbor", "lantern", LANTERN), ("harbor", "spare", SPARE))
    async with (
        open_pool(migrated_database_url, min_size=1, max_size=8, timeout=5) as pool,
        connect(migrated_database_url, autocommit=True) as db,
    ):
        owner = await ensure_principal(db, "owner")
        harbor = await create_workspace(db, owner_id=owner, name="harbor")
        other = await create_workspace(db, owner_id=owner, name="other")
        pins: dict[str, str] = {}
        for workspace, name, files in (*placed, ("other", "vault", VAULT)):
            repo = SourceRepo(repos / name)
            pins[name] = repo.commit(files, "first")
            source = await create_git_source(
                db,
                WriteAccess.for_owner(owner),
                workspace,
                NewGitSource(name=name, location=str(repo.path), refs=["main"]),
                options=GitOptions.from_settings(settings),
            )
            await sync(pool, settings, embedder, source)
        yield World(pool, db, settings, owner, harbor.id, other.id, embedder, pins)


@asynccontextmanager
async def http_client(app: FastAPI, token: str | None) -> AsyncGenerator[httpx2.AsyncClient]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE_URL, headers=headers
        ) as client,
    ):
        yield client


@asynccontextmanager
async def mcp_client(app: FastAPI, token: str) -> AsyncGenerator[Client]:
    async with http_client(app, token) as http:
        transport = streamable_http_client(f"{BASE_URL}{MCP_PATH}", http_client=http)
        async with Client(transport, mode="legacy") as client:
            yield client


def when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def text(result: CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, TextContent)
    return content.text


def ok(result: CallToolResult) -> Json:
    assert result.is_error is False, text(result)
    structured: Json = result.structured_content
    return structured


def refused(result: CallToolResult) -> str:
    assert result.is_error is True
    assert result.structured_content is None
    return text(result)


def body(response: httpx2.Response, status: int = 200) -> Json:
    assert response.status_code == status, response.text
    found: Json = response.json()
    return found


def error(response: httpx2.Response, status: int, code: str) -> str:
    assert response.status_code == status, response.text
    found: Json = response.json()["error"]
    assert found["code"] == code
    return str(found["message"])


async def put(
    client: Client,
    document: Json = PORT,
    *,
    project: str = "port",
    workspace: str = "harbor",
    **more: Any,
) -> CallToolResult:
    arguments = {"workspace": workspace, "project": project, **document, **more}
    return await client.call_tool("put_project", arguments)


def project_url(project: str = "port", workspace: str = "harbor") -> str:
    return f"/v1/workspaces/{workspace}/projects/{project}"


async def found_in(client: Client, tool: str = "search_code", **arguments: Any) -> Json:
    """Every chunk of the fixture holds the word harbour: an exact search lists what a filter
    lets through."""
    query = {"query": "harbour", "limit": 50, "workspaces": ["harbor"], **arguments}
    if tool == "search_code":
        query = {"mode": "exact", **query}
    return ok(await client.call_tool(tool, query))


def places(result: Json) -> set[tuple[str, str]]:
    return {(hit["source"], hit["path"]) for hit in result["items"]}


def hit_of(result: Json, path: str) -> Json:
    return next(hit for hit in result["items"] if hit["path"] == path)


async def digests(db: Conn, *tables: str) -> dict[str, tuple[int, str]]:
    """The number of rows of each table and a digest of every column of every row."""
    found: dict[str, tuple[int, str]] = {}
    for table in tables:
        query = sql.SQL(
            "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), ''))"
            " FROM {} t"
        ).format(sql.Identifier(table))
        row = await (await db.execute(query)).fetchone()
        assert row is not None
        found[table] = (row[0], row[1])
    return found


CATALOG_TABLES = ("project", "service", "domain", "domain_code", "service_link")
INDEX_TABLES = ("record", "chunk", "symbol", "index_state")


# --- put_project ---------------------------------------------------------------------------------


async def test_the_same_document_twice_changes_nothing_the_second_time(world: World) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        first = await put(client)
        stored = await digests(world.db, *CATALOG_TABLES)
        second = await put(client)
        after = await digests(world.db, *CATALOG_TABLES)

    made, again = ok(first), ok(second)
    assert (made["created"], made["changed"], made["version"]) == (True, True, 1)
    assert (again["created"], again["changed"], again["version"]) == (False, False, 1)
    # No row was written: not a time, not a version, not who changed it.
    assert after == stored
    assert stored["project"][0] == 1
    assert stored["service"][0] == 2
    assert stored["domain"][0] == 5
    assert stored["domain_code"][0] == 4
    # The report is computed again, and says the same.
    for part in ("masks", "empty_masks", "overlaps", "services", "redacted"):
        assert again[part] == made[part], part
    assert made["data_notice"] == DATA_NOTICE
    assert text(first).startswith("Project port in workspace harbor: created; version 1.")
    assert text(second).startswith("Project port in workspace harbor: unchanged")


async def test_the_answer_reports_what_the_masks_match(world: World) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        result = await put(client)

    report = ok(result)
    assert [(m["domain"], m["service"], m["path"], m["files"]) for m in report["masks"]] == [
        ("fees/berth", "booking-web", "berth.ts", 1),
        ("fees/berth", "quay", "quay/fees/berth.py", 1),
        ("fees/berth", "quay", "quay/fees/rates.py", 1),
        ("fees/pilot", "quay", "quay/fees/pilot.py", 1),
        ("fees/pilot", "quay", "quay/fees/rates.py", 1),
        ("fees/pilot", "quay", "quay/fees/towage.py", 0),
        ("water", "quay", "quay/tides.py", 1),
    ]
    # A mask that matches nothing is reported.
    assert report["empty_masks"] == [
        {
            "domain": "fees/pilot",
            "project": "port",
            "service": "quay",
            "path": "quay/fees/towage.py",
            "files": 0,
        }
    ]
    # Two subdomains name the same file.
    assert report["overlaps"] == [
        {
            "domains": ["fees/berth", "fees/pilot"],
            "source": "quay",
            "files": 1,
            "examples": ["quay/fees/rates.py"],
        }
    ]
    # The share of each service's files in no domain: the form of the booking pages, the README.
    assert report["services"] == [
        {
            "service": "booking-web",
            "source": "lantern",
            "files": 2,
            "unassigned": 1,
            "unassigned_share": 0.5,
        },
        {
            "service": "quay",
            "source": "quay",
            "files": 5,
            "unassigned": 1,
            "unassigned_share": 0.2,
        },
    ]
    summary = text(result)
    assert "Masks that match no file:" in summary
    assert "- fees/pilot in service quay of project port: quay/fees/towage.py: 0 files" in summary
    assert "fees/berth and fees/pilot in repository quay: 1 file" in summary
    assert "Service quay (repository quay): 5 files, 1 in no domain (20% of its files)." in summary


async def test_a_document_without_warnings_and_a_project_that_goes_away(world: World) -> None:
    clean = with_pilot_paths(["quay/fees/pilot.py"])
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        report = ok(await put(client, clean))
        emptied = ok(await put(client, {"title": "Port operations"}))
        catalog = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))

    assert report["empty_masks"] == []
    assert report["overlaps"] == []
    # The document is the whole project: what it leaves out is removed.
    assert (emptied["changed"], emptied["version"], emptied["masks"]) == (True, 2, [])
    (port,) = catalog["projects"]
    assert (port["services"], port["domains"]) == ([], [])
    assert [each["source"] for each in catalog["unassigned_sources"]] == [
        "lantern",
        "quay",
        "spare",
    ]
    rows = await digests(world.db, "service", "domain", "domain_code")
    assert {table: count for table, (count, _) in rows.items()} == {
        "service": 0,
        "domain": 0,
        "domain_code": 0,
    }


async def test_an_older_document_does_not_overwrite_a_later_change(world: World) -> None:
    agent = await world.new_token("read", "ingest")
    person = await world.new_token("read", "ingest")
    edited = changed(PORT, title="Port and pilots")
    async with mcp_client(world.app(), agent.secret.get_secret_value()) as client:
        assert ok(await put(client, if_version=0))["version"] == 1
        async with mcp_client(world.app(), person.secret.get_secret_value()) as other:
            assert ok(await put(other, edited, if_version=1))["version"] == 2
        stored = await digests(world.db, *CATALOG_TABLES)
        stale = await put(client, if_version=1)
        must_be_new = await put(client, if_version=0)
        unknown = await put(client, project="later", if_version=3)
        catalog = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))
    async with http_client(world.app(), agent.secret.get_secret_value()) as http:
        conflict = await http.put(project_url(), json=PORT | {"if_version": 1})
        current = await http.put(project_url(), json=edited | {"if_version": 2})

    message = refused(stale)
    assert message.startswith("The project is at version 2, not the if_version given")
    assert "get_catalog" in message
    assert refused(must_be_new).startswith("The project is at version 2")
    assert refused(unknown).startswith("The project does not exist yet")
    assert error(conflict, 409, "version_conflict") == message
    # Nothing was written by the refused calls, and the project that did not exist still
    # does not.
    assert await digests(world.db, *CATALOG_TABLES) == stored
    (port,) = catalog["projects"]
    assert (port["name"], port["title"], port["version"]) == ("port", "Port and pilots", 2)
    # The same document with the version it was read at changes nothing.
    assert (body(current)["changed"], body(current)["version"]) == (False, 2)
    # Each entry says who changed it last: the person the project, the agent everything else.
    assert (port["updated_by"], port["updated_token"]) == (str(world.owner), str(person.info.id))
    for entry in (*port["services"], *port["domains"], *port["domains"][0]["subdomains"]):
        assert (entry["updated_by"], entry["updated_token"]) == (
            str(world.owner),
            str(agent.info.id),
        )
        assert when(entry["updated_at"]) <= when(port["updated_at"])


async def test_a_change_touches_only_the_entry_that_changed(world: World) -> None:
    agent = await world.new_token("read", "ingest")
    person = await world.new_token("read", "ingest")
    async with mcp_client(world.app(), agent.secret.get_secret_value()) as client:
        ok(await put(client))
    async with mcp_client(world.app(), person.secret.get_secret_value()) as client:
        result = ok(await put(client, with_pilot_paths(["quay/fees/pilot.py"])))
        catalog = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))

    assert (result["changed"], result["version"]) == (True, 2)
    (port,) = catalog["projects"]
    fees = port["domains"][0]
    berth, pilot = fees["subdomains"]
    assert port["updated_token"] == str(person.info.id)  # the project counts every change
    assert pilot["code"][0]["paths"] == ["quay/fees/pilot.py"]
    assert pilot["code"][0]["updated_token"] == str(person.info.id)
    assert pilot["updated_token"] == str(agent.info.id)  # the subdomain itself did not change
    for entry in (fees, berth, *berth["code"], *port["services"]):
        assert entry["updated_token"] == str(agent.info.id)


async def test_texts_are_redacted_and_a_refused_field_is_named_not_repeated(world: World) -> None:
    leaky = changed(
        PORT,
        description=f"Deploy with {STRIPE} from the README.",
        title="Port operations",
    )
    leaky["services"][0]["description"] = "password = hunter2!x in the compose file"
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        stored = ok(await put(client, leaky))
        catalog = await client.call_tool("get_catalog", {"workspace": "harbor"})
        again = ok(await put(client, leaky))
        # Read the rows now: a later document of this test replaces the project and its services.
        cur = await world.db.execute(
            "SELECT coalesce((SELECT string_agg(p::text, '') FROM project p), '')"
            " || coalesce((SELECT string_agg(s::text, '') FROM service s), '')"
        )
        row = await cur.fetchone()
        bad_name = await put(client, project="Port Operations")
        bad_mask = await put(client, with_pilot_paths(["quay/\nfees"]))
        two_levels = await put(client, {"domains": [{"name": "a", "subdomains": [{"name": "b"}]}]})
        deeper = await client.call_tool(
            "put_project",
            {
                "workspace": "harbor",
                "project": "port",
                "domains": [
                    {"name": "a", "subdomains": [{"name": "b", "subdomains": [{"name": "c"}]}]}
                ],
            },
        )
    async with http_client(world.app(), await world.token("read", "ingest")) as http:
        refused_root = await http.put(
            project_url(),
            json={"services": [{"name": "quay", "source": "quay", "root": "../etc/secret"}]},
        )

    assert stored["redacted"] == {"secret": 1, "stripe_key": 1}
    (port,) = ok(catalog)["projects"]
    assert port["description"] == "Deploy with [REDACTED:stripe_key] from the README."
    assert port["services"][1]["description"] == "password = [REDACTED:secret] in the compose file"
    # What is stored is what the same document cleans into: sending it again changes nothing.
    assert (again["changed"], again["version"]) == (False, 1)
    assert row is not None
    assert "[REDACTED:stripe_key]" in row[0]
    assert STRIPE not in row[0]
    assert "hunter2" not in row[0]
    assert STRIPE not in text(catalog)

    assert refused(bad_name).startswith("The project document is not valid: project: ")
    assert "Port Operations" not in refused(bad_name)
    assert refused(bad_mask) == (
        "The project document is not valid: domains[0].subdomains[1].code[0].paths[0]: a path"
        " glob holds a NUL or a line break."
    )
    assert ok(two_levels)["changed"] is True
    assert refused(deeper).startswith("Invalid arguments: domains.0.subdomains.0.subdomains")
    message = error(refused_root, 422, "invalid_project")
    assert message.startswith("The project document is not valid: services[0].root: a root is")
    assert "etc/secret" not in refused_root.text


# --- Search --------------------------------------------------------------------------------------


async def test_a_search_of_a_subdomain_returns_only_its_files_across_both_repositories(
    world: World,
) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client))
        everything = await found_in(client)
        berth = await found_in(client, domains=["fees/berth"])
        fees = await found_in(client, domains=["fees"])
        water = await found_in(client, domains=["water"])
        plans = await found_in(client, domains=["plans"])
        both = await found_in(client, domains=["fees/berth", "water"])
        booking = await found_in(client, services=["booking-web"])
        port = await found_in(client, projects=["port"])
        fees_in_booking = await found_in(client, domains=["fees"], services=["booking-web"])
        in_python = await found_in(client, domains=["fees"], languages=["python"])
        semantic = ok(
            await client.call_tool(
                "search_code",
                {"query": "what does a pilot cost", "domains": ["fees/pilot"], "limit": 50},
            )
        )
        general = ok(
            await client.call_tool(
                "search",
                {"query": "berth and pilot fees", "domains": ["fees/berth"], "limit": 50},
            )
        )

    assert places(everything) == QUAY_CODE | {("lantern", path) for path in LANTERN} | {
        ("spare", "tool.py")
    }
    # Only files under the masks of the subdomain, in both repositories.
    assert places(berth) == BERTH
    assert {hit["source"] for hit in berth["items"]} == {"quay", "lantern"}
    # A domain includes its subdomains; several values are alternatives.
    assert places(fees) == BERTH | PILOT
    assert places(water) == {("quay", "quay/tides.py")}
    assert places(both) == BERTH | {("quay", "quay/tides.py")}
    # A domain without code is known and has nothing: no hits, no error.
    assert plans["items"] == []
    assert places(booking) == BOOKING
    assert places(port) == QUAY_CODE | BOOKING
    # The filters all apply.
    assert places(fees_in_booking) == {("lantern", "web/booking/berth.ts")}
    assert places(in_python) == {place for place in BERTH | PILOT if place[0] == "quay"}
    # The ranked retrievers go through the same conditions.
    assert semantic["items"]
    assert places(semantic) <= PILOT
    assert general["items"]
    assert places(general) <= BERTH
    assert all(hit["source_kind"] == "repository" for hit in general["items"])
    assert general["decisions"] == []


async def test_a_hit_says_what_its_file_belongs_to(world: World) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        before = await found_in(client)
        ok(await put(client))
        result = await client.call_tool(
            "search_code",
            {"query": "harbour", "mode": "exact", "limit": 50, "workspaces": ["harbor"]},
        )
        symbols = await client.call_tool("search_code", {"query": "harbour_rate", "mode": "symbol"})
    async with http_client(world.app(), await world.token()) as http:
        over_http = await http.get(
            "/v1/search/code",
            params={"query": "harbour", "mode": "exact", "limit": 50, "workspaces": ["harbor"]},
        )
        symbols_over_http = await http.get(
            "/v1/search/code", params={"query": "harbour_rate", "mode": "symbol"}
        )

    nothing: Json = {"project": None, "service": None, "domains": []}
    assert hit_of(before, "quay/fees/rates.py")["context"] == nothing  # no catalog yet
    found = ok(result)
    # A file two subdomains name lists both.
    in_both = {
        "project": "port",
        "service": "quay",
        "domains": [
            {"project": "port", "domain": "fees/berth"},
            {"project": "port", "domain": "fees/pilot"},
        ],
    }
    assert hit_of(found, "quay/fees/rates.py")["context"] == in_both
    assert hit_of(found, "quay/tides.py")["context"] == {
        "project": "port",
        "service": "quay",
        "domains": [{"project": "port", "domain": "water"}],
    }
    # In a service and in no domain; a directory no service is; a repository no service is in.
    assert hit_of(found, "web/booking/form.ts")["context"] == {
        "project": "port",
        "service": "booking-web",
        "domains": [],
    }
    assert hit_of(found, "web/admin/users.ts")["context"] == nothing
    assert hit_of(found, "tool.py")["context"] == nothing
    summary = text(result)
    assert "belongs to: project port, service quay; domains: fees/berth, fees/pilot" in summary
    assert "belongs to: project port, service booking-web; domains: none" in summary
    # A hit of a file in no service has no such line.
    in_a_service = [hit for hit in found["items"] if hit["context"]["service"] is not None]
    assert 0 < len(in_a_service) < len(found["items"])
    assert summary.count("belongs to: ") == len(in_a_service)
    (definition,) = ok(symbols)["items"]
    assert definition["definition"]["name"] == "harbour_rate"
    assert definition["context"] == in_both
    assert "belongs to: project port, service quay" in text(symbols)
    # The HTTP routes return the same shapes.
    assert body(over_http) == found
    assert body(symbols_over_http) == ok(symbols)


async def test_get_file_says_what_the_path_belongs_to(world: World) -> None:
    async def read(client: Client, source: str, path: str) -> CallToolResult:
        return await client.call_tool(
            "get_file", {"workspace": "harbor", "source": source, "path": path}
        )

    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client))
        rates = await read(client, "quay", "quay/fees/rates.py")
        directory = ok(await read(client, "quay", "quay/fees"))
        root = ok(await read(client, "lantern", ""))
        booking = ok(await read(client, "lantern", "web/booking"))
        outside = ok(await read(client, "spare", "tool.py"))
    async with http_client(world.app(), await world.token()) as http:
        over_http = await http.get("/v1/workspaces/harbor/sources/quay/files/quay/fees/rates.py")

    assert ok(rates)["context"] == {
        "project": "port",
        "service": "quay",
        "domains": [
            {"project": "port", "domain": "fees/berth"},
            {"project": "port", "domain": "fees/pilot"},
        ],
    }
    assert "; belongs to: project port, service quay; domains: fees/berth, fees/pilot)" in text(
        rates
    )
    assert directory["context"] == {"project": "port", "service": "quay", "domains": []}
    assert root["context"] == {"project": None, "service": None, "domains": []}
    assert booking["context"] == {"project": "port", "service": "booking-web", "domains": []}
    assert outside["context"] == {"project": None, "service": None, "domains": []}
    assert body(over_http) == ok(rates)


async def test_changing_a_mask_changes_results_without_a_reindex(world: World) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client))
        index = await digests(world.db, *INDEX_TABLES)
        before = await found_in(client, domains=["fees/pilot"])
        moved = ok(await put(client, with_pilot_paths(["quay/tides.py"]), if_version=1))
        after = await found_in(client, domains=["fees/pilot"])
        domain = ok(
            await client.call_tool(
                "get_domain", {"workspace": "harbor", "project": "port", "domain": "fees/pilot"}
            )
        )

    assert places(before) == PILOT
    assert (moved["changed"], moved["version"]) == (True, 2)
    assert places(after) == {("quay", "quay/tides.py")}
    assert hit_of(after, "quay/tides.py")["context"]["domains"] == [
        {"project": "port", "domain": "fees/pilot"},
        {"project": "port", "domain": "water"},
    ]
    assert domain["files"] == 1
    # Not a record, not a chunk, not a symbol was written, and the fingerprint is the same.
    assert await digests(world.db, *INDEX_TABLES) == index
    assert index["chunk"][0] > 0


async def test_a_name_the_catalog_does_not_hold_is_an_error_not_an_empty_answer(
    world: World,
) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        no_catalog = await client.call_tool("search_code", {"query": "x", "domains": ["fees"]})
        ok(await put(client))
        domain = await client.call_tool(
            "search_code", {"query": "harbour", "mode": "exact", "domains": ["fees", "towage"]}
        )
        part = await client.call_tool("search", {"query": "harbour", "domains": ["berth"]})
        service = await client.call_tool("search_code", {"query": "x", "services": ["lantern"]})
        project = await client.call_tool("search", {"query": "x", "projects": ["harbor"]})
        scoped = await client.call_tool(
            "search_code", {"query": "x", "projects": ["port"], "domains": ["locks"]}
        )
    async with http_client(world.app(), await world.token()) as http:
        over_http = await http.get(
            "/v1/search/code", params={"query": "harbour", "domains": ["fees", "towage"]}
        )
        unknown_service = await http.get("/v1/search", params={"query": "x", "services": ["no"]})
        unknown_project = await http.get("/v1/search", params={"query": "x", "projects": ["no"]})

    assert refused(no_catalog) == (
        "Value 1 of the domains filter names no domain of the catalog in the projects searched;"
        " get_catalog lists the names."
    )
    assert refused(domain).startswith("Value 2 of the domains filter names no domain")
    assert "towage" not in refused(domain)
    assert refused(part).startswith("Value 1 of the domains filter")  # it is fees/berth
    assert refused(service).startswith("Value 1 of the services filter names no service")
    assert refused(project) == (
        "Value 1 of the projects filter names no project of the catalog; get_catalog lists the"
        " names."
    )
    assert refused(scoped).startswith("Value 1 of the domains filter")
    assert error(over_http, 422, "unknown_domain") == refused(domain)
    assert error(unknown_service, 422, "unknown_service")
    assert error(unknown_project, 422, "unknown_project")


# --- get_catalog and get_domain ------------------------------------------------------------------


async def test_get_catalog_lists_the_projects_and_a_repository_without_a_service(
    world: World,
) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        empty = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))
        ok(await put(client))
        result = await client.call_tool("get_catalog", {"workspace": "harbor"})
    async with http_client(world.app(), await world.token()) as http:
        over_http = await http.get("/v1/workspaces/harbor/catalog")

    assert empty["projects"] == []
    assert [each["source"] for each in empty["unassigned_sources"]] == [
        "lantern",
        "quay",
        "spare",
    ]
    catalog = ok(result)
    assert (catalog["data_notice"], catalog["workspace"]) == (DATA_NOTICE, "harbor")
    (port,) = catalog["projects"]
    assert (port["name"], port["title"], port["version"]) == ("port", "Port operations", 1)
    assert port["description"] == "Fees and bookings of the synthetic harbour."
    booking, quay = port["services"]
    assert (quay["name"], quay["source"], quay["root"], quay["files"]) == (
        "quay",
        "quay",
        "",
        5,
    )
    assert quay["refs"] == [{"ref": MAIN, "commit": world.pins["quay"]}]
    assert quay["languages"] == [{"language": "python", "files": 4}]
    assert (booking["name"], booking["source"], booking["root"], booking["files"]) == (
        "booking-web",
        "lantern",
        "web/booking",
        2,
    )
    assert booking["languages"] == [{"language": "typescript", "files": 2}]
    assert [(d["name"], d["domain"]) for d in port["domains"]] == [
        ("fees", "fees"),
        ("plans", "plans"),
        ("water", "water"),
    ]
    fees, plans, water = port["domains"]
    assert (fees["title"], fees["code"]) == ("Fees", [])
    assert [(part["name"], part["domain"]) for part in fees["subdomains"]] == [
        ("berth", "fees/berth"),
        ("pilot", "fees/pilot"),
    ]
    # The code of a subdomain in the shape put_project takes it.
    assert [
        {key: code[key] for key in ("project", "service", "paths")}
        for code in fees["subdomains"][0]["code"]
    ] == [
        {"project": "port", "service": "booking-web", "paths": ["berth.ts"]},
        {
            "project": "port",
            "service": "quay",
            "paths": ["quay/fees/berth.py", "quay/fees/rates.py"],
        },
    ]
    assert (plans["code"], plans["subdomains"]) == ([], [])
    assert water["code"][0]["paths"] == ["quay/tides.py"]
    # A repository no service is in. lantern has one, over a directory of it.
    assert catalog["unassigned_sources"] == [
        {
            "source": "spare",
            "refs": [{"ref": MAIN, "commit": world.pins["spare"]}],
            "files": 1,
            "languages": [{"language": "python", "files": 1}],
        }
    ]
    summary = text(result)
    assert summary.startswith(
        f"Catalog of workspace harbor: 1 project, 1 repository without a service. {TEXT_NOTICE}"
    )
    assert "Service booking-web: repository lantern, directory web/booking; 2 files" in summary
    assert "```\nWhat a call at the harbour costs.\n```" in summary
    # HTTP and MCP return the same shape.
    assert body(over_http) == catalog


def coverage_url(source: str, workspace: str = "harbor") -> str:
    return f"/v1/workspaces/{workspace}/sources/{source}/coverage"


async def test_the_coverage_of_a_repository_says_what_the_domains_name_and_what_they_leave(
    world: World,
) -> None:
    agent = await world.new_token("read", "ingest")
    async with http_client(world.app(), await world.token()) as http:
        before = body(await http.get(coverage_url("quay")))
    async with mcp_client(world.app(), agent.secret.get_secret_value()) as client:
        ok(await put(client))
    async with http_client(world.app(), await world.token()) as http:
        quay = body(await http.get(coverage_url("quay")))
        lantern = body(await http.get(coverage_url("lantern")))
        spare = body(await http.get(coverage_url("spare")))
        missing = error(await http.get(coverage_url("nowhere")), 404, "not_found")
        error(await http.get(coverage_url("vault")), 404, "not_found")
    limited = await world.token(workspace=world.other)
    async with http_client(world.app(), limited) as http:
        error(await http.get(coverage_url("quay")), 404, "not_found")
        vault = body(await http.get(coverage_url("vault", "other")))

    # Before the map: every file is uncovered, and no project maps it.
    assert (before["files"], before["covered"], before["covered_share"]) == (5, 0, 0.0)
    assert before["projects"] == []
    assert before["uncovered_folders"] == [
        {"folder": "quay", "files": 4},
        {"folder": "(root)", "files": 1},
    ]
    # quay: four of its five files are in a domain; the README is not.
    assert (quay["workspace"], quay["source"]) == ("harbor", "quay")
    assert (quay["files"], quay["covered"], quay["covered_share"]) == (5, 4, 0.8)
    assert quay["uncovered_folders"] == [{"folder": "(root)", "files": 1}]
    (port,) = quay["projects"]
    assert (port["project"], port["services"], port["files"]) == ("port", ["quay"], 4)
    assert port["domains"] == ["fees/berth", "fees/pilot", "water"]
    assert port["updated_token"] == str(agent.info.id)
    assert port["updated_by"] == str(world.owner)
    assert when(port["updated_at"]) is not None
    # lantern: the service is one folder of it; only the berth quote is in a domain.
    assert (lantern["files"], lantern["covered"], lantern["covered_share"]) == (3, 1, 0.3333)
    assert lantern["projects"][0]["services"] == ["booking-web"]
    assert lantern["projects"][0]["domains"] == ["fees/berth"]
    assert lantern["uncovered_folders"] == [{"folder": "web", "files": 2}]
    # spare: no service is in it, so nothing maps it.
    assert (spare["files"], spare["covered"], spare["projects"]) == (1, 0, [])
    assert vault["projects"] == []
    assert "nowhere" not in missing


async def test_the_streams_of_a_repository_say_what_the_index_holds(world: World) -> None:
    async with http_client(world.app(), await world.token()) as http:
        (quay,) = body(await http.get("/v1/workspaces/harbor/sources/quay/streams"))["items"]
        (lantern,) = body(await http.get("/v1/workspaces/harbor/sources/lantern/streams"))["items"]
    chunks = await world.db.execute(
        "SELECT count(*) FROM chunk c JOIN record r ON r.id = c.record_id"
        " JOIN source s ON s.id = r.source_id WHERE s.name = 'quay' AND r.deleted_at IS NULL"
    )
    row = await chunks.fetchone()
    assert row is not None

    assert quay["index"]["records"] == 5
    assert quay["index"]["chunks"] == row[0] > 0
    assert quay["index"]["commit"] == world.pins["quay"]
    assert when(quay["index"]["committed_at"]) <= datetime.now(UTC)
    assert lantern["index"]["records"] == 3
    assert lantern["index"]["commit"] == world.pins["lantern"]


async def test_what_get_catalog_returns_goes_back_into_put_project(world: World) -> None:
    def document(project: Json) -> Json:
        """The stored project as its document: the names of the document's fields."""

        def domain(entry: Json) -> Json:
            return {
                "name": entry["name"],
                "title": entry["title"],
                "description": entry["description"],
                "code": [
                    {key: code[key] for key in ("project", "service", "paths")}
                    for code in entry["code"]
                ],
            }

        return {
            "title": project["title"],
            "description": project["description"],
            "services": [
                {key: service[key] for key in ("name", "description", "source", "root")}
                for service in project["services"]
            ],
            "domains": [
                domain(entry) | {"subdomains": [domain(part) for part in entry["subdomains"]]}
                for entry in project["domains"]
            ],
            "if_version": project["version"],
        }

    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client))
        (port,) = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))["projects"]
        back = ok(await put(client, document(port)))

    assert (back["changed"], back["version"]) == (False, 1)


async def test_get_domain_lists_both_services_and_what_each_mask_matches(world: World) -> None:
    async def get(client: Client, domain: str, project: str = "port") -> CallToolResult:
        return await client.call_tool(
            "get_domain", {"workspace": "harbor", "project": project, "domain": domain}
        )

    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client))
        result = await get(client, "fees")
        berth = ok(await get(client, "fees/berth"))
        plans = ok(await get(client, "plans"))
        missing = await get(client, "towage")
        too_deep = await get(client, "fees/berth/deep")
        no_project = await get(client, "fees", project="nowhere")
    async with http_client(world.app(), await world.token()) as http:
        over_http = await http.get(f"{project_url()}/domains/fees")
        part_over_http = await http.get(f"{project_url()}/domains/fees/berth")
        missing_over_http = await http.get(f"{project_url()}/domains/towage")

    fees = ok(result)
    assert (fees["project"], fees["domain"], fees["parent"]) == ("port", "fees", None)
    assert (fees["title"], fees["description"]) == ("Fees", "What a call at the harbour costs.")
    # Each file once: berth.py, pilot.py, rates.py and berth.ts.
    assert (fees["files"], fees["code"]) == (4, [])
    parts = {part["domain"]: part for part in fees["subdomains"]}
    assert list(parts) == ["fees/berth", "fees/pilot"]
    assert (parts["fees/berth"]["files"], parts["fees/pilot"]["files"]) == (3, 2)
    assert parts["fees/berth"]["code"] == [
        {
            "project": "port",
            "service": "booking-web",
            "source": "lantern",
            "root": "web/booking",
            "files": 1,
            "paths": [{"path": "berth.ts", "files": 1}],
        },
        {
            "project": "port",
            "service": "quay",
            "source": "quay",
            "root": "",
            "files": 2,
            "paths": [
                {"path": "quay/fees/berth.py", "files": 1},
                {"path": "quay/fees/rates.py", "files": 1},
            ],
        },
    ]
    assert parts["fees/pilot"]["code"][0]["paths"][2] == {
        "path": "quay/fees/towage.py",
        "files": 0,
    }
    assert (berth["domain"], berth["parent"], berth["files"]) == ("fees/berth", "fees", 3)
    assert berth["code"] == parts["fees/berth"]["code"]
    assert berth["subdomains"] == []
    assert (plans["files"], plans["code"]) == (0, [])
    summary = text(result)
    assert summary.startswith("Domain fees of project port in workspace harbor: 4 files; changed")
    assert TEXT_NOTICE in summary.split("\n")[0]
    assert "- quay/fees/towage.py: 0 files" in summary
    assert refused(missing) == "No such domain."
    assert refused(too_deep) == "No such domain."
    assert refused(no_project) == "No such project."
    assert body(over_http) == fees
    assert body(part_over_http) == berth
    assert error(missing_over_http, 404, "not_found") == "No such domain."


# --- Projects beside each other ------------------------------------------------------------------

LIGHTS: Json = {
    "title": "Lights",
    "services": [{"name": "admin-web", "source": "lantern", "root": "web/admin"}],
    "domains": [
        {
            "name": "signals",
            "code": [
                {"service": "admin-web", "paths": ["**"]},
                # Code in a service of another project of the workspace.
                {"project": "port", "service": "quay", "paths": ["quay/tides.py"]},
            ],
        }
    ],
}


async def test_a_domain_names_a_service_of_another_project_of_its_workspace(world: World) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client))
        lights = ok(await put(client, LIGHTS, project="lights"))
        found = await found_in(client)
        of_lights = await found_in(client, projects=["lights"])
        signals = await found_in(client, projects=["lights"], domains=["signals"])
        not_in_port = await client.call_tool(
            "search_code", {"query": "x", "projects": ["port"], "domains": ["signals"]}
        )
        domain = ok(
            await client.call_tool(
                "get_domain", {"workspace": "harbor", "project": "lights", "domain": "signals"}
            )
        )
        # A directory of a repository is one service: port has web/booking already.
        twice = await put(
            client,
            {"services": [{"name": "pages", "source": "lantern", "root": "web/booking/"}]},
            project="again",
        )
        # port cannot drop the service that lights names code in.
        dropped = await put(client, {"title": "Port operations"})
        no_service = await put(
            client,
            {
                "domains": [
                    {"name": "a", "code": [{"project": "port", "service": "tug", "paths": ["x"]}]}
                ]
            },
            project="again",
        )
        no_repository = await put(
            client, {"services": [{"name": "safe", "source": "vault"}]}, project="again"
        )
        catalog = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))
    async with http_client(world.app(), await world.token("read", "ingest")) as http:
        twice_over_http = await http.put(
            project_url("again"),
            json={"services": [{"name": "pages", "source": "lantern", "root": "web/booking"}]},
        )
        dropped_over_http = await http.put(project_url(), json={"title": "Port operations"})

    assert (lights["created"], lights["empty_masks"], lights["overlaps"]) == (True, [], [])
    # The file is in port's service, and domains of both projects name it.
    assert hit_of(found, "quay/tides.py")["context"] == {
        "project": "port",
        "service": "quay",
        "domains": [
            {"project": "lights", "domain": "signals"},
            {"project": "port", "domain": "water"},
        ],
    }
    assert hit_of(found, "web/admin/users.ts")["context"] == {
        "project": "lights",
        "service": "admin-web",
        "domains": [{"project": "lights", "domain": "signals"}],
    }
    # A project is its services and the code its domains name, wherever that is.
    expected = {("lantern", "web/admin/users.ts"), ("quay", "quay/tides.py")}
    assert places(of_lights) == expected
    assert places(signals) == expected
    assert refused(not_in_port).startswith("Value 1 of the domains filter")
    assert [(code["project"], code["service"], code["files"]) for code in domain["code"]] == [
        ("lights", "admin-web", 1),
        ("port", "quay", 1),
    ]
    assert "is that directory of that repository already" in refused(twice)
    assert error(twice_over_http, 409, "service_conflict") == refused(twice)
    assert refused(dropped).startswith("The document removes a service that a domain of another")
    assert error(dropped_over_http, 409, "service_in_use") == refused(dropped)
    assert refused(no_service) == "No such service (domains[0].code[0])."
    # A repository of another workspace is not there to name.
    assert refused(no_repository) == "No such repository (services[0].source)."
    # None of the refused documents left anything behind.
    assert [project["name"] for project in catalog["projects"]] == ["lights", "port"]
    assert catalog["projects"][1]["version"] == 1


async def test_a_service_goes_with_its_repository_and_the_export_holds_the_catalog(
    world: World,
) -> None:
    async def exported(private: bool) -> Json:
        lines = [
            json.loads(line)
            async for line in export_workspace(world.db, "owner", "harbor", include_private=private)
        ]
        (project,) = [line for line in lines if line["type"] == "project"]
        return project

    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client))
        whole = await exported(private=True)
        public = await exported(private=False)
        await delete_source(
            world.db,
            world.access,
            "harbor",
            "lantern",
            mirrors=MirrorManager(GitOptions.from_settings(world.settings)),
        )
        catalog = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))
        berth = await found_in(client, domains=["fees/berth"])

    # The export holds the project as its document, with its version.
    assert (whole["workspace"], whole["name"], whole["version"]) == ("harbor", "port", 1)
    assert [service["name"] for service in whole["services"]] == ["booking-web", "quay"]
    assert [domain["name"] for domain in whole["domains"]] == ["fees", "plans", "water"]
    berth_code = whole["domains"][0]["subdomains"][0]["code"]
    assert [(code["service"], code["paths"]) for code in berth_code] == [
        ("booking-web", ["berth.ts"]),
        ("quay", ["quay/fees/berth.py", "quay/fees/rates.py"]),
    ]
    assert whole["updated_by"] == str(world.owner)
    # The repositories are private sources: without --include-private their services and the
    # code that names them stay out, as their records do. The domains are not theirs.
    assert public["services"] == []
    assert [domain["name"] for domain in public["domains"]] == ["fees", "plans", "water"]
    assert public["domains"][0]["subdomains"][0]["code"] == []

    # Deleting a repository deletes the services over it and the masks that name them.
    (port,) = catalog["projects"]
    assert [service["name"] for service in port["services"]] == ["quay"]
    fees = port["domains"][0]
    assert [part["name"] for part in fees["subdomains"]] == ["berth", "pilot"]
    assert [code["service"] for code in fees["subdomains"][0]["code"]] == ["quay"]
    assert places(berth) == {place for place in BERTH if place[0] == "quay"}
    assert [each["source"] for each in catalog["unassigned_sources"]] == ["spare"]


# --- Links between services ----------------------------------------------------------------------

LINKED_PORT: Json = changed(PORT)
LINKED_PORT["services"][1]["uses"] = [
    # A link inside the project: the booking pages import the fee engine.
    {"service": "quay", "kind": "library", "paths": ["berth.ts"]}
]

SIGNALS: Json = {
    "title": "Signals",
    "services": [
        {
            "name": "admin-web",
            "source": "lantern",
            "root": "web/admin",
            "uses": [
                {
                    "project": "port",
                    "service": "quay",
                    "kind": "http",
                    "note": "Reads the harbour rates for the staff pages.",
                    "paths": ["users.ts", "rates.ts"],
                }
            ],
        }
    ],
}
"""A second project whose only service uses a service of port; ``rates.ts`` does not exist."""


def link_of(link: Json) -> tuple[Any, ...]:
    return (
        link["from_project"],
        link["from_service"],
        link["to_project"],
        link["to_service"],
        link["kind"],
        link["source"],
        link["root"],
        [(mask["path"], mask["files"]) for mask in link["paths"]],
    )


ADMIN_USES_QUAY = (
    "signals",
    "admin-web",
    "port",
    "quay",
    "http",
    "lantern",
    "web/admin",
    [("users.ts", 1), ("rates.ts", 0)],
)
BOOKING_USES_QUAY = (
    "port",
    "booking-web",
    "port",
    "quay",
    "library",
    "lantern",
    "web/booking",
    [("berth.ts", 1)],
)


async def test_a_link_is_returned_by_get_project_of_both_projects_in_its_direction(
    world: World,
) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client, LINKED_PORT))
        written = await put(client, SIGNALS, project="signals")
        port = await client.call_tool("get_project", {"workspace": "harbor", "project": "port"})
        signals = ok(
            await client.call_tool("get_project", {"workspace": "harbor", "project": "signals"})
        )
        between = ok(
            await client.call_tool(
                "get_project",
                {"workspace": "harbor", "project": "port", "linked_projects": ["signals"]},
            )
        )
        inside = ok(
            await client.call_tool(
                "get_project",
                {"workspace": "harbor", "project": "port", "linked_projects": ["port"]},
            )
        )
        missing = await client.call_tool("get_project", {"workspace": "harbor", "project": "tug"})
        no_other = await client.call_tool(
            "get_project", {"workspace": "harbor", "project": "port", "linked_projects": ["tug"]}
        )
        catalog = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))
    async with http_client(world.app(), await world.token("read")) as http:
        port_over_http = await http.get(project_url())
        between_over_http = await http.get(project_url(), params={"linked_projects": ["signals"]})
        missing_over_http = await http.get(project_url("tug"))
    exported = [
        json.loads(line)
        async for line in export_workspace(world.db, "owner", "harbor", include_private=True)
    ]

    # put_project reports the masks of the links, and the one that matches nothing.
    report = ok(written)
    assert [
        (m["service"], m["to_project"], m["to_service"], m["path"], m["files"])
        for m in report["link_masks"]
    ] == [
        ("admin-web", "port", "quay", "users.ts", 1),
        ("admin-web", "port", "quay", "rates.ts", 0),
    ]
    assert [mask["path"] for mask in report["empty_link_masks"]] == ["rates.ts"]
    assert "Masks of links that match no file:" in text(written)

    # port: what it uses itself, and what uses it from signals.
    detail = ok(port)
    assert detail["data_notice"] == DATA_NOTICE
    assert (detail["workspace"], detail["project"]["name"]) == ("harbor", "port")
    assert [link_of(link) for link in detail["uses"]] == [BOOKING_USES_QUAY]
    assert [link_of(link) for link in detail["used_by"]] == [ADMIN_USES_QUAY]
    assert detail["used_by"][0]["note"] == "Reads the harbour rates for the staff pages."
    # signals: the same link, from the other end.
    assert [link_of(link) for link in signals["uses"]] == [ADMIN_USES_QUAY]
    assert signals["used_by"] == []
    # The filter by the other project.
    assert ([link_of(link) for link in between["uses"]], between["used_by"]) == (
        [],
        detail["used_by"],
    )
    assert (inside["uses"], inside["used_by"]) == (detail["uses"], [])
    assert refused(missing) == "No such project."
    assert refused(no_other) == "No such project."
    summary = text(port)
    assert summary.startswith("Project port in workspace harbor, version 1, changed "), summary
    assert TEXT_NOTICE in summary.splitlines()[0]
    assert "service admin-web of project signals uses service quay of project port (http)" in (
        summary
    )
    assert "```\nReads the harbour rates for the staff pages.\n```" in summary

    # get_catalog has each link under the service that uses, in the shape of the document.
    by_name = {project["name"]: project for project in catalog["projects"]}
    (admin,) = by_name["signals"]["services"]
    assert [
        {key: link[key] for key in ("project", "service", "kind", "note", "paths")}
        for link in admin["uses"]
    ] == [SIGNALS["services"][0]["uses"][0]]
    assert [service["uses"] for service in by_name["port"]["services"]][1] == []  # quay

    # HTTP and MCP return the same shapes.
    assert body(port_over_http) == detail
    assert body(between_over_http) == between
    assert error(missing_over_http, 404, "not_found") == "No such project."

    # The export holds the links with the services that use them.
    projects = {line["name"]: line for line in exported if line["type"] == "project"}
    (exported_admin,) = projects["signals"]["services"]
    assert [
        (link["project"], link["service"], link["kind"]) for link in exported_admin["uses"]
    ] == [("port", "quay", "http")]


async def test_links_sent_twice_change_nothing_and_a_change_is_one_version(world: World) -> None:
    def document(project: Json) -> Json:
        """What get_catalog says of a project with services only, as its document."""
        return {
            "title": project["title"],
            "description": project["description"],
            "services": [
                {key: service[key] for key in ("name", "description", "source", "root")}
                | {
                    "uses": [
                        {key: link[key] for key in ("project", "service", "kind", "note", "paths")}
                        for link in service["uses"]
                    ]
                }
                for service in project["services"]
            ],
            "if_version": project["version"],
        }

    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client, LINKED_PORT))
        first = ok(await put(client, SIGNALS, project="signals"))
        stored = await digests(world.db, *CATALOG_TABLES)
        again = ok(await put(client, SIGNALS, project="signals"))
        after = await digests(world.db, *CATALOG_TABLES)
        catalog = ok(await client.call_tool("get_catalog", {"workspace": "harbor"}))
        (read,) = [each for each in catalog["projects"] if each["name"] == "signals"]
        back = ok(await put(client, document(read), project="signals"))
        noted = changed(SIGNALS)
        noted["services"][0]["uses"][0] |= {"kind": "other", "note": "Polls the rates."}
        renoted = ok(await put(client, noted, project="signals"))
        link_rows = await digests(world.db, "service_link")
        service_rows = await digests(world.db, "service")
        unlinked = changed(SIGNALS)
        del unlinked["services"][0]["uses"]
        dropped = ok(await put(client, unlinked, project="signals"))
        port = ok(await client.call_tool("get_project", {"workspace": "harbor", "project": "port"}))

    assert (first["created"], first["version"]) == (True, 1)
    assert (again["changed"], again["version"]) == (False, 1)
    assert again["link_masks"] == first["link_masks"]
    assert after == stored
    assert stored["service_link"][0] == 2
    # What get_catalog returns goes back unchanged.
    assert (back["changed"], back["version"]) == (False, 1)
    # A changed kind and note is one change: the link's row, not the service's.
    assert (renoted["changed"], renoted["version"]) == (True, 2)
    assert link_rows["service_link"] != stored["service_link"]
    assert service_rows["service"] == stored["service"]
    # A link the document leaves out is removed.
    assert (dropped["changed"], dropped["version"]) == (True, 3)
    assert [link_of(link) for link in port["uses"]] == [BOOKING_USES_QUAY]
    assert port["used_by"] == []


async def test_a_link_out_of_the_workspace_or_to_no_service_is_refused(world: World) -> None:
    def using(link: Json) -> Json:
        return {
            "services": [
                {"name": "admin-web", "source": "lantern", "root": "web/admin", "uses": [link]}
            ]
        }

    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client, LINKED_PORT))
        ok(await put(client, SAFE, project="safe", workspace="other"))
        stored = await digests(world.db, *CATALOG_TABLES)
        # vault is a service of the other workspace: here it is not there to name.
        other_workspace = await put(
            client,
            using({"project": "safe", "service": "vault", "kind": "http"}),
            project="signals",
        )
        no_service = await put(
            client, using({"project": "port", "service": "tug", "kind": "queue"}), project="signals"
        )
        not_in_document = await put(
            client, using({"service": "quay", "kind": "library"}), project="signals"
        )
        after_refusals = await digests(world.db, *CATALOG_TABLES)
        ok(await put(client, SIGNALS, project="signals"))
        # port cannot drop the service that a link of signals names.
        kept = changed(LINKED_PORT, domains=[])
        kept["services"] = [{"name": "booking-web", "source": "lantern", "root": "web/booking"}]
        dropped = await put(client, kept)
    async with http_client(world.app(), await world.token("read", "ingest")) as http:
        other_over_http = await http.put(
            project_url("signals"),
            json=using({"project": "safe", "service": "vault", "kind": "http"}),
        )
        dropped_over_http = await http.put(project_url(), json=kept)

    assert refused(other_workspace) == "No such service (services[0].uses[0])."
    assert error(other_over_http, 404, "not_found") == refused(other_workspace)
    assert refused(no_service) == refused(other_workspace)
    assert refused(not_in_document) == (
        "The project document is not valid: services[0].uses[0].service: names a service the"
        " document does not have."
    )
    # A refused document leaves nothing behind, not even the project.
    assert after_refusals == stored
    assert refused(dropped).startswith(
        "The document removes a service that a service of another project declares it uses."
    )
    assert error(dropped_over_http, 409, "service_in_use") == refused(dropped)

    # The database keeps a link inside its workspace whatever the code does.
    row = await (
        await world.db.execute(
            "SELECT s.id, s.workspace_id, s.owner_id, v.id FROM service s, service v"
            " WHERE s.name = 'admin-web' AND v.name = 'vault'"
        )
    ).fetchone()
    assert row is not None
    with pytest.raises(errors.ForeignKeyViolation):
        await world.db.execute(
            "INSERT INTO service_link (workspace_id, owner_id, service_id, target_id, kind,"
            " updated_by) VALUES (%s, %s, %s, %s, 'http', %s)",
            (row[1], row[2], row[0], row[3], row[2]),
        )
    with pytest.raises(errors.CheckViolation):
        await world.db.execute(
            "INSERT INTO service_link (workspace_id, owner_id, service_id, target_id, kind,"
            " updated_by) VALUES (%s, %s, %s, %s, 'http', %s)",
            (row[1], row[2], row[0], row[0], row[2]),
        )


async def test_a_link_goes_with_the_repository_of_either_service(world: World) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client, LINKED_PORT))
        ok(await put(client, SIGNALS, project="signals"))
        await delete_source(
            world.db,
            world.access,
            "harbor",
            "lantern",
            mirrors=MirrorManager(GitOptions.from_settings(world.settings)),
        )
        port = ok(await client.call_tool("get_project", {"workspace": "harbor", "project": "port"}))
        signals = ok(
            await client.call_tool("get_project", {"workspace": "harbor", "project": "signals"})
        )

    # Both services that used quay were over lantern: they and their links are gone.
    assert (port["uses"], port["used_by"]) == ([], [])
    assert (signals["project"]["services"], signals["uses"]) == ([], [])
    assert (await digests(world.db, "service_link"))["service_link"][0] == 0


# --- Access --------------------------------------------------------------------------------------


async def test_a_workspace_token_sees_no_other_workspaces_catalog(world: World) -> None:
    async with mcp_client(world.app(), await world.token("read", "ingest")) as client:
        ok(await put(client))
        ok(await put(client, SAFE, project="safe", workspace="other"))
        unnamed = await client.call_tool("get_catalog", {})
    stored = await digests(world.db, *CATALOG_TABLES)

    limited = await world.token("read", "ingest", workspace=world.other)
    async with mcp_client(world.app(), limited) as client:
        own = ok(await client.call_tool("get_catalog", {}))
        harbor = await client.call_tool("get_catalog", {"workspace": "harbor"})
        domain = await client.call_tool(
            "get_domain", {"workspace": "harbor", "project": "port", "domain": "fees"}
        )
        own_domain = ok(
            await client.call_tool("get_domain", {"project": "safe", "domain": "locks"})
        )
        write = await put(client, changed(PORT, title="Taken over"))
        filtered = await client.call_tool("search_code", {"query": "harbour", "domains": ["fees"]})
        named = await client.call_tool(
            "search_code", {"query": "harbour", "mode": "exact", "projects": ["safe"]}
        )
        everything = ok(
            await client.call_tool("search_code", {"query": "harbour", "mode": "exact"})
        )
    async with http_client(world.app(), limited) as http:
        catalog_over_http = await http.get("/v1/workspaces/harbor/catalog")
        domain_over_http = await http.get(f"{project_url()}/domains/fees")
        write_over_http = await http.put(project_url(), json=changed(PORT, title="Taken over"))
        missing_over_http = await http.get("/v1/workspaces/nowhere/catalog")
        own_over_http = await http.get("/v1/workspaces/other/catalog")

    # Without a workspace: the one the token reads, or a question when it reads several.
    assert refused(unnamed) == "The token reads more than one workspace: name the one."
    assert (own["workspace"], [project["name"] for project in own["projects"]]) == (
        "other",
        ["safe"],
    )
    assert own["unassigned_sources"] == []
    assert own_domain["files"] == 1
    # The other workspace looks exactly like one that does not exist, read or written.
    assert refused(harbor) == "No such workspace."
    assert refused(domain) == "No such workspace."
    assert refused(write) == "No such workspace."
    assert error(catalog_over_http, 404, "not_found") == "No such workspace."
    assert error(missing_over_http, 404, "not_found") == "No such workspace."
    assert error(domain_over_http, 404, "not_found") == "No such workspace."
    assert error(write_over_http, 404, "not_found") == "No such workspace."
    assert body(own_over_http) == own
    assert await digests(world.db, *CATALOG_TABLES) == stored
    # Its names are not names here, and nothing of it is in any answer.
    assert refused(filtered).startswith("Value 1 of the domains filter names no domain")
    assert places(ok(named)) == {("vault", "vault.py")}
    assert {hit["workspace"] for hit in everything["items"]} == {"other"}
    assert hit_of(everything, "vault.py")["context"] == {
        "project": "safe",
        "service": "vault",
        "domains": [{"project": "safe", "domain": "locks"}],
    }
    for answer in (own, own_domain, everything):
        for name in ("Port operations", "quay", "booking-web", "lantern", "fees"):
            assert name not in json.dumps(answer), name


async def test_put_project_needs_ingest_and_read_and_the_reads_need_read(world: World) -> None:
    reader = await world.token("read")
    pusher = await world.token("ingest")
    both = await world.token("read", "ingest")
    app = world.app()
    async with mcp_client(app, reader) as client:
        listed = [tool.name for tool in (await client.list_tools()).tools]
        with pytest.raises(MCPError, match=r"^Unknown tool\.$"):
            await put(client)
        guide = await client.call_tool("catalog_guide", {})
    async with http_client(app, reader) as http:
        read_only = await http.put(project_url(), json=PORT)
        guide_over_http = await http.get("/v1/catalog/guide")
    async with http_client(app, pusher) as http:
        # A token that may only push would learn file counts from the report.
        push_only = await http.put(project_url(), json=PORT)
        no_catalog = await http.get("/v1/workspaces/harbor/catalog")
        no_guide = await http.get("/v1/catalog/guide")
    async with http_client(app, None) as http:
        anonymous = await http.get("/v1/catalog/guide")
    untouched = await digests(world.db, *CATALOG_TABLES)
    async with http_client(app, both) as http:
        written = await http.put(project_url(), json=PORT)
    async with mcp_client(app, both) as client:
        with_ingest = [tool.name for tool in (await client.list_tools()).tools]
        again = ok(await put(client))

    assert {"get_catalog", "get_domain", "catalog_guide"} <= set(listed)
    assert "put_project" not in listed
    assert "put_project" in with_ingest
    assert error(read_only, 403, "insufficient_scope") == "The token lacks the ingest scope."
    assert error(push_only, 403, "insufficient_scope") == "The token lacks the read scope."
    assert 'scope="read"' in push_only.headers["WWW-Authenticate"]
    assert error(no_catalog, 403, "insufficient_scope")
    assert error(no_guide, 403, "insufficient_scope")
    assert error(anonymous, 401, "unauthorized")
    assert untouched["project"][0] == 0
    # The guide is the service's own text: the same over both, and no source's.
    assert ok(guide) == {"guide": CATALOG_GUIDE}
    assert text(guide) == CATALOG_GUIDE
    assert body(guide_over_http) == {"guide": CATALOG_GUIDE}
    # HTTP and MCP write the same and answer the same shape.
    made = body(written)
    assert (made["created"], made["changed"], made["version"]) == (True, True, 1)
    assert {key: value for key, value in again.items() if key not in ("created", "changed")} == {
        key: value for key, value in made.items() if key not in ("created", "changed")
    }
    assert (again["created"], again["changed"]) == (False, False)


# --- One matcher ---------------------------------------------------------------------------------

MASKS: list[tuple[str, str]] = [
    ("", "*.py"),
    ("", "src/**"),
    ("", "src/**/test_*.py"),
    ("", "docs"),
    ("", "docs/"),
    ("", "/a.b"),
    ("", "a?c"),
    ("", "wäl/*"),
    ("", "a$b"),
    ("", "we(ird)[1]/{x}+|^"),
    ("", "x/🌱"),
    ("", "**"),
    ("", "**/b"),
    ("", "a/**/b"),
    ("", "a**b"),
    ("svc/api", "*.py"),
    ("svc/api", "src/*"),
    ("svc/api", "/src"),
    ("svc/api", "**"),
    ("svc/api", "**/deep/*.py"),
    ("a*b", "x"),
    ("r.s", "x/**/y"),
    ("sp ace", "f?le"),
]
PATHS: list[str] = [
    "a.py",
    "src/a.py",
    "src/a.py\n",
    "src/a\nb.py",
    "a\n/b.py",
    "src",
    "src/x/y/test_a.py",
    "src/test_a.py",
    "lib/src/test_a.py",
    "docs/a.md",
    "x/docs/a.md",
    "docsx/a.md",
    "a.b",
    "aXb",
    "x/a.b",
    "abc",
    "a/c",
    "a/b",
    "a/x/y/b",
    "aXXb",
    "a/Xb",
    "b",
    "wäl/x",
    "wal/x",
    "a$b",
    "a",
    "we(ird)[1]/{x}+|^",
    "weird1/x",
    "x/🌱",
    "x/🌱/y",
    "svc/api",
    "svc/api/a.py",
    "svc/api/x/a.py",
    "svc/api/x/deep/a.py",
    "svc/api-old/a.py",
    "svc/apis/a.py",
    "svc/api/src/a",
    "svc/api/src/a/b",
    "svc/api/lib/src/a",
    "a*b/x",
    "aXb/x",
    "a*b/y/x/z",
    "r.s/x/y",
    "r.s/x/q/w/y",
    "rXs/x/y",
    "sp ace/file",
    "sp ace/deep/fxle/more",
    "space/file",
    "",
    "/",
    " ",
    "\n",
]


async def test_postgres_and_python_agree_on_every_mask_and_path(empty_database_url: str) -> None:
    """The search filter is evaluated by Postgres and a hit's context by Python, from one
    expression: on no mask and no path may they differ."""
    expressions = [glob_regex(mask, root=root) for root, mask in MASKS]
    expressions += [directory_regex(root) for root in sorted({root for root, _ in MASKS if root})]
    expressions.append(globs_regex(("*.py", "src/**", "docs"), root="svc/api"))
    expressions.append(globs_regex(tuple(mask for root, mask in MASKS if not root)))
    matched = 0
    async with connect(empty_database_url, autocommit=True) as db:
        for expression in expressions:
            cur = await db.execute(
                "SELECT p.path ~ %s FROM unnest(%s::text[]) WITH ORDINALITY AS p (path, n)"
                " ORDER BY p.n",
                (expression, PATHS),
            )
            by_postgres = [row[0] for row in await cur.fetchall()]
            matcher = path_matcher(expression)
            by_python = [matcher(path) for path in PATHS]
            assert by_python == by_postgres, expression
            matched += sum(by_postgres)

    # The table is not one of paths nothing matches: both sides had something to disagree on.
    assert matched > 100
    assert matched < len(expressions) * len(PATHS) - 100


# --- What the database itself refuses ------------------------------------------------------------


async def test_the_database_keeps_two_levels_one_workspace_and_git_sources(
    migrated_database_url: str,
) -> None:
    async with connect(migrated_database_url, autocommit=True) as db:
        owner = await ensure_principal(db, "owner")
        access = WriteAccess.for_owner(owner)
        here = (await create_workspace(db, owner_id=owner, name="here")).id
        there = (await create_workspace(db, owner_id=owner, name="there")).id
        pushed = await create_push_source(db, access, "here", NewPushSource(name="chat"))

        async def one(query: LiteralString, *params: Any) -> Any:
            row = await (await db.execute(query, params)).fetchone()
            assert row is not None
            return row[0]

        async def project(workspace: uuid.UUID, name: str) -> uuid.UUID:
            found: uuid.UUID = await one(
                "INSERT INTO project (workspace_id, owner_id, name, updated_by)"
                " VALUES (%s, %s, %s, %s) RETURNING id",
                workspace,
                owner,
                name,
                owner,
            )
            return found

        async def git_source(workspace: uuid.UUID, name: str) -> uuid.UUID:
            found: uuid.UUID = await one(
                "INSERT INTO source (workspace_id, owner_id, name, kind)"
                " VALUES (%s, %s, %s, 'repository') RETURNING id",
                workspace,
                owner,
                name,
            )
            return found

        async def service(
            workspace: uuid.UUID, of: uuid.UUID, name: str, source: uuid.UUID, root: str = ""
        ) -> uuid.UUID:
            found: uuid.UUID = await one(
                "INSERT INTO service (workspace_id, owner_id, project_id, name, source_id, root,"
                " updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                workspace,
                owner,
                of,
                name,
                source,
                root,
                owner,
            )
            return found

        async def domain(
            workspace: uuid.UUID,
            of: uuid.UUID,
            name: str,
            parent: uuid.UUID | None = None,
            level: int | None = None,
        ) -> uuid.UUID:
            found: uuid.UUID = await one(
                "INSERT INTO domain (workspace_id, owner_id, project_id, name, level, parent_id,"
                " parent_level, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                workspace,
                owner,
                of,
                name,
                level if level is not None else (1 if parent is None else 2),
                parent,
                None if parent is None else 1,
                owner,
            )
            return found

        async def code(
            workspace: uuid.UUID, of: uuid.UUID, where: uuid.UUID, paths: list[str]
        ) -> None:
            await db.execute(
                "INSERT INTO domain_code (workspace_id, owner_id, domain_id, service_id, paths,"
                " updated_by) VALUES (%s, %s, %s, %s, %s, %s)",
                (workspace, owner, of, where, paths, owner),
            )

        port = await project(here, "port")
        lights = await project(here, "lights")
        far = await project(there, "far")
        repository = await git_source(here, "quay")
        elsewhere = await git_source(there, "quay")
        quay = await service(here, port, "quay", repository)
        fees = await domain(here, port, "fees")
        berth = await domain(here, port, "berth", parent=fees)
        await code(here, berth, quay, ["quay/fees/**"])

        # Two levels and no more: a subdomain has no subdomain, whatever level it claims.
        with pytest.raises(errors.ForeignKeyViolation):
            await domain(here, port, "deep", parent=berth)
        with pytest.raises(errors.CheckViolation):
            await domain(here, port, "deep", parent=berth, level=3)
        with pytest.raises(errors.CheckViolation):
            await domain(here, port, "loose", level=2)
        # A domain with subdomains cannot become a subdomain itself.
        other_domain = await domain(here, port, "water")
        with pytest.raises(errors.ForeignKeyViolation):
            await db.execute(
                "UPDATE domain SET level = 2, parent_id = %s, parent_level = 1 WHERE id = %s",
                (other_domain, fees),
            )
        # A parent is of the same project.
        with pytest.raises(errors.ForeignKeyViolation):
            await domain(here, lights, "stray", parent=fees)
        # A name is one among the domains of a project, and among the subdomains of a domain.
        with pytest.raises(errors.UniqueViolation):
            await domain(here, port, "fees")
        with pytest.raises(errors.UniqueViolation):
            await domain(here, port, "berth", parent=fees)
        await domain(here, port, "berth")  # as a domain of its own it is another name
        await domain(here, lights, "fees")

        # A domain names a service of another project of its workspace, never of another one.
        signals = await domain(here, lights, "signals")
        await code(here, signals, quay, ["quay/tides.py"])
        distant = await service(there, far, "quay", elsewhere)
        with pytest.raises(errors.ForeignKeyViolation):
            await code(here, signals, distant, ["**"])
        with pytest.raises(errors.ForeignKeyViolation):
            await code(there, signals, distant, ["**"])
        # A service is over a git source of its own workspace.
        with pytest.raises(errors.ForeignKeyViolation):
            await service(here, port, "chat", pushed.id)
        with pytest.raises(errors.ForeignKeyViolation):
            await service(here, port, "far", elsewhere)
        with pytest.raises(errors.CheckViolation):
            await service(here, port, "up", repository, "../up")
        with pytest.raises(errors.CheckViolation):
            await service(here, port, "abs", repository, "/abs")
        with pytest.raises(errors.CheckViolation):
            await db.execute("UPDATE domain_code SET paths = '{}'")
        # A directory of a repository is one service; the check waits for the commit, so one
        # write may move two services into each other's place.
        web = await service(here, port, "web", repository, "web")
        async with db.transaction():
            await db.execute("UPDATE service SET root = 'web' WHERE id = %s", (quay,))
            await db.execute("UPDATE service SET root = '' WHERE id = %s", (web,))
        with pytest.raises(errors.UniqueViolation):
            await service(here, lights, "again", repository, "web")

        # The catalog goes with its workspace, and a service with its repository.
        await db.execute("DELETE FROM source WHERE id = %s", (repository,))
        assert await one("SELECT count(*) FROM service WHERE workspace_id = %s", here) == 0
        assert await one("SELECT count(*) FROM domain_code WHERE workspace_id = %s", here) == 0
        assert await one("SELECT count(*) FROM domain WHERE workspace_id = %s", here) == 6
        await db.execute("DELETE FROM workspace WHERE id = %s", (here,))
        assert await one("SELECT count(*) FROM project") == 1
        assert await one("SELECT count(*) FROM domain") == 0
        assert await one("SELECT count(*) FROM service") == 1
