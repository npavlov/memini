"""``memini decision`` as a client of the running service, against a real Postgres.

The service runs as in test_cli_client_db.py (uvicorn on a free port, the FakeEmbedder). From the
CLI: record a decision and an open question with evidence by source and external id and by record
id, correct the decision with another that replaces it, answer the question, close and retract,
and see the journal refuse a change to an item that is no longer active (409, the constraint
named) and a token without the ``ingest`` scope (403). Run with `make test-integration`. Every
record, person and statement is synthetic.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
from pydantic import SecretStr

from ingest_helpers import HOUR, T0, iso, thread_payload
from memini.cli import main
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_DENIED, EXIT_FAILURE, EXIT_OK
from memini.config import Settings, environment_variables
from memini.embeddings import FakeEmbedder
from test_cli_client_db import Run, running_service

pytestmark = pytest.mark.integration

SAID = T0 + 9 * HOUR


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


def as_json(result: Run) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(ok(result).out)
    return body


def push(url: str, token: str) -> None:
    """Two threads and a document into the push source notes of workspace alpha."""
    records = [
        {
            "kind": "thread",
            "external_id": external_id,
            "occurred_at": iso(at),
            "payload": thread_payload(at, text),
        }
        for external_id, at, text in (
            ("garden/0001", SAID, "Water the beds at six?"),
            ("garden/0002", SAID + 24 * HOUR, "Seven, then. Who waters on Sundays?"),
        )
    ]
    records.append(
        {
            "kind": "document",
            "external_id": "handbook",
            "occurred_at": iso(T0),
            "payload": {"title": "Greenhouse handbook", "text": "# Watering\n\nEvery morning.\n"},
        }
    )
    response = httpx2.post(
        f"{url}/v1/workspaces/alpha/sources/notes/records",
        json={"records": records},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert response.status_code == 200, response.text


def add_person(database_url: str, workspace: str, name: str) -> str:
    with psycopg.connect(database_url, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO person (workspace_id, owner_id, display_name)"
            " SELECT id, owner_id, %s FROM workspace WHERE name = %s RETURNING id",
            (name, workspace),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_from_the_cli_decisions_are_recorded_corrected_answered_closed_and_retracted(
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

    settings = Settings(
        database_url=SecretStr(empty_database_url), git_mirror_dir=tmp_path / "mirrors"
    )
    with running_service(settings, FakeEmbedder()) as url:
        monkeypatch.delenv("MEMINI_DATABASE_URL")
        monkeypatch.setenv("MEMINI_URL", url)
        monkeypatch.setenv("MEMINI_TOKEN_FILE", str(admin_file))
        ok(cli("workspace", "create", "alpha"))
        ok(cli("source", "add", "alpha", "notes", "--kind", "push"))
        writer = ok(cli("token", "create", "--scope", "read", "--scope", "ingest")).out.strip()
        reader = ok(cli("token", "create", "--scope", "read")).out.strip()
        push(url, writer)
        person = add_person(empty_database_url, "alpha", "Person One")
        monkeypatch.delenv("MEMINI_TOKEN_FILE")
        monkeypatch.setenv("MEMINI_TOKEN", writer)

        # A decision with evidence by source and external id, and a question.
        wrong = as_json(
            cli(
                "decision", "record", "alpha", "--statement", "Water the beds at six.",
                "--decided-at", iso(SAID), "--evidence", "notes:garden/0001#m2",
                "--participant", person, "--json",
            )
        )  # fmt: skip
        question = as_json(
            cli(
                "decision", "record", "alpha", "--kind", "open_question",
                "--statement", "Who waters on Sundays?", "--decided-at", iso(SAID + 24 * HOUR),
                "--evidence", "notes:garden/0002#m2", "--json",
            )
        )  # fmt: skip
        # The correction cites the record by id, and the handbook whole; it replaces the first.
        thread = wrong["evidence"][0]["record_id"]
        fixed = ok(
            cli(
                "decision", "record", "alpha", "--statement", "Water the beds at seven, not six.",
                "--decided-at", iso(SAID + 25 * HOUR), "--evidence", f"{thread}#m2",
                "--evidence", "notes:handbook", "--supersedes", wrong["id"],
            )
        )  # fmt: skip
        fixed_id = fixed.out.splitlines()[0].split()[1]
        answered = as_json(
            cli(
                "decision", "supersede", "alpha", fixed_id, "--supersedes",
                f"{question['id']}:answers", "--json",
            )
        )  # fmt: skip
        later = as_json(
            cli(
                "decision", "record", "alpha", "--statement", "Water by hand in a heatwave.",
                "--evidence", "notes:handbook", "--json",
            )
        )  # fmt: skip
        closed = ok(cli("decision", "close", "alpha", later["id"]))
        again = cli("decision", "retract", "alpha", later["id"])
        retracted = as_json(cli("decision", "retract", "alpha", fixed_id, "--json"))
        inactive = cli("decision", "supersede", "alpha", fixed_id, "--supersedes", wrong["id"])
        unknown = cli("decision", "close", "alpha", "0193a1b2-0000-7000-8000-00000000dead")

        monkeypatch.setenv("MEMINI_TOKEN", reader)
        denied = cli("decision", "close", "alpha", question["id"])

    assert (wrong["kind"], wrong["status"], wrong["manual"]) == ("decision", "active", True)
    assert wrong["evidence"][0]["source"] == "notes"
    assert wrong["evidence"][0]["anchor"] == "m2"
    assert [p["display_name"] for p in wrong["participants"]] == ["Person One"]
    assert question["kind"] == "open_question"

    lines = fixed.out.splitlines()
    assert lines[0].split()[0] == "id"
    assert lines[3].split() == ["status", "active"]
    assert lines[4].split(maxsplit=1) == ["statement", "Water the beds at seven, not six."]
    assert lines[5].split() == ["decided", "2026-03-02T10:00:00Z"]
    assert ["evidence", "notes:garden/0001#m2", "(thread,", "stored)"] in [
        line.split() for line in lines
    ]
    assert ["notes:handbook", "(document,", "stored)"] in [line.split() for line in lines]
    assert ["supersedes", wrong["id"], "(replaces)"] in [line.split() for line in lines]

    assert [(link["supersedes_id"], link["relation"]) for link in answered["supersedes"]] == [
        (wrong["id"], "replaces"),
        (question["id"], "answers"),
    ]
    assert ["status", "closed"] in [line.split() for line in closed.out.splitlines()]
    assert again.code == EXIT_FAILURE
    assert again.out == ""
    assert f"Knowledge item {later['id']} is closed, not active" in again.err
    assert "(constraint knowledge_item_status_transition). (not_active, HTTP 409" in again.err
    assert retracted["status"] == "retracted"
    assert inactive.code == EXIT_FAILURE
    assert "(not_active, HTTP 409" in inactive.err
    assert "constraint knowledge_supersession_active_check" in inactive.err
    assert unknown.code == EXIT_FAILURE
    assert unknown.err.startswith("memini: No such knowledge item. (not_found, HTTP 404")
    assert denied.code == EXIT_DENIED
    assert "(insufficient_scope, HTTP 403" in denied.err
