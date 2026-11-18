"""``memini replay`` against a real Postgres: the operator command over memini.responses.replay.
The hearth recipe of docs/recipes.md and its test parser; every value is synthetic."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString

import pytest

from hearth_helpers import (
    CHANNEL,
    HEARTH_RECIPE_PATH,
    WINDOW_1,
    FakeClient,
    FakeHearth,
    Target,
    hearth_parser,
    hearth_recipe,
    sync_window,
)
from memini.auth import WriteAccess
from memini.cli import main
from memini.cli import replay as replay_cli
from memini.cli.common import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from memini.config import Settings, environment_variables
from memini.db import connect, migrate
from memini.embeddings import Embedder, EmbeddingsNotConfiguredError
from memini.recipes import register_parser, unregister_parser
from memini.workspaces import create_workspace, ensure_principal

pytestmark = pytest.mark.integration

PARSER = "hearth.thread"


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> Run:
    capsys.readouterr()
    code = main(list(argv))
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


@dataclass(frozen=True)
class World:
    url: str
    client: FakeClient


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, empty_database_url: str) -> Iterator[World]:
    """A migrated database in ``MEMINI_DATABASE_URL``; workspace ``acme`` of the CLI's principal
    with the mcp source ``hearth``, one window of it synced with the parser that writes user ids;
    no embedding provider; ``hearth.thread`` registered as the parser that writes names."""
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    url = empty_database_url
    monkeypatch.setenv("MEMINI_DATABASE_URL", url)

    def create_embedder(settings: Settings) -> Embedder:
        raise EmbeddingsNotConfiguredError()

    monkeypatch.setattr(replay_cli, "create_embedder", create_embedder)
    client = FakeClient(FakeHearth())

    async def build() -> None:
        await migrate(url)
        async with connect(url, autocommit=True) as conn:
            owner = await ensure_principal(conn, "owner")
            acme = await create_workspace(conn, owner_id=owner, name="acme")
            cur = await conn.execute(
                "INSERT INTO source (workspace_id, owner_id, name, kind)"
                " VALUES (%s, %s, 'hearth', 'mcp') RETURNING id",
                (acme.id, owner),
            )
            source = await cur.fetchone()
            assert source is not None
            cur = await conn.execute(
                "INSERT INTO stream (workspace_id, owner_id, source_id, key)"
                " VALUES (%s, %s, %s, %s) RETURNING id",
                (acme.id, owner, source[0], CHANNEL),
            )
            stream = await cur.fetchone()
            assert stream is not None
            await sync_window(
                conn,
                client,
                Target(acme.id, owner, source[0], stream[0]),
                WINDOW_1,
                recipe=hearth_recipe(),
                parser=hearth_parser(),
                access=WriteAccess.for_owner(owner),
            )

    asyncio.run(build())
    register_parser(PARSER, hearth_parser(names=True), replace=True)
    try:
        yield World(url, client)
    finally:
        unregister_parser(PARSER)


def query(url: str, sql: LiteralString) -> list[tuple[Any, ...]]:
    async def run() -> list[tuple[Any, ...]]:
        async with connect(url) as conn:
            cur = await conn.execute(sql)
            return [tuple(row) for row in await cur.fetchall()]

    return asyncio.run(run())


def authors(url: str) -> set[str]:
    found = query(url, "SELECT convert_from(payload, 'UTF8') FROM record")
    return {m["author"] for (payload,) in found for m in json.loads(payload)["messages"]}


def test_replay_parses_the_stored_responses_again(
    capsys: pytest.CaptureFixture[str], world: World
) -> None:
    calls = world.client.calls
    assert authors(world.url) == {"U0001", "U0002", "U0003", "U0004"}

    result = invoke(
        capsys,
        "replay",
        "--workspace",
        "acme",
        "--source",
        "hearth",
        "--recipe",
        str(HEARTH_RECIPE_PATH),
    )

    assert result.code == EXIT_OK, result.err
    assert result.out == (
        "replayed 1 windows of source hearth (1 complete, 5 responses): 4 records, 0 new,"
        " 4 changed, 0 revived, 0 unchanged; left out: 0 held by a later window, 0 deleted at"
        " the source, 0 expired, 0 forgotten; 0 calls to the source\n"
    )
    assert "no embedding provider" in result.err
    assert authors(world.url) == {"Ann Example", "Bo Sample", "Cy Placeholder", "Dee Sample"}
    assert world.client.calls == calls
    assert query(
        world.url,
        "SELECT status, (report->'counts'->>'records_not_indexed')::int FROM run"
        " WHERE kind = 'replay'",
    ) == [("succeeded", 4)]


def test_replay_refuses_what_it_cannot_do(
    capsys: pytest.CaptureFixture[str], world: World, tmp_path: Path
) -> None:
    recipe = str(HEARTH_RECIPE_PATH)

    unknown = invoke(
        capsys, "replay", "--workspace", "nowhere", "--source", "hearth", "--recipe", recipe
    )
    assert unknown.code == EXIT_FAILURE
    assert unknown.err == "memini: no such workspace: see `memini source list nowhere`\n"

    broken = tmp_path / "broken.toml"
    broken.write_text('format = 1\nname = "hearth"\n')
    invalid = invoke(
        capsys, "replay", "--workspace", "acme", "--source", "hearth", "--recipe", str(broken)
    )
    assert invalid.code == EXIT_USAGE
    assert "not a valid recipe" in invalid.err

    unregister_parser(PARSER)
    missing = invoke(
        capsys, "replay", "--workspace", "acme", "--source", "hearth", "--recipe", recipe
    )
    assert missing.code == EXIT_USAGE
    assert missing.err == "memini: no parser named hearth.thread in this build\n"
    assert authors(world.url) == {"U0001", "U0002", "U0003", "U0004"}
