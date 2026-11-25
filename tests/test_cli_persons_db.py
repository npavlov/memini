"""``memini person …`` against the running service and a real Postgres: the acceptance flow of
memini-yq0 from the CLI. The service runs under uvicorn on a free port in a thread; the client
commands get its URL and a token file, and no database URL. Every name and address is made up."""

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from pydantic import SecretStr

from ingest_helpers import HOUR, T0, iso
from memini.api import create_app
from memini.cli import main
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_FAILURE, EXIT_OK
from memini.config import Settings, environment_variables
from memini.db import open_pool
from memini.embeddings import FakeEmbedder

pytestmark = [pytest.mark.integration]


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


@contextmanager
def running_service(settings: Settings) -> Generator[str]:
    """Serve the application on a free port in a thread of its own until the block ends."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    state: dict[str, Any] = {}

    async def serve() -> None:
        async with open_pool(
            settings.database_url.get_secret_value(), min_size=1, max_size=4, timeout=10
        ) as pool:
            app = create_app(settings, pool=pool, embedder=FakeEmbedder())
            config = uvicorn.Config(
                app, host="127.0.0.1", port=port, lifespan="on", log_config=None, access_log=False
            )
            server = uvicorn.Server(config)
            state["server"] = server
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


def thread(external_id: str, author: str, hours: int, people: list[dict[str, str]]) -> Any:
    at = T0 + hours * HOUR
    return {
        "kind": "thread",
        "external_id": external_id,
        "occurred_at": iso(at),
        "payload": {
            "people": people,
            "messages": [{"id": "m1", "author": author, "sent_at": iso(at), "text": "Ready."}],
        },
    }


def person_id(run: Run) -> str:
    """The id a person command printed first."""
    return run.out.split()[1]


def test_from_the_cli_mail_and_chat_become_one_person_and_split_again(
    cli: Callable[..., Run],
    monkeypatch: pytest.MonkeyPatch,
    empty_database_url: str,
    tmp_path: Path,
) -> None:
    for name in (*environment_variables(), *CLIENT_VARIABLES, "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", empty_database_url)
    ok(cli("migrate"))
    admin = ok(cli("bootstrap")).out.strip()
    token_file = tmp_path / "admin.token"
    token_file.write_text(f"{admin}\n", encoding="utf-8")
    token_file.chmod(0o600)

    with running_service(Settings(database_url=SecretStr(empty_database_url))) as url:
        monkeypatch.delenv("MEMINI_DATABASE_URL")
        monkeypatch.setenv("MEMINI_URL", url)
        monkeypatch.setenv("MEMINI_TOKEN_FILE", str(token_file))
        ok(cli("workspace", "create", "acme"))
        ok(cli("source", "add", "acme", "inbox", "--kind", "push"))
        pusher = ok(cli("token", "create", "--scope", "ingest")).out.strip()
        batch = {
            "records": [
                thread("mail-1", "Ann Example <ann@example.test>", 9, []),
                thread("chat-1", "Ann", 10, [{"name": "Ann", "chat_user_id": "U-ANN"}]),
            ]
        }
        pushed = httpx2.post(
            f"{url}/v1/workspaces/acme/sources/inbox/records",
            json=batch,
            headers={"Authorization": f"Bearer {pusher}"},
            timeout=30,
        )
        assert pushed.status_code == 200, pushed.text

        by_mail = person_id(ok(cli("person", "lookup", "acme", "email", "ANN@example.test")))
        by_chat = person_id(ok(cli("person", "lookup", "acme", "chat_user_id", "U-ANN")))
        assert by_mail != by_chat
        listed = ok(cli("person", "list", "acme"))
        assert {by_mail, by_chat} <= set(listed.out.split())

        added = ok(cli("person", "alias", "add", "acme", by_mail, "chat_user_id", "U-ANN"))
        assert f"memini person split acme {by_chat}" in added.err
        assert person_id(ok(cli("person", "lookup", "acme", "chat_user_id", "U-ANN"))) == by_mail
        assert by_chat not in ok(cli("person", "list", "acme")).out
        assert by_chat in ok(cli("person", "list", "acme", "--merged")).out

        ok(cli("person", "split", "acme", by_chat))
        assert person_id(ok(cli("person", "lookup", "acme", "chat_user_id", "U-ANN"))) == by_chat
        refused = cli("person", "split", "acme", by_chat)
        assert refused.code == EXIT_FAILURE
        assert "not_merged" in refused.err

        ok(cli("person", "merge", "acme", by_chat, "--into", by_mail))
        ok(cli("person", "me", "acme", "--set", by_chat))
        assert person_id(ok(cli("person", "me", "acme"))) == by_mail  # resolved through the merge
        ok(cli("person", "me", "acme", "--unset"))
        assert "no_me" in cli("person", "me", "acme").err
