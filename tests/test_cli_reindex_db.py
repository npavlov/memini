"""``memini reindex`` against a real Postgres. The embedder of the settings is replaced by the
FakeEmbedder; every value is synthetic."""

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from index_helpers import FILE_V1, add_record
from memini.cli import index as index_cli
from memini.cli import main
from memini.cli.common import EXIT_FAILURE, EXIT_OK
from memini.config import Settings, environment_variables
from memini.db import connect, migrate
from memini.embeddings import Embedder, FakeEmbedder
from memini.ids import RecordKind
from memini.index import current_fingerprint, read_index_state
from memini.ingest import canonical_json
from memini.workspaces import create_workspace, ensure_principal

pytestmark = pytest.mark.integration


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
    workspace: uuid.UUID
    owner: uuid.UUID
    repo: uuid.UUID
    notes: uuid.UUID


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, empty_database_url: str, tmp_path: Path) -> World:
    """A migrated database in ``MEMINI_DATABASE_URL`` with a workspace ``acme``, owned by the CLI's
    principal, holding a git and a push source; the FakeEmbedder in place of the configured one;
    an empty mirror directory."""
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    url = empty_database_url
    monkeypatch.setenv("MEMINI_DATABASE_URL", url)
    monkeypatch.setenv("MEMINI_GIT_MIRROR_DIR", str(tmp_path / "mirrors"))
    fake = FakeEmbedder()

    def create_embedder(settings: Settings) -> Embedder:
        return fake

    monkeypatch.setattr(index_cli, "create_embedder", create_embedder)

    async def build() -> World:
        await migrate(url)
        async with connect(url, autocommit=True) as conn:
            owner = await ensure_principal(conn, "owner")
            acme = await create_workspace(conn, owner_id=owner, name="acme")
            ids: list[uuid.UUID] = []
            for name, kind in (("greenhouse", "repository"), ("notes", "push")):
                cur = await conn.execute(
                    "INSERT INTO source (workspace_id, owner_id, name, kind)"
                    " VALUES (%s, %s, %s, %s) RETURNING id",
                    (acme.id, owner, name, kind),
                )
                row = await cur.fetchone()
                assert row is not None
                ids.append(row[0])
        return World(url, acme.id, owner, ids[0], ids[1])

    return asyncio.run(build())


def query(url: str, sql: Any, params: Any = None) -> list[tuple[Any, ...]]:
    async def run() -> list[tuple[Any, ...]]:
        async with connect(url) as conn:
            cur = await conn.execute(sql, params)
            return [tuple(row) for row in await cur.fetchall()]

    return asyncio.run(run())


T = "2026-03-01T09:00:00Z"


def add(world: World, kind: RecordKind) -> str:
    async def run() -> str:
        async with connect(world.url, autocommit=True) as conn:
            if kind == "code_file":
                return await add_record(
                    conn,
                    workspace_id=world.workspace,
                    owner_id=world.owner,
                    source_id=world.repo,
                    external_id="refs/heads/main:src/greenhouse.py",
                    content=FILE_V1,
                )
            return await add_record(
                conn,
                workspace_id=world.workspace,
                owner_id=world.owner,
                source_id=world.notes,
                external_id="note-1",
                content=canonical_json(
                    {"messages": [{"author": "Ann Example", "sent_at": T, "text": "A note."}]}
                ),
                kind=kind,
            )

    return asyncio.run(run())


def test_reindex_records_the_fingerprint_and_a_run(
    capsys: pytest.CaptureFixture[str], world: World
) -> None:
    add(world, "thread")

    result = invoke(capsys, "reindex", "--workspace", "acme")

    assert result.code == EXIT_OK, result.err
    assert result.out == (
        "rebuilt the index of workspace acme: 1 records (1 indexed), 1 chunks (1 new, 0 changed,"
        " 0 unchanged, 0 deleted), 1 texts embedded, 0 vectors reused\n"
    )
    assert result.err.startswith(
        "memini: rebuilding the index of workspace acme with model fake-embedder"
    )

    async def state() -> object:
        async with connect(world.url) as conn:
            found = await read_index_state(conn, world.workspace)
            return None if found is None else found.built_with

    assert asyncio.run(state()) == current_fingerprint(FakeEmbedder().spec)
    runs = query(world.url, "SELECT kind, status, report->'counts'->>'records_indexed' FROM run")
    assert runs == [("reindex", "succeeded", "1")]


def test_reindex_of_code_stops_without_the_mirror_and_changes_no_fingerprint(
    capsys: pytest.CaptureFixture[str], world: World
) -> None:
    rid = add(world, "code_file")

    result = invoke(capsys, "reindex", "--workspace", "acme")

    assert result.code == EXIT_FAILURE
    assert result.out == ""
    assert result.err.endswith(
        f"memini: code record {rid} cannot be read: source {world.repo} has no repository mirror"
        " yet; its next sync creates it; the rebuild of workspace acme stopped before it"
        " finished, and search stays as it was\n"
    )
    assert query(world.url, "SELECT count(*) FROM index_state") == [(0,)]
    assert query(world.url, "SELECT kind, status FROM run") == [("reindex", "failed")]


def test_reindex_of_an_unknown_workspace_exits_1(
    capsys: pytest.CaptureFixture[str], world: World
) -> None:
    result = invoke(capsys, "reindex", "--workspace", "nowhere")

    assert result.code == EXIT_FAILURE
    assert result.err == "memini: no workspace named nowhere; see `memini workspace list`\n"
