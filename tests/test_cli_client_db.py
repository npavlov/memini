"""The acceptance flow of the CLI as a client of the running service, against a real Postgres.

Acceptance of memini-00y: from the CLI, create two workspaces, add a repository to each, trigger a
sync and see the run report. ``memini migrate`` and ``memini bootstrap`` run on the database as
operator commands; every other command goes over HTTP to a real uvicorn on a free port, which
serves the application with the job worker and the git sync handler, as ``memini serve`` does, but
with the FakeEmbedder. The client commands get the service URL and the token file, and no
database URL. Run with `make test-integration`. Repositories and their files are synthetic, made
here with fixed dates.
"""

import asyncio
import json
import shutil
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from pydantic import SecretStr

from git_helpers import SourceRepo
from memini.api import create_app
from memini.cli import main
from memini.cli import sources as source_commands
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_DENIED, EXIT_FAILURE, EXIT_OK
from memini.cli.serve import background_jobs
from memini.config import Settings, environment_variables
from memini.db import open_pool
from memini.embeddings import FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, git_sync_handler

pytestmark = [
    pytest.mark.integration,
    pytest.mark.git,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

LAMP = '''"""A desk lamp."""


def switch_on(lamp: dict[str, bool]) -> None:
    """Turn the lamp on."""
    lamp["on"] = True
'''

POST = """export function slugify(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}
"""


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def running_service(settings: Settings, embedder: FakeEmbedder) -> Generator[str]:
    """Serve the application, the job worker and the scheduler on a free port in a thread of its
    own, with its own event loop and pool, until the block ends; yield the service URL."""
    port = free_port()
    state: dict[str, Any] = {}

    async def serve() -> None:
        async with open_pool(
            settings.database_url.get_secret_value(), min_size=1, max_size=8, timeout=10
        ) as pool:
            app = create_app(settings, pool=pool, embedder=embedder)
            handlers = {
                GIT_SYNC: git_sync_handler(
                    pool, MirrorManager(GitOptions.from_settings(settings)), embedder
                )
            }
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                lifespan="on",
                log_config=None,
                access_log=False,
            )
            server = uvicorn.Server(config)
            state["server"] = server
            async with background_jobs(pool, handlers):
                await server.serve()

    def run() -> None:
        try:
            asyncio.run(serve())
        except BaseException as error:  # reported by the test thread
            state["error"] = error

    thread = threading.Thread(target=run, name="memini-test-service", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while True:
        server: uvicorn.Server | None = state.get("server")
        if server is not None and server.started:
            break
        if not thread.is_alive() or time.monotonic() > deadline:
            raise AssertionError(f"the service did not start: {state.get('error')!r}")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=60)
        assert not thread.is_alive(), "the service did not stop"
    if "error" in state:
        raise state["error"]


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Callable[..., Run]:
    def invoke(*argv: str) -> Run:
        capsys.readouterr()
        code = main(list(argv))
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return invoke


def ok(result: Run) -> Run:
    assert result.code == EXIT_OK, result.err
    return result


def test_from_the_cli_two_workspaces_each_with_a_repository_synced_and_reported(
    cli: Callable[..., Run],
    monkeypatch: pytest.MonkeyPatch,
    empty_database_url: str,
    tmp_path: Path,
) -> None:
    for name in (*environment_variables(), *CLIENT_VARIABLES, "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(source_commands, "POLL_INTERVAL", 0.2)
    repos = tmp_path / "repos"
    shop = SourceRepo(repos / "shop")
    shop.commit({"src/lamp.py": LAMP, "README.md": "# Shop\n\nSells lamps.\n"})
    blog = SourceRepo(repos / "blog")
    blog.commit({"src/post.ts": POST, "notes/draft.txt": "not indexed\n"})

    # Operator commands, on the database: migrate, then the first admin token.
    monkeypatch.setenv("MEMINI_DATABASE_URL", empty_database_url)
    ok(cli("migrate"))
    admin = ok(cli("bootstrap")).out.strip()
    token_file = tmp_path / "admin.token"
    token_file.write_text(f"{admin}\n", encoding="utf-8")
    token_file.chmod(0o600)

    settings = Settings(
        database_url=SecretStr(empty_database_url),
        git_mirror_dir=tmp_path / "mirrors",
        git_local_roots=(repos,),
    )
    with running_service(settings, FakeEmbedder()) as url:
        # Client commands: the service URL and the token file, no database URL.
        monkeypatch.delenv("MEMINI_DATABASE_URL")
        monkeypatch.setenv("MEMINI_URL", url)
        monkeypatch.setenv("MEMINI_TOKEN_FILE", str(token_file))

        ok(cli("workspace", "create", "alpha"))
        ok(cli("workspace", "create", "beta"))
        workspaces = ok(cli("workspace", "list"))
        added_shop = ok(
            cli("source", "add", "alpha", "shop", "--location", str(shop.path), "--ref", "main")
        )
        ok(
            cli(
                "source", "add", "beta", "blog", "--location", str(blog.path), "--ref", "main",
                "--include", "src/**", "--schedule", "1d",
            )
        )  # fmt: skip
        pushed = ok(cli("source", "add", "beta", "notes", "--kind", "push"))
        sources = ok(cli("source", "list", "beta"))

        synced = cli("sync", "alpha", "shop", "--wait")
        queued = ok(cli("sync", "beta", "blog"))
        blog_runs = wait_for_run(cli, "beta")

        runs = ok(cli("runs", "alpha"))
        run_id = runs.out.splitlines()[1].split()[0]
        report = ok(cli("run", run_id))
        report_json = json.loads(ok(cli("run", run_id, "--json")).out)
        streams = ok(cli("streams", "alpha", "shop"))
        found = ok(cli("search", "--code", "switch_on", "--mode", "exact"))
        everywhere = ok(cli("search", "lamps", "--workspace", "alpha"))

        # A read token limited to beta: it sees beta's runs, not alpha's, and changes nothing.
        reader = ok(cli("token", "create", "--scope", "read", "--workspace", "beta")).out.strip()
        tokens = ok(cli("token", "list"))
        monkeypatch.delenv("MEMINI_TOKEN_FILE")
        monkeypatch.setenv("MEMINI_TOKEN", reader)
        reader_beta = ok(cli("runs", "beta"))
        reader_alpha = cli("runs", "alpha")
        reader_sync = cli("sync", "beta", "blog")

    assert [line.split()[0] for line in workspaces.out.splitlines()] == ["NAME", "alpha", "beta"]
    assert ["location", str(shop.path.resolve())] in [
        line.split() for line in added_shop.out.splitlines()
    ]
    assert sources.out.splitlines()[1].split()[:6] == [
        "blog",
        "git",
        "refs/heads/main",
        "every",
        "1d",
        str(blog.path.resolve()),
    ]
    assert sources.out.splitlines()[2].split() == ["notes", "push", "-", "when", "triggered", "-"]
    assert "/v1/workspaces/beta/sources/notes/records" in pushed.err

    assert synced.code == EXIT_OK, synced.err
    assert synced.out.splitlines()[0].startswith("refs/heads/main: sync queued, job ")
    assert synced.out.splitlines()[1] == f"refs/heads/main: run {run_id} succeeded"
    assert queued.out.startswith("refs/heads/main: sync queued, job ")
    assert blog_runs["runs"][0]["status"] == "succeeded"
    assert blog_runs["runs"][0]["source"] == "blog"

    header, row = runs.out.splitlines()
    assert header.split()[:5] == ["ID", "KIND", "STATUS", "SOURCE", "STREAM"]
    assert row.split()[1:5] == ["git_sync", "succeeded", "shop", "refs/heads/main"]

    lines = report.out.splitlines()
    assert lines[:6] == [
        f"run        {run_id}",
        "kind       git_sync",
        "status     succeeded",
        "workspace  alpha",
        "source     shop",
        "stream     refs/heads/main",
    ]
    assert "steps" in lines
    assert any(line.split()[:2] == ["index", "succeeded"] for line in lines)
    assert "counts" in lines
    assert any(line.split() == ["files_added", "2"] for line in lines)
    assert report_json["report"]["version"] == 1
    assert report_json["report"]["counts"]["files_added"] == 2

    stream = streams.out.splitlines()[1].split()
    assert stream[0] == "refs/heads/main"
    assert "succeeded" in stream
    assert stream[-1] == run_id

    assert found.out.splitlines()[0].startswith("shop@")
    assert ":src/lamp.py#L" in found.out.splitlines()[0]
    assert "(code_file, alpha/shop," in found.out
    assert "alpha/shop" in everywhere.out
    assert "beta/" not in everywhere.out

    assert reader.startswith("memini_")
    assert reader not in tokens.out + tokens.err
    assert admin not in tokens.out + tokens.err
    assert reader_beta.out.splitlines()[1].split()[1:4] == ["git_sync", "succeeded", "blog"]
    assert reader_alpha.code == EXIT_FAILURE
    assert reader_alpha.err.startswith("memini: No such workspace. (not_found, HTTP 404, request")
    assert reader_sync.code == EXIT_DENIED
    assert "(insufficient_scope, HTTP 403" in reader_sync.err


def wait_for_run(cli: Callable[..., Run], workspace: str) -> dict[str, Any]:
    """Poll ``memini runs WORKSPACE --json`` until the newest run has finished."""
    deadline = time.monotonic() + 60
    while True:
        body: dict[str, Any] = json.loads(ok(cli("runs", workspace, "--json")).out)
        runs: list[dict[str, Any]] = body["runs"]
        if runs and runs[0]["finished_at"] is not None:
            return body
        assert time.monotonic() < deadline, f"no finished run in {workspace}: {body}"
        time.sleep(0.2)
