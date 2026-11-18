"""``memini extract`` against a fake service: the arguments, the call and the output.

The service is an ``httpx2.MockTransport``, as in test_cli_client.py, whose helpers this module
uses; the route and the library against a real Postgres are in test_reextract_db.py. Every value
is synthetic.
"""

import json
from collections.abc import Callable

import httpx2
import pytest

from memini.cli import main
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_DENIED, EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from memini.config import environment_variables
from test_cli_client import TOKEN, FakeService, Json, Run, error, ok

EXTRACTOR = "qwen3:4b-instruct@decisions-v2"


def answer(*, dry_run: bool = False) -> Json:
    return {
        "workspace": "orchard",
        "dry_run": dry_run,
        "since": None,
        "totals": {"records": 9, "current": 2, "enqueued": 4, "queued": 1, "skipped": 2},
        "sources": [
            {
                "source": "chat",
                "extractor": EXTRACTOR,
                "skipped_reason": None,
                "records": 7,
                "current": 2,
                "enqueued": 4,
                "queued": 1,
                "skipped": 0,
            },
            {
                "source": "mail",
                "extractor": None,
                "skipped_reason": "policy_none",
                "records": 2,
                "current": 0,
                "enqueued": 0,
                "queued": 0,
                "skipped": 2,
            },
        ],
    }


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    """The token in MEMINI_TOKEN and no other MEMINI_* setting from the developer's shell."""
    for name in (*environment_variables(), *CLIENT_VARIABLES, "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_TOKEN", TOKEN)
    return FakeService()


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], service: FakeService) -> Callable[..., Run]:
    def invoke(*argv: str) -> Run:
        capsys.readouterr()
        code = main(list(argv), transport=httpx2.MockTransport(service))
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return invoke


def test_extract_enqueues_and_prints_the_counts(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok(answer(), 202))

    result = cli("extract", "orchard")

    assert result.code == EXIT_OK, result.err
    assert service.last.method == "POST"
    assert service.last.url.path == "/v1/workspaces/orchard/extract"
    assert json.loads(service.last.content) == {"dry_run": False}
    head, table = result.out.split("\n\n")
    fields = {line[:9].strip(): line[9:].strip() for line in head.splitlines()}
    assert fields["records"] == "9 live threads and meetings"
    assert fields["current"] == "2 read by the current extractor already"
    assert fields["enqueued"] == "4"
    assert fields["queued"] == "1 queued or running already"
    assert fields["skipped"].startswith("2 ")
    header, chat, mail = table.splitlines()
    assert header.split() == [
        "SOURCE",
        "EXTRACTOR",
        "RECORDS",
        "CURRENT",
        "ENQUEUED",
        "QUEUED",
        "SKIPPED",
    ]
    assert chat.split() == ["chat", EXTRACTOR, "7", "2", "4", "1", "0"]
    assert mail.split() == ["mail", "-", "(policy_none)", "2", "0", "0", "0", "2"]


def test_extract_sends_the_options(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(answer(dry_run=True)))

    result = cli(
        "extract",
        "orchard",
        "--source",
        "chat",
        "--source",
        "mail",
        "--source",
        "chat",
        "--since",
        "2026-03-01T00:00:00Z",
        "--dry-run",
    )

    assert result.code == EXIT_OK, result.err
    assert json.loads(service.last.content) == {
        "dry_run": True,
        "sources": ["chat", "mail"],
        "since": "2026-03-01T00:00:00+00:00",
    }
    assert "would enqueue" in result.out


def test_extract_json_prints_the_answer(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(answer(), 202))

    result = cli("extract", "orchard", "--json")

    assert result.code == EXIT_OK
    assert json.loads(result.out) == answer()


@pytest.mark.parametrize(
    "argv",
    [
        ("extract",),
        ("extract", "orchard", "--since", "2026-03-01T00:00:00"),
        ("extract", "orchard", "--source", "Not A Name"),
        ("extract", "orchard", "--all"),
    ],
)
def test_extract_refuses_wrong_arguments(
    cli: Callable[..., Run], service: FakeService, argv: tuple[str, ...]
) -> None:
    result = cli(*argv)

    assert result.code == EXIT_USAGE
    assert service.requests == []


def test_extract_reports_what_the_service_refuses(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(
        error(404, "not_found", "No such source."),
        error(403, "insufficient_scope", "The token lacks the admin scope."),
    )

    missing = cli("extract", "orchard", "--source", "nowhere")
    denied = cli("extract", "orchard")

    assert missing.code == EXIT_FAILURE
    assert "No such source." in missing.err
    assert denied.code == EXIT_DENIED
