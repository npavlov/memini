"""list_commits through the MCP endpoint and the HTTP API, against a real Postgres and real git
mirrors: who changed what, read from the mirror on demand.

Acceptance of memini-qpk.4: on synthetic repositories, the commits for one file, for a service (a
repository narrowed by path masks) and for a subdomain that spans two repositories come back
newest first; the author and period filters work; a path outside the token's workspace returns
nothing; a walk past the time limit is reported as a timeout; no table gains rows. Repositories
are made here and synced as the git_sync job syncs them; authors, messages and files are invented.
"""

import dataclasses
import shutil
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, TextContent
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import SecretStr

from git_helpers import SourceRepo, stamp
from memini.api import create_app
from memini.auth import Principal, ReadFilter, Scope, WriteAccess, authorization, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, run_git_sync
from memini.jobs import Job
from memini.mcp import MCP_PATH
from memini.mcp.text import TEXT_NOTICE
from memini.persons import create_person, merge_person
from memini.query import (
    CommitLimits,
    CommitScope,
    CommitsTimeoutError,
    QueryServices,
    list_commits,
)
from memini.sources import NewGitSource, Source, create_git_source
from memini.workspaces import create_workspace, ensure_principal
from redaction_helpers import GITHUB

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
ANN, BO, CY, DEE = "Ann Example", "Bo Example", "Cy Example", "Dee Example"
SECOND = timedelta(seconds=1)


def day(number: int) -> datetime:
    return datetime(2026, 3, number, 10, tzinfo=UTC)


@dataclass
class World:
    pool: Pool
    db: Conn
    settings: Settings
    owner: uuid.UUID
    acme: uuid.UUID
    other: uuid.UUID
    sources: dict[str, Source]
    """acme's billing, gateway and pending, and other's billing as ``vault``."""
    commits: dict[str, str]
    """Commit ids by label: b1 to b5 (billing), g1 to g3 (gateway), v1 (other's billing)."""
    persons: dict[str, uuid.UUID]
    """Canonical persons of acme by label: ann, robert."""
    embedder: FakeEmbedder

    def app(self, *, limits: CommitLimits | None = None, git: Path | None = None) -> FastAPI:
        """The application; with other bounds for list_commits, or another git executable."""
        app = create_app(self.settings, pool=self.pool, embedder=self.embedder)
        services: QueryServices = app.state.services
        if limits is not None:
            services.commit_limits = limits
        if git is not None:
            options = GitOptions.from_settings(self.settings)
            services.mirrors = MirrorManager(dataclasses.replace(options, git=str(git)))
        return app

    async def token(self, *scopes: Scope, workspace: uuid.UUID | None = None) -> str:
        new = await create_token(
            self.db,
            owner_id=self.owner,
            name="test agent",
            scopes=scopes or ("read",),
            workspace_id=workspace,
        )
        return new.secret.get_secret_value()

    def mirrors(self) -> MirrorManager:
        return MirrorManager(GitOptions.from_settings(self.settings))

    def reader(self, workspace: uuid.UUID | None = None) -> ReadFilter:
        """What a read token of the owner may read, without a token in the database."""
        return authorization.authorize_read(
            Principal(self.owner, uuid.uuid4(), frozenset({"read"}), workspace)
        )


async def add_source(
    db: Conn, owner: uuid.UUID, workspace: str, name: str, repo: SourceRepo, settings: Settings
) -> Source:
    return await create_git_source(
        db,
        WriteAccess.for_owner(owner),
        workspace,
        NewGitSource(name=name, location=str(repo.path), refs=["main"]),
        options=GitOptions.from_settings(settings),
    )


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


def billing_repo(path: Path) -> tuple[SourceRepo, dict[str, str]]:
    """The billing service: fees under src/fees, invoices beside them."""
    repo = SourceRepo(path)
    commits = {
        "b1": repo.commit(
            {
                "README.md": "# Billing\n",
                "src/fees/calc.py": "def fee(amount):\n    return amount * 2 // 100\n",
            },
            "Add the fee calculation\n\nFees are a flat rate for now.",
            env=stamp(ANN, day(1), authored=day(1) - timedelta(hours=2)),
        ),
        "b2": repo.commit(
            {"src/fees/calc.py": "def fee(amount):\n    return round(amount * 0.02, 2)\n"},
            "Round fees to cents\n\nTicket BILL-12 asked for it.\n"
            f"The deploy token {GITHUB} was rotated the same day.",
            env=stamp(BO, day(3)),
        ),
        "b3": repo.commit(
            {"src/invoices/model.py": "class Invoice:\n    total = 0\n"},
            "Add invoices",
            env=stamp(ANN, day(5)),
        ),
        "b4": repo.commit(
            {"src/fees/table.py": "PARTNERS = {}\n", "docs/fees.md": "# Partner fees\n"},
            "Add the partner fee table",
            env=stamp(CY, day(7)),
        ),
        "b5": repo.commit({"README.md": None}, "Drop the readme", env=stamp(BO, day(9))),
    }
    return repo, commits


def gateway_repo(path: Path) -> tuple[SourceRepo, dict[str, str]]:
    """The gateway: it applies fees too, under lib/fees."""
    repo = SourceRepo(path)
    commits = {
        "g1": repo.commit(
            {"lib/router.rb": "class Router\nend\n", "lib/fees/apply.rb": "def apply\nend\n"},
            "Add the gateway",
            env=stamp(CY, day(2)),
        ),
        "g2": repo.commit(
            {"lib/fees/apply.rb": "def apply(order)\n  order\nend\n"},
            "Apply fees at checkout",
            env=stamp(ANN, day(4)),
        ),
        "g3": repo.commit(
            {"lib/router.rb": "class Router\n  RETRIES = 2\nend\n"},
            "Retry on a timeout",
            env=stamp(BO, day(8)),
        ),
    }
    return repo, commits


@pytest.fixture
async def world(migrated_database_url: str, tmp_path: Path) -> AsyncIterator[World]:
    """acme/billing and acme/gateway, synced; acme/pending, never synced; other/billing, another
    repository of the same name. After the sync billing gets a commit on main and a branch the
    source does not track, both fetched into the mirror: neither is part of its history here."""
    repos = tmp_path / "repos"
    settings = Settings(
        database_url=SecretStr(migrated_database_url),
        git_mirror_dir=tmp_path / "mirrors",
        git_local_roots=(repos,),
    )
    billing, commits = billing_repo(repos / "billing")
    gateway, gateway_commits = gateway_repo(repos / "gateway")
    vault = SourceRepo(repos / "vault")
    commits |= gateway_commits
    commits["v1"] = vault.commit(
        {"src/fees/calc.py": "def fee(amount):\n    return 1\n"},
        "Add vault fees",
        env=stamp(DEE, day(6)),
    )
    pending = SourceRepo(repos / "pending")
    pending.commit({"a.py": "x = 1\n"}, "Pending")

    embedder = FakeEmbedder()
    async with (
        open_pool(migrated_database_url, min_size=1, max_size=8, timeout=5) as pool,
        connect(migrated_database_url, autocommit=True) as db,
    ):
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        other = await create_workspace(db, owner_id=owner, name="other")
        sources = {
            "billing": await add_source(db, owner, "acme", "billing", billing, settings),
            "gateway": await add_source(db, owner, "acme", "gateway", gateway, settings),
            "pending": await add_source(db, owner, "acme", "pending", pending, settings),
            "vault": await add_source(db, owner, "other", "billing", vault, settings),
        }
        for name in ("billing", "gateway", "vault"):
            await sync(pool, settings, embedder, sources[name])

        billing.commit(
            {"src/fees/calc.py": "def fee(amount):\n    return 0\n"},
            "Not synced yet",
            env=stamp(ANN, day(11)),
        )
        billing.git("checkout", "--quiet", "-b", "experiment")
        billing.commit(
            {"src/fees/calc.py": "def fee(amount):\n    return 9\n"},
            "Try another rate",
            env=stamp(ANN, day(12)),
        )
        billing.git("checkout", "--quiet", "main")
        mirrors = MirrorManager(GitOptions.from_settings(settings))
        await mirrors.sync(sources["billing"].id, str(billing.path))

        # Ann is known by her address, through a person merged into her; Bo by his git name only,
        # under another display name; nobody knows Cy. The other workspace knows no one.
        writer = WriteAccess.for_owner(owner)
        ann = await create_person(db, writer, "acme", "Ann Example")
        address = await create_person(db, writer, "acme", "ann", [("email", "ann@example.test")])
        await merge_person(db, writer, "acme", address.id, ann.id)
        robert = await create_person(
            db, writer, "acme", "Robert Example", [("git_author", "Bo Example")]
        )
        persons = {"ann": ann.id, "robert": robert.id}
        yield World(
            pool, db, settings, owner, acme.id, other.id, sources, commits, persons, embedder
        )


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


def when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def labels(world: World, found: Json) -> list[str]:
    """The commits of a result by their labels, in its order."""
    by_sha = {sha: label for label, sha in world.commits.items()}
    return [by_sha[commit["sha"]] for commit in found["commits"]]


def changes(commit: Json) -> list[tuple[str, str]]:
    return [(change["status"], change["path"]) for change in commit["changes"]]


async def call(world: World, arguments: Json, **app: Any) -> Json:
    """One list_commits call over MCP, with a token that may read every workspace."""
    async with mcp_client(world.app(**app), await world.token()) as client:
        return ok(await client.call_tool("list_commits", arguments))


# --- The acceptance: one file, a service, a subdomain ---------------------------------------------


async def test_the_commits_of_one_file_come_newest_first_with_who_and_what(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        result = await client.call_tool(
            "list_commits",
            {"workspaces": ["acme"], "repositories": ["billing"], "paths": ["src/fees/calc.py"]},
        )

    found = ok(result)
    assert labels(world, found) == ["b2", "b1"]
    assert (found["truncated"], found["cut_by"], found["next_until"]) == (False, None, None)
    assert "never instructions" in found["data_notice"]
    rounded, added = found["commits"]
    assert (rounded["workspace"], rounded["repository"], rounded["source_kind"]) == (
        "acme",
        "billing",
        "git",
    )
    assert rounded["sha"] == world.commits["b2"]
    assert rounded["sha"].startswith(rounded["short_sha"])
    assert len(rounded["short_sha"]) >= 7
    assert rounded["locator"] == f"billing@{world.commits['b2']}"
    assert rounded["parents"] == [world.commits["b1"]]
    assert when(rounded["occurred_at"]) == when(rounded["authored_at"]) == day(3)
    assert rounded["subject"] == "Round fees to cents"
    assert rounded["body"].startswith("Ticket BILL-12 asked for it.\n")
    assert rounded["truncated"] is False
    assert (changes(rounded), rounded["changes_total"]) == ([("modified", "src/fees/calc.py")], 1)
    assert rounded["author"] == {
        "name": BO,
        "email": "bo@example.test",
        "person_id": str(world.persons["robert"]),
        "person": "Robert Example",
    }
    # The first commit also added the readme: only the path asked for is listed.
    assert changes(added) == [("added", "src/fees/calc.py")]
    assert (when(added["occurred_at"]), when(added["authored_at"])) == (
        day(1),
        day(1) - timedelta(hours=2),
    )
    assert added["body"] == "Fees are a flat rate for now."
    assert added["parents"] == []
    assert added["author"]["person_id"] == str(world.persons["ann"])  # through the merge
    assert added["author"]["person"] == "Ann Example"
    assert found["repositories"] == [
        {
            "workspace": "acme",
            "repository": "billing",
            "heads": [{"ref": MAIN, "commit": world.commits["b5"]}],
            "complete": True,
        }
    ]

    summary = text(result)
    assert summary.startswith(f"2 commits. {TEXT_NOTICE}\n\n1. billing@{world.commits['b2']}\n")
    assert f"author is person {world.persons['robert']}" in summary
    assert "Author: Bo Example <bo@example.test>\n\nRound fees to cents\n" in summary
    assert "modified src/fees/calc.py\n```" in summary


async def test_the_commits_of_a_service_are_those_of_its_path_masks(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        service = ok(
            await client.call_tool(
                "list_commits",
                {"workspaces": ["acme"], "repositories": ["billing"], "paths": ["src/fees/**"]},
            )
        )
        directory = ok(
            await client.call_tool(
                "list_commits",
                {"workspaces": ["acme"], "repositories": ["billing"], "paths": ["src/fees/"]},
            )
        )
        anywhere = ok(
            await client.call_tool(
                "list_commits", {"workspaces": ["acme"], "paths": ["*.md", "invoices"]}
            )
        )
        nothing = ok(
            await client.call_tool("list_commits", {"paths": ["src/fees/../../etc/passwd"]})
        )

    assert labels(world, service) == ["b4", "b2", "b1"]
    table = service["commits"][0]
    # The commit also added docs/fees.md, which is not the service's.
    assert (changes(table), table["changes_total"]) == ([("added", "src/fees/table.py")], 1)
    assert (table["author"]["name"], table["author"]["person_id"]) == (CY, None)
    assert labels(world, directory) == ["b4", "b2", "b1"]
    # A mask without a directory in front matches at any depth: the whole history is read.
    assert labels(world, anywhere) == ["b5", "b4", "b3", "b1"]
    assert [changes(commit) for commit in anywhere["commits"]] == [
        [("deleted", "README.md")],
        [("added", "docs/fees.md")],
        [("added", "src/invoices/model.py")],
        [("added", "README.md")],
    ]
    assert nothing["commits"] == []  # no path of a repository has a ".." in it


async def test_a_subdomain_that_spans_two_repositories_is_one_list(world: World) -> None:
    """The seam for the catalog: a scope is a source with its own path masks."""
    scopes = [
        CommitScope(world.sources["billing"].id, ("src/fees",)),
        CommitScope(world.sources["gateway"].id, ("lib/fees/**",)),
    ]

    found = (await list_commits(world.db, world.reader(), world.mirrors(), scopes)).model_dump(
        mode="json"
    )
    whole = await list_commits(
        world.db,
        world.reader(),
        world.mirrors(),
        [*scopes, CommitScope(world.sources["gateway"].id)],  # and all of the gateway
        since=day(4),
    )
    async with mcp_client(world.app(), await world.token()) as client:
        by_names = ok(
            await client.call_tool(
                "list_commits",
                {
                    "workspaces": ["acme"],
                    "repositories": ["billing", "gateway"],
                    "paths": ["src/fees/**", "lib/fees/**"],
                },
            )
        )

    assert labels(world, found) == ["b4", "g2", "b2", "g1", "b1"]
    assert [(c["repository"], when(c["occurred_at"])) for c in found["commits"]] == [
        ("billing", day(7)),
        ("gateway", day(4)),
        ("billing", day(3)),
        ("gateway", day(2)),
        ("billing", day(1)),
    ]
    assert changes(found["items"][3]) == [("added", "lib/fees/apply.rb")]  # not the router
    assert [walk["source"] for walk in found["sources"]] == ["billing", "gateway"]
    assert [c.subject for c in whole.items] == [
        "Retry on a timeout",
        "Add the partner fee table",
        "Apply fees at checkout",
    ]
    assert by_names == found


async def test_without_filters_every_readable_repository_is_listed(world: World) -> None:
    found = await call(world, {})
    acme = await call(world, {"workspaces": ["acme"]})

    assert labels(world, found) == ["b5", "g3", "b4", "v1", "b3", "g2", "b2", "g1", "b1"]
    assert labels(world, acme) == ["b5", "g3", "b4", "b3", "g2", "b2", "g1", "b1"]
    vault = found["commits"][3]
    assert (vault["workspace"], vault["repository"]) == ("other", "billing")
    assert vault["author"] == {
        "name": DEE,
        "email": "dee@example.test",
        "person_id": None,
        "person": None,
    }
    assert [(w["workspace"], w["repository"]) for w in found["repositories"]] == [
        ("acme", "billing"),
        ("acme", "gateway"),
        ("acme", "pending"),
        ("other", "billing"),
    ]


# --- The pinned commit ----------------------------------------------------------------------------


async def test_history_starts_at_the_pinned_commit_not_at_what_the_mirror_holds(
    world: World,
) -> None:
    mirrors = world.mirrors()
    mirror = mirrors.open(world.sources["billing"].id)
    fetched = await mirrors.log(mirror, list((await mirrors.refs(mirror)).values()), max_commits=50)
    async with mcp_client(world.app(), await world.token()) as client:
        result = await client.call_tool("list_commits", {"workspaces": ["acme"]})

    # The mirror holds a later commit of main and a branch the source does not track.
    assert {"Not synced yet", "Try another rate"} <= {c.subject for c in fetched.commits}
    found = ok(result)
    assert {c["subject"] for c in found["commits"]}.isdisjoint(
        {"Not synced yet", "Try another rate", "Pending"}
    )
    billing, gateway, pending = found["repositories"]
    assert billing["heads"] == [{"ref": MAIN, "commit": world.commits["b5"]}]
    assert gateway["heads"] == [{"ref": MAIN, "commit": world.commits["g3"]}]
    assert pending == {
        "workspace": "acme",
        "repository": "pending",
        "heads": [],
        "complete": True,
    }
    assert "No synced commit yet, so nothing was read of: acme/pending." in text(result)


# --- Filters --------------------------------------------------------------------------------------


async def test_the_author_filter_matches_the_name_or_the_address(world: World) -> None:
    by_address = await call(world, {"workspaces": ["acme"], "author": "bo@example.test"})
    by_name = await call(world, {"author": "CY EXAMPLE"})
    in_one_file = await call(world, {"author": "ann", "paths": ["src/fees/calc.py"]})
    nobody = await call(world, {"author": "Eve Example"})

    assert labels(world, by_address) == ["b5", "g3", "b2"]
    assert {c["author"]["person"] for c in by_address["commits"]} == {"Robert Example"}
    assert labels(world, by_name) == ["b4", "g1"]
    assert labels(world, in_one_file) == ["b1"]
    assert (nobody["commits"], nobody["truncated"]) == ([], False)


async def test_the_period_is_since_inclusive_and_until_exclusive(world: World) -> None:
    window = await call(
        world,
        {"workspaces": ["acme"], "since": day(3).isoformat(), "until": day(7).isoformat()},
    )
    since = await call(world, {"workspaces": ["acme"], "since": (day(8) + SECOND).isoformat()})
    until = await call(world, {"workspaces": ["acme"], "until": "2026-03-02T11:00:00+01:00"})
    quarter = await call(
        world,
        {
            "repositories": ["billing"],
            "workspaces": ["acme"],
            "paths": ["src/fees/**"],
            "author": "example.test",
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-04-01T00:00:00Z",
        },
    )

    assert labels(world, window) == ["b3", "g2", "b2"]
    assert labels(world, since) == ["b5"]
    assert labels(world, until) == ["b1"]  # 10:00 UTC, the time of g1, is not before itself
    assert labels(world, quarter) == ["b4", "b2", "b1"]  # "who changed fees this quarter?"


async def test_text_is_looked_for_in_the_message_as_it_is_returned(world: World) -> None:
    subject = await call(world, {"workspaces": ["acme"], "text": "CENTS"})
    in_body = await call(world, {"text": "bill-12"})
    several = await call(world, {"workspaces": ["acme"], "text": "fee"})
    secret = await call(world, {"text": GITHUB[:14]})
    marker = await call(world, {"text": "rotated the same day"})

    assert labels(world, subject) == ["b2"]
    assert labels(world, in_body) == ["b2"]
    assert labels(world, several) == ["b4", "g2", "b2", "b1"]
    # The message held a token: it is redacted, and what was redacted cannot be searched for.
    assert secret["commits"] == []
    (rounded,) = marker["commits"]
    assert rounded["body"] == (
        "Ticket BILL-12 asked for it.\n"
        "The deploy token [REDACTED:github_token] was rotated the same day."
    )
    assert GITHUB not in str(marker)


# --- Access ---------------------------------------------------------------------------------------


async def test_a_path_outside_the_tokens_workspace_returns_nothing(world: World) -> None:
    other = await world.token("read", workspace=world.other)
    acme = await world.token("read", workspace=world.acme)
    async with mcp_client(world.app(), other) as client:
        own = ok(await client.call_tool("list_commits", {}))
        named = ok(await client.call_tool("list_commits", {"repositories": ["gateway"]}))
        by_workspace = ok(await client.call_tool("list_commits", {"workspaces": ["acme"]}))
        by_path = ok(await client.call_tool("list_commits", {"paths": ["lib/fees/**"]}))
        same_name = ok(await client.call_tool("list_commits", {"repositories": ["billing"]}))
    async with http_client(world.app(), other) as http:
        over_http = body(await http.get("/v1/commits", params={"workspaces": ["acme"]}))
    async with mcp_client(world.app(), acme) as client:
        theirs = ok(await client.call_tool("list_commits", {"repositories": ["billing"]}))
    # The library keeps the filter whoever names the source: a scope is no way around it.
    seam = await list_commits(
        world.db,
        world.reader(world.other),
        world.mirrors(),
        [CommitScope(world.sources["gateway"].id), CommitScope(world.sources["billing"].id)],
    )

    assert labels(world, own) == ["v1"]
    for nothing in (named, by_workspace, by_path, over_http):
        assert (nothing["commits"], nothing["truncated"]) == ([], False)
    for unseen in (named, by_workspace, over_http):
        assert unseen["repositories"] == []  # not even that such a repository exists
    assert labels(world, same_name) == ["v1"]
    assert labels(world, theirs) == ["b5", "b4", "b3", "b2", "b1"]
    assert (seam.items, seam.sources) == ([], [])


async def test_list_commits_reads_through_the_authorization_filter(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Principal] = []
    original = authorization.authorize_read

    def watched(principal: Principal, **kwargs: Any) -> ReadFilter:
        calls.append(principal)
        return original(principal, **kwargs)

    monkeypatch.setattr(authorization, "authorize_read", watched)
    reader = await world.token("read", workspace=world.acme)
    writer = await world.token("ingest", "admin")
    async with mcp_client(world.app(), reader) as client:
        ok(await client.call_tool("list_commits", {}))
    async with http_client(world.app(), reader) as http:
        body(await http.get("/v1/commits"))
    async with http_client(world.app(), writer) as http:
        denied = await http.get("/v1/commits")

    assert [principal.workspace_id for principal in calls] == [world.acme, world.acme]
    assert error(denied, 403, "insufficient_scope")


# --- Bounds ---------------------------------------------------------------------------------------


async def test_a_list_cut_at_its_limit_says_how_to_continue(world: World) -> None:
    pages: list[Json] = []
    arguments: Json = {"workspaces": ["acme"], "limit": 3}
    async with mcp_client(world.app(), await world.token()) as client:
        while True:
            result = await client.call_tool("list_commits", arguments)
            pages.append(ok(result))
            if not pages[-1]["truncated"]:
                break
            assert f"call again with until {pages[-1]['next_until']}" in text(result).replace(
                "+00:00", "Z"
            )
            arguments = {**arguments, "until": pages[-1]["next_until"]}

    assert [labels(world, page) for page in pages] == [
        ["b5", "g3", "b4"],
        ["b3", "g2", "b2"],
        ["g1", "b1"],
    ]
    assert [page["cut_by"] for page in pages] == ["limit", "limit", None]
    assert [page["next_until"] and when(page["next_until"]) for page in pages] == [
        day(7),
        day(3),
        None,
    ]
    first = {walk["repository"]: walk["complete"] for walk in pages[0]["repositories"]}
    assert first == {"billing": False, "gateway": True, "pending": True}


async def test_the_service_returns_no_more_than_its_own_limit(world: World) -> None:
    found = await call(
        world, {"workspaces": ["acme"], "limit": 500}, limits=CommitLimits(max_commits=2)
    )
    async with http_client(world.app(), await world.token()) as http:
        too_many = await http.get("/v1/commits", params={"limit": 1001})

    assert labels(world, found) == ["b5", "g3"]
    assert (found["truncated"], found["cut_by"], when(found["next_until"])) == (
        True,
        "limit",
        day(8),
    )
    assert error(too_many, 422, "validation_error") == "The request is not valid."


async def test_a_walk_stopped_at_its_limit_returns_what_it_found_and_continues(
    world: World,
) -> None:
    """Two commits of a repository are read per call, and Ann wrote the third and the fifth."""
    limits = CommitLimits(max_walk=2)
    arguments: Json = {"workspaces": ["acme"], "repositories": ["billing"], "author": "ann"}
    pages: list[Json] = []
    async with mcp_client(world.app(limits=limits), await world.token()) as client:
        while True:
            result = await client.call_tool("list_commits", arguments)
            pages.append(ok(result))
            if not pages[-1]["truncated"]:
                break
            arguments = {**arguments, "until": pages[-1]["next_until"]}
        first = await client.call_tool("list_commits", {"workspaces": ["acme"], "author": "ann"})

    assert [labels(world, page) for page in pages] == [[], ["b3"], ["b1"]]
    assert [page["cut_by"] for page in pages] == ["walk_limit", "walk_limit", None]
    assert [page["next_until"] and when(page["next_until"]) for page in pages] == [
        day(5) + SECOND,
        day(1) + SECOND,
        None,
    ]
    # Billing was read only down to its third commit: what the gateway holds from before that
    # waits for the next call too, so that the list stays newest first across calls.
    assert labels(world, ok(first)) == []
    assert when(ok(first)["next_until"]) == day(5) + SECOND
    assert text(first).startswith(
        "0 commits.\n\nThe service read as many commits of a repository as it reads in one call"
    )


def slow_git(directory: Path) -> Path:
    """A git whose ``log`` never answers; every other command is git's."""
    script = directory / "slow-git"
    script.write_text(
        '#!/bin/sh\nfor arg in "$@"; do\n  if [ "$arg" = log ]; then exec sleep 60; fi\ndone\n'
        'exec git "$@"\n'
    )
    script.chmod(0o755)
    return script


async def test_a_walk_past_the_time_limit_is_reported_as_a_timeout(
    world: World, tmp_path: Path
) -> None:
    app = world.app(limits=CommitLimits(timeout=0.3), git=slow_git(tmp_path))
    message = (
        "The history walk was stopped after 0.3 s (git_log_timeout); narrow it with"
        " repositories, paths, since or until."
    )
    async with mcp_client(app, await world.token()) as client:
        over_mcp = await client.call_tool("list_commits", {"repositories": ["billing"]})
    async with http_client(app, await world.token()) as http:
        over_http = await http.get("/v1/commits", params={"repositories": ["billing"]})
        unsynced = await http.get("/v1/commits", params={"repositories": ["pending"]})

    assert refused(over_mcp) == message
    assert error(over_http, 422, "commits_timeout") == message
    assert body(unsynced)["commits"] == []  # nothing to walk, so nothing to wait for
    with pytest.raises(CommitsTimeoutError):
        await list_commits(
            world.db,
            world.reader(),
            MirrorManager(
                dataclasses.replace(
                    GitOptions.from_settings(world.settings), git=str(slow_git(tmp_path))
                )
            ),
            [CommitScope(world.sources["billing"].id)],
            limits=CommitLimits(timeout=0.2),
        )


# --- Arguments, HTTP, and what is written ---------------------------------------------------------


async def test_arguments_that_are_not_valid_are_refused_without_being_repeated(
    world: World,
) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        bad_glob = await client.call_tool("list_commits", {"paths": ["a\nb"]})
        no_repository = await client.call_tool(
            "list_commits", {"repositories": ["nowhere"], "paths": ["a\nb"]}
        )
        backwards = await client.call_tool(
            "list_commits", {"since": day(5).isoformat(), "until": day(5).isoformat()}
        )
        naive = await client.call_tool("list_commits", {"since": "2026-03-01T00:00"})
        blank = await client.call_tool("list_commits", {"author": "   "})
        catalog = await client.call_tool("list_commits", {"project": "billing"})
        zero = await client.call_tool("list_commits", {"limit": 0})

    tool = tools["list_commits"]
    assert set(tool.input_schema["properties"]) == {
        "repositories",
        "workspaces",
        "paths",
        "author",
        "since",
        "until",
        "text",
        "limit",
    }
    assert "required" not in tool.input_schema
    assert tool.input_schema["additionalProperties"] is False
    assert tool.output_schema is not None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert refused(bad_glob) == "A path glob holds a NUL or a line break."
    assert refused(no_repository) == "A path glob holds a NUL or a line break."
    assert refused(backwards) == "Since must be before until."
    assert refused(naive) == "Invalid arguments: since: timezone_aware."
    assert refused(blank) == "The author filter is empty."
    assert refused(catalog) == "Invalid arguments: project: extra_forbidden."
    assert refused(zero) == "Invalid arguments: limit: greater_than_equal."


async def test_the_http_route_mirrors_the_tool(world: World) -> None:
    arguments: Json = {
        "workspaces": ["acme"],
        "repositories": ["billing", "gateway"],
        "paths": ["src/fees/**", "lib/fees/**"],
        "author": "example",
        "since": day(2).isoformat(),
        "until": day(8).isoformat(),
        "text": "fee",
        "limit": 2,
    }
    token = await world.token()
    async with mcp_client(world.app(), token) as client:
        over_mcp = ok(await client.call_tool("list_commits", arguments))
    async with http_client(world.app(), token) as http:
        over_http = await http.get("/v1/commits", params=arguments)
        unknown = await http.get("/v1/commits", params={"domain": "fees"})
        bad_glob = await http.get("/v1/commits", params={"paths": "a\nb"})

    assert labels(world, over_mcp) == ["b4", "g2"]
    assert (over_mcp["cut_by"], when(over_mcp["next_until"])) == ("limit", day(4))
    assert body(over_http) == over_mcp
    assert error(unknown, 422, "validation_error") == "The request is not valid."
    assert error(bad_glob, 422, "invalid_search") == "A path glob holds a NUL or a line break."


async def row_counts(db: Conn) -> dict[str, int]:
    """The rows of every table of the public schema, as the catalog lists them."""
    cur = await db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    counts: dict[str, int] = {}
    for (table,) in await cur.fetchall():
        count = await db.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        row = await count.fetchone()
        assert row is not None
        counts[table] = row[0]
    return counts


def mirror_files(root: Path) -> dict[str, int]:
    """Every file of the mirrors with its size: a read leaves them as they were."""
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def test_nothing_is_stored_no_table_gains_rows(world: World, tmp_path: Path) -> None:
    token = await world.token()
    scoped = await world.token("read", workspace=world.acme)
    before = await row_counts(world.db)
    files = mirror_files(tmp_path / "mirrors")
    assert before["record"] > 0  # the sync stored the files, so the counts would show a write
    assert before["person"] == 3

    async with mcp_client(world.app(), token) as client:
        everything = ok(await client.call_tool("list_commits", {}))
        ok(await client.call_tool("list_commits", {"paths": ["src/fees/**"], "author": "ann"}))
        ok(await client.call_tool("list_commits", {"text": "fee", "limit": 1}))
    async with http_client(world.app(), scoped) as http:
        body(await http.get("/v1/commits", params={"since": day(2).isoformat()}))
    slow = world.app(limits=CommitLimits(timeout=0.2), git=slow_git(tmp_path))
    async with http_client(slow, token) as http:
        error(await http.get("/v1/commits"), 422, "commits_timeout")

    assert len(everything["commits"]) == 9
    # Authors nobody knew are still nobody: a commit makes no person, alias or record.
    assert await row_counts(world.db) == before
    assert mirror_files(tmp_path / "mirrors") == files
