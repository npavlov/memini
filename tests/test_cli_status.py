"""``memini status`` against a fake service, and ``GET /v1/status`` without a database.

The fake service answers what :class:`memini.status.ServiceStatus` serializes to, so the command
reads the fields the route really sends. The database-backed path is in tests/test_status_db.py.
Every value is synthetic; the token is generated per run.
"""

import itertools
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from memini.api import create_app
from memini.auth import Principal, Scope, generate_token
from memini.cli import main
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_OK
from memini.config import Settings, environment_variables
from memini.status import (
    DatabaseStatus,
    GitPin,
    IndexStatus,
    ServiceStatus,
    StreamRun,
    StreamStatus,
    WorkspaceIndex,
    WorkspaceStatus,
)

TOKEN = "memini_" + secrets.token_urlsafe(32)
AS_OF = datetime(2026, 11, 18, 16, 2, 3, tzinfo=UTC)
RUN = uuid.UUID("0193a1b2-0000-7000-8000-00000000c0de")
COMMIT = "c0ffee" + "0" * 34

type Json = dict[str, Any]


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.Request]:
    """The token in MEMINI_TOKEN and no other MEMINI_* setting; the requests the CLI sent."""
    for name in (*environment_variables(), *CLIENT_VARIABLES, "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_TOKEN", TOKEN)
    return []


@pytest.fixture
def cli(
    capsys: pytest.CaptureFixture[str], answers: list[httpx2.Request]
) -> Callable[[Json, str], Run]:
    def invoke(body: Json, *argv: str) -> Run:
        def handle(request: httpx2.Request) -> httpx2.Response:
            answers.append(request)
            return httpx2.Response(200, json=body)

        capsys.readouterr()
        code = main(["status", *argv], transport=httpx2.MockTransport(handle))
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return invoke


def index(
    state: str = "ok",
    *,
    records: int = 0,
    not_indexed: int = 0,
    behind: bool = False,
    changed: list[str] | None = None,
    reindex: str | None = None,
) -> WorkspaceIndex:
    return WorkspaceIndex.model_validate(
        {
            "state": state,
            "built_with": None if state in ("none", "off") else "model fake (dimension 8)",
            "changed": changed or [],
            "records": records,
            "records_not_indexed": not_indexed,
            "chunks": 2 * records,
            "behind": behind,
            "reindex": reindex,
        }
    )


def a_status() -> Json:
    failed = StreamRun(
        id=RUN,
        kind="git_sync",
        status="failed",
        started_at=AS_OF,
        finished_at=AS_OF,
        error="GitCommandError: git fetch failed (exit 128)",
    )
    succeeded = StreamRun(
        id=RUN, kind="ingest", status="succeeded", started_at=AS_OF, finished_at=AS_OF
    )
    code = StreamStatus(
        source="code",
        source_kind="git",
        key="refs/heads/main",
        schedule_seconds=900.0,
        checked_until=AS_OF,
        lag_seconds=7200.0,
        current=False,
        behind=["lag"],
        last_run=failed,
        failures=3,
        last_failure=failed,
        git=GitPin(commit=COMMIT, partial=True),
        records=40,
    )
    chat = StreamStatus(
        source="chat",
        source_kind="push",
        key="general",
        schedule_seconds=None,
        checked_until=AS_OF,
        lag_seconds=60.0,
        current=False,
        behind=["not_indexed"],
        last_run=succeeded,
        checked_from=datetime(2026, 10, 1, tzinfo=UTC),
        records=5,
        records_not_indexed=2,
    )
    mail = StreamStatus(
        source="mail",
        source_kind="mcp",
        key="inbox",
        schedule_seconds=3600.0,
        checked_until=AS_OF,
        lag_seconds=30.0,
        current=True,
        last_run=None,
        checked_from=datetime(2026, 10, 1, tzinfo=UTC),
        history_start=datetime(2025, 11, 18, tzinfo=UTC),
        history_loaded=False,
        records=0,
    )
    status = ServiceStatus(
        version="9.9.9",
        as_of=AS_OF,
        current=False,
        database=DatabaseStatus(reachable=True, schema_version=12, expected_schema_version=12),
        workspaces=[
            WorkspaceStatus(
                id=uuid.UUID("00000000-0000-7000-8000-0000000000a1"),
                name="alpha",
                streams=[chat, code, mail],
                index=index(
                    records=45,
                    not_indexed=2,
                    behind=True,
                    reindex="memini reindex --workspace alpha",
                ),
            ),
            WorkspaceStatus(
                id=uuid.UUID("00000000-0000-7000-8000-0000000000b2"),
                name="beta",
                streams=[],
                index=index(
                    "stale",
                    behind=True,
                    changed=["model"],
                    reindex="memini reindex --workspace beta",
                ),
            ),
        ],
        index=IndexStatus(
            state="stale", configured="model fake (dimension 8)", note="Stale in beta."
        ),
    )
    body: Json = status.model_dump(mode="json")
    return body


def test_status_prints_the_verdict_a_row_per_stream_and_what_needs_a_person(
    cli: Callable[..., Run], answers: list[httpx2.Request]
) -> None:
    result = cli(a_status())

    assert result.code == EXIT_OK
    assert answers[0].method == "GET"
    assert answers[0].url.path == "/v1/status"
    assert answers[0].headers["Authorization"] == f"Bearer {TOKEN}"
    lines = result.out.splitlines()
    assert lines[:5] == [
        "current   no",
        "as of     2026-11-18T16:02Z",
        "version   9.9.9",
        "database  reachable, schema version 12 (this version expects 12)",
        "index     stale: Stale in beta.",
    ]
    assert lines[5] == ""
    header, *rows = lines[6:10]
    assert header.split() == [
        "WORKSPACE",
        "SOURCE",
        "STREAM",
        "LAST",
        "RESULT",
        "LAG",
        "HISTORY",
        "FROM",
        "INDEX",
        "CURRENT",
    ]
    columns = [header.index(name) for name in ("LAST RESULT", "LAG", "HISTORY", "INDEX", "CURRENT")]

    def cells(row: str) -> list[str]:
        edges = [0, header.index("SOURCE"), header.index("STREAM"), *columns, len(row) + 1]
        return [row[start:end].strip() for start, end in itertools.pairwise(edges)]

    chat, code, mail = (cells(row) for row in rows)
    assert chat == [
        "alpha",
        "chat",
        "general",
        "succeeded 2026-11-18T16:02Z",
        "1m",
        "2026-10-01T00:00Z",
        "2 of 5 not indexed",
        "no (not indexed)",
    ]
    assert code == [
        "alpha",
        "code",
        "refs/heads/main",
        "failed 2026-11-18T16:02Z (3 failed)",
        "2h",
        f"{COMMIT[:12]} (partial)",
        "ok",
        "no (lag)",
    ]
    assert mail == [
        "alpha",
        "mail",
        "inbox",
        "never ran",
        "30s",
        "2026-10-01T00:00Z (loading to 2025-11-18T00:00Z)",
        "-",
        "yes",
    ]
    assert lines[10:] == [
        "",
        "failing:",
        f"  alpha/code refs/heads/main: failed 3 times in a row, last at 2026-11-18T16:02Z (run"
        f" {RUN}): GitCommandError: git fetch failed (exit 128)",
        "",
        "index behind:",
        "  alpha: index ok, 2 records not indexed; run `memini reindex --workspace alpha`",
        "  beta: index stale (changed: model); run `memini reindex --workspace beta`",
    ]
    assert result.err == ""


def test_status_json_is_the_answer(cli: Callable[..., Run]) -> None:
    body = a_status()

    result = cli(body, "--json")

    assert result.code == EXIT_OK
    assert json.loads(result.out) == body


def test_status_without_streams_says_so_and_still_names_what_is_behind(
    cli: Callable[..., Run],
) -> None:
    body = a_status()
    for workspace in body["workspaces"]:
        workspace["streams"] = []

    result = cli(body)

    assert result.code == EXIT_OK
    assert "WORKSPACE" not in result.out
    assert "no streams yet" in result.err
    assert "  beta: index stale (changed: model); run `memini reindex --workspace beta`" in (
        result.out.splitlines()
    )


def test_status_escapes_what_a_terminal_would_take_for_control_sequences(
    cli: Callable[..., Run],
) -> None:
    body = a_status()
    stream = body["workspaces"][0]["streams"][1]
    stream["last_failure"]["error"] = "RuntimeError: \x1b[2Jcleared"

    result = cli(body)

    assert "\x1b" not in result.out


# --- The route without a database -----------------------------------------------------------------


class FakeAuthenticator:
    """Resolves the tokens it issued."""

    def __init__(self) -> None:
        self.tokens: dict[str, Principal] = {}

    def issue(self, *scopes: Scope) -> dict[str, str]:
        token = generate_token()
        self.tokens[token] = Principal(
            principal_id=uuid.uuid4(), token_id=uuid.uuid4(), scopes=frozenset(scopes)
        )
        return {"Authorization": f"Bearer {token}"}

    async def __call__(self, token: str) -> Principal:
        return self.tokens[token]


def test_the_route_needs_the_read_scope_and_reports_a_missing_database(
    settings: Settings,
) -> None:
    authenticator = FakeAuthenticator()
    client = TestClient(create_app(settings, authenticator=authenticator))

    refused = client.get("/v1/status", headers=authenticator.issue("admin", "ingest"))
    assert refused.status_code == 403
    assert refused.headers["WWW-Authenticate"].endswith('scope="read"')

    answered = client.get("/v1/status", headers=authenticator.issue("read"))
    assert answered.status_code == 200
    body = answered.json()
    assert body["current"] is False
    assert body["database"]["reachable"] is False
    assert body["workspaces"] == []
    assert body["index"]["state"] == "off"
    assert client.get("/v1/status").status_code == 401


def test_the_schema_documents_the_route(settings: Settings) -> None:
    schema = TestClient(create_app(settings)).get("/openapi.json").json()

    responses = schema["paths"]["/v1/status"]["get"]["responses"]
    assert "200" in responses
    assert "default" in responses
    for model in ("ServiceStatus", "StreamStatus", "StreamRun", "WorkspaceIndex", "RunSummary"):
        assert model in schema["components"]["schemas"]
