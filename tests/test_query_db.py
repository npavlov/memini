"""The query tools through the MCP endpoint and the HTTP API, against a real Postgres and real git
mirrors: search, search_code, get_symbol, get_file.

Acceptance of memini-o8t: each tool is called through the MCP endpoint by a real MCP client; path
traversal attempts are refused. Repositories are made here and synced as the git_sync job syncs
them; every file is synthetic. Vectors come from the FakeEmbedder, whose vectors carry no meaning,
so a "semantic" query here finds chunks through its words.
"""

import logging
import os
import shutil
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from git_helpers import SourceRepo
from memini.api import create_app
from memini.auth import Principal, ReadFilter, Scope, WriteAccess, authorization, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import EmbeddingSpec, FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, run_git_sync
from memini.jobs import Job
from memini.mcp import MCP_PATH
from memini.mcp.text import TEXT_NOTICE
from memini.query import QueryServices, ReadLimits
from memini.rerank import RerankError
from memini.search import Hit
from memini.sources import NewGitSource, Source, create_git_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.git,
    pytest.mark.anyio,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

BASE_URL = "http://memini.test"
MAX_BLOB = 4096
MAIN = "refs/heads/main"

LAMP = '''"""A desk lamp with a dimmer."""


class Lamp:
    """A lamp that can be switched and dimmed."""

    def __init__(self, level):
        self.level = level
        self.lit = False

    def on(self):
        """Switch the lamp on at its level."""
        self.lit = True
        return self.level

    def off(self):
        """Switch the lamp off."""
        self.lit = False
        return 0


def brighten(lamp, step):
    """Raise the lamp's level by step, at most 100."""
    lamp.level = min(lamp.level + step, 100)
    return lamp.level
'''

CART = """export function totalPrice(items: number[]): number {
  let total = 0;
  for (const item of items) {
    total += item;
  }
  return total;
}
"""

VAULT = '''def unlock(code):
    """Open the vault of the other workspace."""
    return code == 1234
'''

FIRST_COMMIT = datetime(2026, 1, 2, 12, tzinfo=UTC)
"""The committer time of the first commit of each fixture repository (git_helpers)."""

LONG = "".join(f"line {number}\n" for number in range(1, 31))
BINARY = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR synthetic image"
SUBMODULE = "1" * 40


@dataclass
class World:
    pool: Pool
    db: Conn
    settings: Settings
    owner: uuid.UUID
    acme: uuid.UUID
    other: uuid.UUID
    shop: SourceRepo
    pin: str
    """The commit main of acme/shop is pinned at."""
    head: str
    """A later commit of main: fetched into the mirror, not synced."""
    dev: str
    """The head of the untracked branch dev."""
    embedder: FakeEmbedder

    def app(self, *, embedder: FakeEmbedder | None = None, default: bool = False) -> FastAPI:
        """The application with the world's embedder, another one, or (default) none given."""
        if default:
            return create_app(self.settings, pool=self.pool)
        return create_app(self.settings, pool=self.pool, embedder=embedder or self.embedder)

    async def token(self, *scopes: Scope, workspace: uuid.UUID | None = None) -> str:
        new = await create_token(
            self.db,
            owner_id=self.owner,
            name="test agent",
            scopes=scopes or ("read",),
            workspace_id=workspace,
        )
        return new.secret.get_secret_value()


def symlink(repo: SourceRepo, path: str, target: str) -> None:
    link = repo.path / path
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)
    repo.git("add", "--", path)


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


async def sync(
    world_pool: Pool, settings: Settings, embedder: FakeEmbedder, source: Source
) -> None:
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
    mirrors = MirrorManager(GitOptions.from_settings(settings))
    await run_git_sync(world_pool, mirrors, embedder, job)


@pytest.fixture
async def world(migrated_database_url: str, tmp_path: Path) -> AsyncIterator[World]:
    """acme/shop: synced at ``pin``; main moved on to ``head`` in the mirror; an untracked dev
    branch and a tag v1. other/shop: another repository of the same name. acme/pending: never
    synced."""
    repos = tmp_path / "repos"
    settings = Settings(
        database_url=SecretStr(migrated_database_url),
        git_mirror_dir=tmp_path / "mirrors",
        git_local_roots=(repos,),
        git_max_blob_size=MAX_BLOB,
    )
    shop = SourceRepo(repos / "shop")
    symlink(shop, "docs/readme.md", "../README.md")
    symlink(shop, "escape", "/etc/passwd")
    shop.git("update-index", "--add", "--cacheinfo", f"160000,{SUBMODULE},vendor/lib")
    pin = shop.commit(
        {
            "README.md": "# Shop\n\nA synthetic shop for tests.\n",
            "src/lamp.py": LAMP,
            "web/cart.ts": CART,
            "notes/long.txt": LONG,
            "notes/empty.txt": "",
            "assets/logo.png": BINARY,
            "data/big.txt": "y" * (MAX_BLOB + 1),
        },
        "first",
    )
    shop.git("tag", "v1")
    shop.git("checkout", "--quiet", "-b", "dev")
    dev = shop.commit({"src/lamp.py": LAMP.replace("at most 100", "at most 50")}, "dimmer")
    shop.git("checkout", "--quiet", "main")
    vault = SourceRepo(repos / "vault")
    vault.commit({"vault.py": VAULT}, "vault")
    pending = SourceRepo(repos / "pending")
    pending.commit({"a.py": "x = 1\n"}, "pending")

    embedder = FakeEmbedder()
    async with (
        open_pool(migrated_database_url, min_size=1, max_size=8, timeout=5) as pool,
        connect(migrated_database_url, autocommit=True) as db,
    ):
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        other = await create_workspace(db, owner_id=owner, name="other")
        acme_shop = await add_source(db, owner, "acme", "shop", shop, settings)
        other_shop = await add_source(db, owner, "other", "shop", vault, settings)
        await add_source(db, owner, "acme", "pending", pending, settings)
        await sync(pool, settings, embedder, acme_shop)
        await sync(pool, settings, embedder, other_shop)
        head = shop.commit({"README.md": "# Shop\n\nMoved on.\n"}, "later")
        await MirrorManager(GitOptions.from_settings(settings)).sync(acme_shop.id, str(shop.path))
        yield World(pool, db, settings, owner, acme.id, other.id, shop, pin, head, dev, embedder)


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


# --- tools/list ----------------------------------------------------------------------------------


async def test_the_query_tools_are_listed_with_their_schemas(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert list(tools) == [
        "search",
        "search_code",
        "get_file",
        "get_symbol",
        "list_commits",
        "get_catalog",
        "get_project",
        "get_domain",
        "catalog_guide",
        "list_meetings",
        "get_meeting",
        "list_messages",
        "get_thread",
        "list_decisions",
        "get_decision",
        "status",
    ]
    expected = {
        "search": {"query", "limit", "workspaces", "sources", "repositories", "kinds", "since"},
        "search_code": {"query", "mode", "repositories", "paths", "languages", "case_sensitive"},
        "get_file": {"repository", "path", "workspace", "ref", "start_line", "end_line"},
        "get_symbol": {"name", "match", "repositories", "paths", "languages"},
        "list_commits": {"repositories", "workspaces", "paths", "author", "since", "until", "text"},
    }
    for name, arguments in expected.items():
        tool = tools[name]
        assert arguments <= set(tool.input_schema["properties"]), name
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    assert tools["search"].input_schema["required"] == ["query"]
    assert tools["get_file"].input_schema["required"] == ["source"]


# --- search and search_code ----------------------------------------------------------------------


def code_hit(hits: list[Json], path: str) -> Json:
    return next(hit for hit in hits if hit["path"] == path)


async def test_search_finds_code_with_repo_at_sha_and_time(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        result = await client.call_tool("search", {"query": "lamp level", "workspaces": ["acme"]})

    hits = ok(result)["items"]
    lamp = code_hit(hits, "src/lamp.py")
    assert lamp["workspace"] == "acme"
    assert (lamp["source"], lamp["source_kind"], lamp["kind"]) == (
        "shop",
        "repository",
        "code_file",
    )
    assert (lamp["source"], lamp["ref"], lamp["commit"]) == ("shop", MAIN, world.pin)
    assert lamp["locator"].startswith(f"shop@{world.pin}:src/lamp.py#L")
    assert when(lamp["occurred_at"]) == FIRST_COMMIT
    assert lamp["score"] > 0
    assert lamp["snippet"] in LAMP
    assert all(hit["workspace"] == "acme" for hit in hits)
    summary = text(result)
    assert summary.startswith(f"{len(hits)} hits.")
    assert lamp["locator"] in summary


async def test_search_filters_by_kind_and_time(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        threads = await client.call_tool("search", {"query": "lamp", "kinds": ["thread"]})
        documents = await client.call_tool(
            "search", {"query": "synthetic shop", "kinds": ["document"], "workspaces": ["acme"]}
        )
        later = await client.call_tool(
            "search", {"query": "lamp", "since": "2026-02-01T00:00:00+00:00"}
        )
        naive = await client.call_tool("search", {"query": "lamp", "since": "2026-02-01T00:00"})
        lexical = await client.call_tool("search", {"query": "unlock", "mode": "lexical"})

    assert ok(threads)["items"] == []
    # A Markdown file of a repository is a document, located like code.
    readme = ok(documents)["items"][0]
    assert (readme["kind"], readme["source_kind"], readme["path"]) == (
        "document",
        "git",
        "README.md",
    )
    assert (readme["source"], readme["ref"], readme["commit"]) == ("shop", MAIN, world.pin)
    assert readme["locator"].startswith(f"shop@{world.pin}:README.md")
    assert ok(later)["items"] == []
    assert refused(naive) == "Invalid arguments: since: timezone_aware."
    (vault,) = ok(lexical)["items"]
    assert (vault["workspace"], vault["path"], vault["symbol"]) == ("other", "vault.py", "unlock")


async def test_search_code_in_every_mode(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        semantic = await client.call_tool(
            "search_code", {"query": "totalPrice items", "sources": ["shop"]}
        )
        exact = await client.call_tool(
            "search_code", {"query": "def brighten", "mode": "exact", "workspaces": ["acme"]}
        )
        insensitive = await client.call_tool(
            "search_code", {"query": "DEF BRIGHTEN", "mode": "exact", "case_sensitive": False}
        )
        regex = await client.call_tool(
            "search_code", {"query": r"^ +def o(n|ff)\(", "mode": "regex"}
        )
        symbol = await client.call_tool("search_code", {"query": "Lamp.on", "mode": "symbol"})
        by_path = await client.call_tool(
            "search_code", {"query": "total", "mode": "exact", "paths": ["web/**"]}
        )
        by_language = await client.call_tool(
            "search_code", {"query": "return", "mode": "exact", "languages": ["python"]}
        )

    cart = code_hit(ok(semantic)["items"], "web/cart.ts")
    assert (cart["commit"], cart["language"], cart["symbol"]) == (
        world.pin,
        "typescript",
        "totalPrice",
    )
    (brighten,) = ok(exact)["items"]
    assert brighten["locator"].startswith(f"shop@{world.pin}:src/lamp.py#L")
    assert brighten["match_line"] == 22
    assert brighten["score"] is None
    assert [hit["match_line"] for hit in ok(insensitive)["items"]] == [22]
    regex_hits = ok(regex)["items"]
    assert {hit["path"] for hit in regex_hits} == {"src/lamp.py"}
    assert regex_hits[0]["match_line"] == 11
    (on,) = ok(symbol)["items"]
    assert on["definition"]["qualified_name"] == "Lamp.on"
    assert on["definition"]["kind"] == "method"
    assert (on["definition"]["start_line"], on["definition"]["end_line"]) == (11, 14)
    assert {hit["path"] for hit in ok(by_path)["items"]} == {"web/cart.ts"}
    python = ok(by_language)["items"]
    assert {hit["path"] for hit in python} == {"src/lamp.py", "vault.py"}
    assert {hit["language"] for hit in python} == {"python"}


async def test_search_code_reports_a_bad_query_as_a_tool_error(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        bad_regex = await client.call_tool("search_code", {"query": "(unclosed", "mode": "regex"})
        bad_glob = await client.call_tool(
            "search_code", {"query": "x", "mode": "exact", "paths": ["a\nb"]}
        )
        bad_mode = await client.call_tool("search_code", {"query": "x", "mode": "fuzzy"})
        blank = await client.call_tool("search_code", {"query": "   "})

    assert refused(bad_regex).startswith("The regular expression is not usable")
    assert "(unclosed" not in text(bad_regex)
    assert refused(bad_glob) == "A path glob holds a NUL or a line break."
    assert refused(bad_mode) == "Invalid arguments: mode: literal_error."
    assert refused(blank) == "The query text is empty."


async def test_search_refuses_a_stale_index_and_names_the_rebuild(world: World) -> None:
    other_model = FakeEmbedder(
        EmbeddingSpec(provider="openai_compatible", model="fake-embedder-2", dimension=12)
    )
    async with mcp_client(world.app(embedder=other_model), await world.token()) as client:
        searched = await client.call_tool("search", {"query": "lamp"})
        exact = await client.call_tool("search_code", {"query": "lamp", "mode": "exact"})
        symbol = await client.call_tool("get_symbol", {"name": "Lamp"})
        read = await client.call_tool("get_file", {"workspace": "acme", "source": "shop"})

    for result in (searched, exact, symbol):
        message = refused(result)
        assert message.startswith("The index of workspace acme is out of date")
        assert "`memini reindex --workspace acme`" in message
    assert other_model.batches == []  # refused before the query was embedded
    assert ok(read)["kind"] == "directory"  # reading code needs no index


async def test_without_embeddings_search_says_so_and_reading_works(world: World) -> None:
    async with mcp_client(world.app(default=True), await world.token()) as client:
        searched = await client.call_tool("search", {"query": "lamp"})
        symbol = await client.call_tool("get_symbol", {"name": "Lamp"})
        read = await client.call_tool(
            "get_file", {"workspace": "acme", "source": "shop", "path": "src/lamp.py"}
        )

    for result in (searched, symbol):
        assert "MEMINI_EMBEDDING_PROVIDER" in refused(result)
    assert ok(read)["commit"] == world.pin


# --- get_symbol ----------------------------------------------------------------------------------


async def test_get_symbol_finds_functions_and_methods_with_their_location(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        method = await client.call_tool("get_symbol", {"name": "on"})
        function = await client.call_tool("get_symbol", {"name": "brighten"})
        qualified = await client.call_tool("get_symbol", {"name": "Lamp.off"})
        prefix = await client.call_tool("get_symbol", {"name": "Lam", "match": "prefix"})
        typescript = await client.call_tool(
            "get_symbol", {"name": "totalprice", "case_sensitive": False}
        )
        filtered = await client.call_tool(
            "get_symbol", {"name": "brighten", "languages": ["typescript"]}
        )
        missing = await client.call_tool("get_symbol", {"name": "nothing_here"})

    (on,) = ok(method)["definitions"]
    assert (on["qualified_name"], on["kind"], on["parent"]) == ("Lamp.on", "method", "Lamp")
    assert (on["source"], on["ref"], on["commit"]) == ("shop", MAIN, world.pin)
    assert on["locator"] == f"shop@{world.pin}:src/lamp.py#L11-L14"
    assert when(on["occurred_at"]) == FIRST_COMMIT
    assert "def on(self):" in on["snippet"]
    (brighten,) = ok(function)["definitions"]
    assert (brighten["kind"], brighten["start_line"], brighten["end_line"]) == ("function", 22, 25)
    assert brighten["snippet"].startswith("def brighten(lamp, step):")
    assert [d["qualified_name"] for d in ok(qualified)["definitions"]] == ["Lamp.off"]
    assert [d["qualified_name"] for d in ok(prefix)["definitions"]] == ["Lamp"]
    (total,) = ok(typescript)["definitions"]
    assert (total["qualified_name"], total["language"]) == ("totalPrice", "typescript")
    assert ok(filtered)["definitions"] == []
    assert ok(missing)["definitions"] == []
    assert text(method).startswith(f"1 definition. {TEXT_NOTICE}\n\n1. method Lamp.on at shop@")


# --- get_file ---------------------------------------------------------------------------------


async def test_read_source_reads_a_file_at_the_pinned_commit(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        whole = await client.call_tool(
            "get_file", {"workspace": "acme", "source": "shop", "path": "src/lamp.py"}
        )
        lines = await client.call_tool(
            "get_file",
            {
                "workspace": "acme",
                "source": "shop",
                "path": "src/lamp.py",
                "start_line": 11,
                "end_line": 14,
            },
        )

    read = ok(whole)
    assert (read["workspace"], read["source"], read["source_kind"]) == (
        "acme",
        "shop",
        "repository",
    )
    assert (read["ref"], read["commit"], read["pinned"]) == (MAIN, world.pin, True)
    assert when(read["occurred_at"]) == FIRST_COMMIT
    assert (read["kind"], read["language"], read["binary"]) == ("file", "python", False)
    assert read["content"] == LAMP
    assert (read["start_line"], read["end_line"], read["total_lines"]) == (1, 25, 25)
    assert read["truncated"] is False
    assert read["locator"] == f"shop@{world.pin}:src/lamp.py#L1-L25"
    assert {s["qualified_name"] for s in read["symbols"]} == {
        "Lamp",
        "Lamp.__init__",
        "Lamp.on",
        "Lamp.off",
        "brighten",
    }
    part = ok(lines)
    assert part["content"] == "".join(LAMP.splitlines(keepends=True)[10:14])
    assert (part["start_line"], part["end_line"]) == (11, 14)
    assert part["locator"] == f"shop@{world.pin}:src/lamp.py#L11-L14"
    assert [s["qualified_name"] for s in part["symbols"]] == ["Lamp", "Lamp.on"]
    summary = text(lines)
    assert summary.startswith(f"shop@{world.pin}:src/lamp.py#L11-L14 (pinned commit of")
    assert "def on(self):" in summary


async def test_read_source_at_a_ref_a_tag_or_a_commit(world: World) -> None:
    async def read(client: Client, ref: str) -> Json:
        return ok(
            await client.call_tool(
                "get_file",
                {"workspace": "acme", "source": "shop", "path": "README.md", "ref": ref},
            )
        )

    async with mcp_client(world.app(), await world.token()) as client:
        tracked = await read(client, "main")
        full = await read(client, MAIN)
        later = await read(client, world.head)
        tag = await read(client, "v1")
        dev = ok(
            await client.call_tool(
                "get_file",
                {"workspace": "acme", "source": "shop", "path": "src/lamp.py", "ref": "dev"},
            )
        )

    assert (tracked["commit"], tracked["pinned"], tracked["ref"]) == (world.pin, True, MAIN)
    assert full["commit"] == world.pin
    assert tracked["content"] == "# Shop\n\nA synthetic shop for tests.\n"
    assert (later["commit"], later["pinned"], later["ref"]) == (world.head, False, None)
    assert later["content"] == "# Shop\n\nMoved on.\n"
    assert later["symbols"] == []
    assert (tag["commit"], tag["ref"], tag["pinned"]) == (world.pin, "v1", False)
    assert (dev["commit"], dev["ref"], dev["pinned"]) == (world.dev, "dev", False)
    assert "at most 50" in dev["content"]
    assert dev["symbols"] == []  # the index holds main's content, not dev's


async def test_read_source_lists_directories_and_reports_other_entries(world: World) -> None:
    async def read(client: Client, path: str, **more: Any) -> CallToolResult:
        return await client.call_tool(
            "get_file", {"workspace": "acme", "source": "shop", "path": path, **more}
        )

    async with mcp_client(world.app(), await world.token()) as client:
        root = ok(await read(client, ""))
        src = ok(await read(client, "src/"))
        link = ok(await read(client, "docs/readme.md"))
        escape = ok(await read(client, "escape"))
        submodule = ok(await read(client, "vendor/lib"))
        image = ok(await read(client, "assets/logo.png"))
        empty = ok(await read(client, "notes/empty.txt"))
        big = await read(client, "data/big.txt")
        missing = await read(client, "src/missing.py")
        through_link = await read(client, "escape/shadow")
        past_end = await read(client, "notes/long.txt", start_line=31)
        backwards = await read(client, "notes/long.txt", start_line=5, end_line=4)
        bad_ref = await read(client, "README.md", ref="main~1")
        no_ref = await read(client, "README.md", ref="feature")
        no_commit = await read(client, "README.md", ref="0" * 40)

    assert root["kind"] == "directory"
    assert root["locator"] == f"shop@{world.pin}"
    assert [(e["name"], e["kind"]) for e in root["entries"]] == [
        ("README.md", "file"),
        ("assets", "directory"),
        ("data", "directory"),
        ("docs", "directory"),
        ("escape", "symlink"),
        ("notes", "directory"),
        ("src", "directory"),
        ("vendor", "directory"),
        ("web", "directory"),
    ]
    assert src["path"] == "src"
    assert [(e["path"], e["size"]) for e in src["entries"]] == [("src/lamp.py", len(LAMP))]
    assert (link["kind"], link["target"], link["content"]) == ("symlink", "../README.md", None)
    assert (escape["kind"], escape["target"], escape["content"]) == ("symlink", "/etc/passwd", None)
    assert (submodule["kind"], submodule["target"]) == ("submodule", SUBMODULE)
    assert (image["binary"], image["size"], image["content"]) == (True, len(BINARY), None)
    assert (empty["content"], empty["total_lines"], empty["start_line"]) == ("", 0, None)
    assert refused(big) == (
        f"The file holds {MAX_BLOB + 1} bytes, over the read limit of {MAX_BLOB} bytes"
        " (git_max_blob_size)."
    )
    assert refused(missing) == "No such path."
    assert refused(through_link) == "No such path."
    assert refused(past_end) == "The start line is past the end of the file, which has 30 lines."
    assert refused(backwards) == "The end line is before the start line."
    assert refused(bad_ref).startswith("A ref is a branch or tag name")
    assert refused(no_ref) == "No such ref or commit."
    assert refused(no_commit) == "No such ref or commit."


async def test_read_source_stops_at_its_limits(world: World) -> None:
    app = world.app()
    services: QueryServices = app.state.services
    services.read_limits = ReadLimits(max_lines=10, max_chars=50, max_entries=2)

    async with mcp_client(app, await world.token()) as client:
        lines = ok(
            await client.call_tool(
                "get_file",
                {"workspace": "acme", "source": "shop", "path": "notes/long.txt"},
            )
        )
        chars = ok(
            await client.call_tool(
                "get_file",
                {
                    "workspace": "acme",
                    "source": "shop",
                    "path": "notes/long.txt",
                    "start_line": 20,
                },
            )
        )
        listing = await client.call_tool("get_file", {"workspace": "acme", "source": "shop"})

    assert (lines["start_line"], lines["end_line"], lines["truncated"]) == (1, 7, True)
    assert lines["content"] == "".join(f"line {n}\n" for n in range(1, 8))  # 49 characters
    assert (chars["start_line"], chars["end_line"], chars["truncated"]) == (20, 25, True)
    assert len(ok(listing)["entries"]) == 2
    assert ok(listing)["truncated"] is True
    assert "the first 2 shown" in text(listing)


PATHS = [
    "../README.md",
    "/etc/passwd",
    "src/../../etc/passwd",
    "src/./lamp.py",
    "src//lamp.py",
    "..",
    ".",
    "/",
    "src/lamp.py\x00.txt",
    "\x00",
    "src/\nlamp.py",
]


@pytest.mark.parametrize("path", PATHS)
async def test_read_source_refuses_paths_out_of_the_repository(world: World, path: str) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        result = await client.call_tool(
            "get_file", {"workspace": "acme", "source": "shop", "path": path}
        )

    message = refused(result)
    assert message.startswith("A path is relative to the repository root")
    assert "passwd" not in message


@pytest.mark.parametrize("path", ["%2e%2e/%2e%2e/etc/passwd", "..%2Fetc%2Fpasswd", "~/x"])
async def test_an_encoded_path_is_a_name_not_a_way_out(world: World, path: str) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        result = await client.call_tool(
            "get_file", {"workspace": "acme", "source": "shop", "path": path}
        )

    assert refused(result) == "No such path."


# --- Access ---------------------------------------------------------------------------------------


async def test_a_workspace_token_reads_only_its_workspace(world: World) -> None:
    token = await world.token("read", workspace=world.other)
    async with mcp_client(world.app(), token) as client:
        acme = await client.call_tool("get_file", {"workspace": "acme", "source": "shop"})
        unnamed = ok(await client.call_tool("get_file", {"source": "shop", "path": "vault.py"}))
        searched = ok(await client.call_tool("search_code", {"query": "def", "mode": "exact"}))
        symbols = ok(await client.call_tool("get_symbol", {"name": "brighten"}))
        everything = ok(await client.call_tool("search", {"query": "lamp unlock"}))

    assert refused(acme) == "No such workspace."
    assert (unnamed["workspace"], unnamed["content"]) == ("other", VAULT)
    assert {hit["workspace"] for hit in searched["items"]} == {"other"}
    assert symbols["definitions"] == []
    assert {hit["workspace"] for hit in everything["items"]} == {"other"}


async def test_a_repository_name_in_two_workspaces_needs_the_workspace(world: World) -> None:
    async with mcp_client(world.app(), await world.token()) as client:
        ambiguous = await client.call_tool("get_file", {"source": "shop"})
        unknown = await client.call_tool("get_file", {"source": "nowhere"})
        unsynced = await client.call_tool("get_file", {"workspace": "acme", "source": "pending"})

    assert refused(ambiguous) == (
        "More than one workspace has a repository of that name: name the workspace."
    )
    assert refused(unknown) == "No such repository."
    assert refused(unsynced) == (
        "The repository has not been synced yet: its first sync creates its mirror."
    )


async def test_every_query_tool_reads_through_the_authorization_filter(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Principal] = []
    original = authorization.authorize_read

    def watched(principal: Principal, **kwargs: Any) -> ReadFilter:
        calls.append(principal)
        return original(principal, **kwargs)

    monkeypatch.setattr(authorization, "authorize_read", watched)
    token = await world.token("read", workspace=world.acme)
    async with mcp_client(world.app(), token) as client:
        ok(await client.call_tool("search", {"query": "lamp"}))
        ok(await client.call_tool("search_code", {"query": "lamp", "mode": "exact"}))
        ok(await client.call_tool("get_symbol", {"name": "Lamp"}))
        ok(await client.call_tool("get_file", {"source": "shop"}))

    assert len(calls) == 4
    assert {principal.workspace_id for principal in calls} == {world.acme}


_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "search", "arguments": {"query": "lamp"}},
}


async def test_a_token_without_the_read_scope_calls_no_tool(world: World) -> None:
    token = await world.token("ingest", "admin")
    async with http_client(world.app(), token) as http:
        response = await http.post(
            MCP_PATH, json=_CALL, headers={"Accept": "application/json, text/event-stream"}
        )
        search = await http.get("/v1/search", params={"query": "lamp"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert search.status_code == 403


# --- HTTP routes ---------------------------------------------------------------------------------


def body(response: httpx2.Response, status: int = 200) -> Json:
    assert response.status_code == status, response.text
    found: Json = response.json()
    return found


def error(response: httpx2.Response, status: int, code: str) -> str:
    assert response.status_code == status, response.text
    found: Json = response.json()["error"]
    assert found["code"] == code
    return str(found["message"])


async def test_http_search_routes_mirror_the_tools(world: World) -> None:
    async with http_client(world.app(), await world.token()) as http:
        searched = await http.get(
            "/v1/search", params={"query": "lamp", "workspaces": ["acme"], "kinds": ["code_file"]}
        )
        code = await http.get("/v1/search/code", params={"query": "def brighten", "mode": "exact"})
        symbols = await http.get("/v1/symbols", params={"name": "Lamp.on"})
        since = await http.get(
            "/v1/search", params={"query": "lamp", "since": "2026-01-01T00:00:00+00:00"}
        )

    hits = body(searched)["items"]
    assert code_hit(hits, "src/lamp.py")["commit"] == world.pin
    (brighten,) = body(code)["items"]
    assert brighten["match_line"] == 22
    (on,) = body(symbols)["definitions"]
    assert on["locator"] == f"shop@{world.pin}:src/lamp.py#L11-L14"
    assert body(since)["items"]  # the + of the offset reaches the server encoded


class Reversing:
    async def rerank(self, query: str, hits: Sequence[Hit]) -> list[Hit]:
        return list(reversed(hits))


class Broken:
    async def rerank(self, query: str, hits: Sequence[Hit]) -> list[Hit]:
        raise RerankError("the reranker's model files are gone")


async def test_a_reranker_reorders_search_and_its_failure_is_no_error(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    token = await world.token()
    params = {"query": "lamp level", "workspaces": ["acme"], "limit": 3}
    async with http_client(world.app(), token) as http:
        plain = body(await http.get("/v1/search", params=params))["items"]
    app = create_app(world.settings, pool=world.pool, embedder=world.embedder, reranker=Reversing())
    async with http_client(app, token) as http:
        reversed_hits = body(await http.get("/v1/search", params=params))["items"]
    app = create_app(world.settings, pool=world.pool, embedder=world.embedder, reranker=Broken())
    with caplog.at_level(logging.WARNING, logger="memini.search.engine"):
        async with http_client(app, token) as http:
            kept = body(await http.get("/v1/search", params=params))["items"]
            code = body(await http.get("/v1/search/code", params={"query": "lamp"}))["items"]

    assert len(plain) == 3
    assert [h["locator"] for h in reversed_hits][:1] != [h["locator"] for h in plain][:1]
    assert kept == plain  # the fused order, answered with 200
    assert code
    assert "reranking failed, the fused order is kept: RerankError" in caplog.text


async def test_http_search_errors_have_the_error_shape(world: World) -> None:
    stale = FakeEmbedder(EmbeddingSpec(provider="openai_compatible", model="other", dimension=4))
    async with http_client(world.app(), await world.token()) as http:
        unknown = await http.get("/v1/search", params={"query": "lamp", "colour": "red"})
        bad_regex = await http.get("/v1/search/code", params={"query": "(x", "mode": "regex"})
        missing = await http.get("/v1/symbols")
    async with http_client(world.app(embedder=stale), await world.token()) as http:
        mismatch = await http.get("/v1/search", params={"query": "lamp"})
    async with http_client(world.app(default=True), await world.token()) as http:
        unconfigured = await http.get("/v1/symbols", params={"name": "Lamp"})

    assert error(unknown, 422, "validation_error") == "The request is not valid."
    assert error(bad_regex, 422, "invalid_search").startswith("The regular expression")
    assert error(missing, 422, "validation_error") == "The request is not valid."
    assert "memini reindex --workspace acme" in error(mismatch, 503, "index_mismatch")
    assert "MEMINI_EMBEDDING_PROVIDER" in error(unconfigured, 503, "embeddings_not_configured")


FILES = "/v1/workspaces/acme/sources/shop/files"


async def test_http_reads_files_and_directories(world: World) -> None:
    async with http_client(world.app(), await world.token()) as http:
        root = await http.get(f"{FILES}/")
        lamp = await http.get(f"{FILES}/src/lamp.py", params={"start_line": 22})
        later = await http.get(f"{FILES}/README.md", params={"ref": world.head})
        missing = await http.get(f"{FILES}/src/nothing.py")
        too_large = await http.get(f"{FILES}/data/big.txt")
        bad_range = await http.get(f"{FILES}/src/lamp.py", params={"start_line": 0})
        unsynced = await http.get("/v1/workspaces/acme/sources/pending/files/a.py")

    assert body(root)["kind"] == "directory"
    read = body(lamp)
    assert read["content"].startswith("def brighten(lamp, step):")
    assert (read["start_line"], read["end_line"], read["commit"]) == (22, 25, world.pin)
    assert body(later)["content"] == "# Shop\n\nMoved on.\n"
    assert error(missing, 404, "not_found") == "No such path."
    assert error(too_large, 422, "file_too_large").startswith("The file holds")
    assert error(bad_range, 422, "validation_error") == "The request is not valid."
    assert error(unsynced, 409, "not_synced").startswith("The repository has not been synced")


@pytest.mark.parametrize(
    "path",
    [
        "..%2F..%2Fetc%2Fpasswd",
        "src%2F..%2F..%2Fetc%2Fpasswd",
        "%2Fetc%2Fpasswd",
        "src/%2e%2e/%2e%2e/etc/passwd",
        "src/lamp.py%00",
        "%2e%2e",
    ],
)
async def test_http_refuses_paths_out_of_the_repository(world: World, path: str) -> None:
    async with http_client(world.app(), await world.token()) as http:
        response = await http.get(f"{FILES}/{path}")

    # The client sends the percent-encoding as it is; the server decodes it before routing.
    assert response.request.url.raw_path.endswith(path.encode())
    message = error(response, 422, "invalid_path")
    assert message.startswith("A path is relative to the repository root")
    assert "passwd" not in response.text


async def test_http_reads_only_what_the_token_may_read(world: World) -> None:
    token = await world.token("read", workspace=world.other)
    async with http_client(world.app(), token) as http:
        acme = await http.get(f"{FILES}/README.md")
        other = await http.get("/v1/workspaces/other/sources/shop/files/vault.py")
        searched = await http.get("/v1/search/code", params={"query": "def", "mode": "exact"})

    assert error(acme, 404, "not_found") == "No such workspace."
    assert body(other)["content"] == VAULT
    assert {hit["workspace"] for hit in body(searched)["items"]} == {"other"}
