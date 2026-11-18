"""``memini push`` against the running service and a real Postgres: a chat thread, a mail thread, a
meeting and a document pushed from a file, read back as they were sent; pushed again, unchanged; an
edited message and a later reply, updated; a tombstone, deleted; a record the service would refuse,
rejected while the others are stored.

The service runs under uvicorn on a free port in a thread, with the FakeEmbedder; the client
commands get its URL and a token file, and no database URL. The records are the examples of
docs/intake.md (docs/examples, synthetic) and a chat thread written here, in Russian and English.
"""

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import httpx2
import pytest
import uvicorn
from pydantic import SecretStr

from memini.api import create_app
from memini.cli import main
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_FAILURE, EXIT_OK
from memini.config import Settings, environment_variables
from memini.db import open_pool
from memini.embeddings import FakeEmbedder

pytestmark = [pytest.mark.integration]

EXAMPLES: Final = Path(__file__).resolve().parents[1] / "docs" / "examples"

type Json = dict[str, Any]


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


def example(name: str) -> Json:
    record: Json = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    return record


def chat_thread(*, edited: bool = False) -> Json:
    """A thread of #release with a root message and replies; ``edited``: its second message has a
    new text and a third message came later."""
    messages: list[Json] = [
        {
            "id": "1793700000.000100",
            "author": "Lev Primerov",
            "sent_at": "2026-11-05T10:00:00Z",
            "text": "Выпускаем 2.4 в четверг или ждём исправления экспорта?",
        },
        {
            "id": "1793700300.000200",
            "author": "Ana Example",
            "sent_at": "2026-11-05T10:05:00Z",
            "mentions": ["Lev Primerov"],
            "text": "Ждём исправления, выпускаем в пятницу.",
        },
    ]
    if edited:
        messages[1]["text"] = "Ждём исправления, выпускаем в понедельник."
        messages.append(
            {
                "id": "1793701200.000300",
                "author": "Lev Primerov",
                "sent_at": "2026-11-05T10:20:00Z",
                "text": "Agreed, Monday.",
            }
        )
    return {
        "kind": "thread",
        "external_id": "C0RELEASE/1793700000.000100",
        "occurred_at": messages[-1]["sent_at"],
        "external_url": "https://chat.example.invalid/archives/C0RELEASE/p1793700000000100",
        "payload": {
            "channel": "release",
            "people": [
                {"name": "Lev Primerov", "chat_user_id": "U03LEV"},
                {"name": "Ana Example", "chat_user_id": "U01ANA"},
            ],
            "messages": messages,
        },
    }


def write_lines(path: Path, *records: Json) -> str:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return str(path)


def test_from_the_cli_records_of_every_kind_are_pushed_read_back_and_updated(
    cli: Callable[..., Run],
    monkeypatch: pytest.MonkeyPatch,
    empty_database_url: str,
    tmp_path: Path,
) -> None:
    for name in (*environment_variables(), *CLIENT_VARIABLES, "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", empty_database_url)
    ok(cli("migrate"))
    admin_file = tmp_path / "admin.token"
    admin_file.write_text(ok(cli("bootstrap")).out, encoding="utf-8")
    admin = admin_file.read_text(encoding="utf-8").strip()

    with running_service(Settings(database_url=SecretStr(empty_database_url))) as url:
        monkeypatch.delenv("MEMINI_DATABASE_URL")
        monkeypatch.setenv("MEMINI_URL", url)
        monkeypatch.setenv("MEMINI_TOKEN_FILE", str(admin_file))
        ok(cli("workspace", "create", "acme"))
        ok(cli("source", "add", "acme", "inbox", "--kind", "push"))
        pusher = tmp_path / "ingest.token"
        pusher.write_text(
            ok(cli("token", "create", "--scope", "ingest", "--workspace", "acme")).out,
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMINI_TOKEN_FILE", str(pusher))

        def push(*argv: str) -> Run:
            return cli("push", "--workspace", "acme", "--source", "inbox", *argv)

        def read(kind: str, external_id: str, **params: str) -> Json:
            answer = httpx2.get(
                f"{url}/v1/workspaces/acme/sources/inbox/{kind}/{quote(external_id, safe='')}",
                params=params,
                headers={"Authorization": f"Bearer {admin}"},
                timeout=30,
            )
            assert answer.status_code == 200, answer.text
            body: Json = answer.json()
            return body

        mail, meeting, document = (
            example("mail-thread.json"),
            example("meeting.json"),
            example("document.json"),
        )
        records = write_lines(tmp_path / "records.jsonl", chat_thread(), mail, meeting, document)

        # Every kind, from one file of JSON lines.
        first = ok(push(records))
        assert first.out.splitlines() == [
            "sent 4 records in 1 batch to acme/inbox",
            "stored 4 (inserted 4, updated 0, revived 0), deleted 0, unchanged 0",
        ]

        # Read back as they were sent: texts, names and times verbatim.
        chat = read("threads", chat_thread()["external_id"])
        assert chat["channel"] == "release"
        assert [m["text"] for m in chat["messages"]] == [
            m["text"] for m in chat_thread()["payload"]["messages"]
        ]
        assert chat["messages"][1]["mentions"] == ["Lev Primerov"]
        mail_back = read("threads", mail["external_id"])
        assert mail_back["title"] == "Retention of the archive bucket"
        sent = mail["payload"]["messages"]
        assert [
            (m["author"], m["recipients"], m["cc"], m["text"]) for m in mail_back["messages"]
        ] == [(m["author"], m["recipients"], m["cc"], m["text"]) for m in sent]
        notes = read("meetings", meeting["external_id"], include_transcript="true")
        assert notes["title"] == "Platform weekly"
        assert notes["participants"] == meeting["payload"]["participants"]
        assert notes["summary"] == meeting["payload"]["summary"]
        assert [item["owner"] for item in notes["action_items"]] == ["Ben Sample", None]
        assert [s["text"] for s in notes["transcript"]] == [
            s["text"] for s in meeting["payload"]["transcript"]
        ]

        # The same records again: nothing changes.
        again = ok(push(records))
        assert "stored 0 (inserted 0, updated 0, revived 0), deleted 0, unchanged 4" in again.out

        # An edited message and a later reply: the thread is pushed whole again and updated.
        edited = write_lines(tmp_path / "edited.jsonl", chat_thread(edited=True))
        updated = ok(push(edited))
        assert "stored 1 (inserted 0, updated 1, revived 0), deleted 0, unchanged 0" in updated.out
        chat = read("threads", chat_thread()["external_id"])
        assert [m["text"] for m in chat["messages"]] == [
            "Выпускаем 2.4 в четверг или ждём исправления экспорта?",
            "Ждём исправления, выпускаем в понедельник.",
            "Agreed, Monday.",
        ]

        # A tombstone deletes; a record the service would refuse is rejected, the others stored.
        late = example("document.json") | {"external_id": "wiki/runbooks/archive-bucket"}
        broken = example("meeting.json") | {"external_id": "notes/broken", "occurred_at": "today"}
        mixed = write_lines(
            tmp_path / "mixed.jsonl",
            {"kind": "document", "external_id": document["external_id"], "deleted": True},
            broken,
            late,
        )
        partly = push(mixed)
        assert partly.code == EXIT_FAILURE, partly.err
        assert partly.out.splitlines()[:2] == [
            "sent 1 record and 1 tombstone in 1 batch to acme/inbox",
            "stored 1 (inserted 1, updated 0, revived 0), deleted 1, unchanged 0",
        ]
        assert "  line 2 (meeting notes/broken): occurred_at:" in partly.out

        # The batch of the install guide, with its stream; the stream is created.
        guide = ok(push("--json", str(EXAMPLES / "thread.json")))
        summary = json.loads(guide.out)
        assert (summary["stream"], summary["counts"]["inserted"]) == ("chat/platform", 1)
        assert len(summary["runs"]) == 1
        assert summary["rejected"] == []
        monkeypatch.setenv("MEMINI_TOKEN_FILE", str(admin_file))
        assert "chat/platform" in ok(cli("streams", "acme", "inbox")).out
