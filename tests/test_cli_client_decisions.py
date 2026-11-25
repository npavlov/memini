"""``memini decision record|supersede|close|retract`` against a fake service: the arguments, the
calls, the output and the refusals of the journal.

The service is an ``httpx2.MockTransport``, as in test_cli_client.py, whose helpers this module
uses; the flow against the real service is in test_cli_client_decisions_db.py. Every value is
synthetic.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from memini.cli import main
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_DENIED, EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from memini.config import environment_variables
from test_cli_client import TOKEN, FakeService, Handler, Json, Run, error, ok

ITEM_ID = "0193a1b2-0000-7000-8000-0000000000a1"
EARLIER_ID = "0193a1b2-0000-7000-8000-0000000000a0"
QUESTION_ID = "0193a1b2-0000-7000-8000-0000000000a2"
PERSON_ID = "0193a1b2-0000-7000-8000-0000000000b1"
WORKSPACE_ID = "0193a1b2-0000-7000-8000-0000000000c1"
SOURCE_ID = "0193a1b2-0000-7000-8000-0000000000d1"
RECORD_ID = "ab" * 32
COMMIT = "4f" * 20


def item(**changes: object) -> Json:
    """A decision as the service answers it."""
    body: Json = {
        "id": ITEM_ID,
        "workspace_id": WORKSPACE_ID,
        "workspace": "alpha",
        "kind": "decision",
        "statement": "Water the beds at seven, not six.",
        "occurred_at": "2026-03-01T10:00:00Z",
        "status": "active",
        "created_at": "2026-11-18T16:02:03.456789Z",
        "invalidated_at": None,
        "extractor": "manual",
        "manual": True,
        "extraction_id": None,
        "evidence": [
            {
                "record_id": RECORD_ID,
                "source_id": SOURCE_ID,
                "source": "chat",
                "record_kind": "thread",
                "external_id": "garden/0001",
                "external_url": None,
                "anchor": "m2",
                "record_state": "stored",
            },
            {
                "record_id": "cd" * 32,
                "source_id": SOURCE_ID,
                "source": None,
                "record_kind": "document",
                "external_id": "handbook",
                "external_url": None,
                "anchor": None,
                "record_state": "gone",
            },
        ],
        "participants": [{"person_id": PERSON_ID, "display_name": "Person One"}],
        "supersedes": [
            {
                "item_id": ITEM_ID,
                "supersedes_id": EARLIER_ID,
                "relation": "replaces",
                "created_at": "2026-11-18T16:02:03Z",
            }
        ],
        "superseded_by": None,
    }
    body.update(changes)
    return body


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


def fields(output: str) -> list[tuple[str, str]]:
    """``print_fields`` lines as (name, value); a continuation line has the name ''."""
    width = output.index(ITEM_ID) if ITEM_ID in output else 0
    return [(line[:width].strip(), line[width:]) for line in output.splitlines()]


# --- record --------------------------------------------------------------------------------------


def test_record_sends_the_item_and_prints_it(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(item(), 201))

    result = cli(
        "decision",
        "record",
        "alpha",
        "--statement",
        "Water the beds at seven, not six.",
        "--occurred-at",
        "2026-03-01T10:00:00Z",
        "--evidence",
        f"{RECORD_ID}#m2",
        "--evidence",
        f"{RECORD_ID.upper()}#00:12:30",
        "--evidence",
        f"{'ef' * 32}#{COMMIT}#L12-L30",
        "--evidence",
        "cd" * 32,
        "--participant",
        PERSON_ID,
        "--supersedes",
        EARLIER_ID,
        "--supersedes",
        f"{QUESTION_ID}:answers",
    )

    assert result.code == EXIT_OK, result.err
    assert result.err == ""
    request = service.last
    assert (request.method, request.url.path) == ("POST", "/v1/workspaces/alpha/decisions")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "kind": "decision",
        "statement": "Water the beds at seven, not six.",
        "occurred_at": "2026-03-01T10:00:00+00:00",
        "evidence": [
            {"record_id": RECORD_ID, "anchor": "m2"},
            {"record_id": RECORD_ID, "anchor": "00:12:30"},
            {"record_id": "ef" * 32, "anchor": f"{COMMIT}#L12-L30"},
            {"record_id": "cd" * 32},
        ],
        "participants": [PERSON_ID],
        "supersedes": [
            {"item_id": EARLIER_ID, "relation": "replaces"},
            {"item_id": QUESTION_ID, "relation": "answers"},
        ],
    }
    assert fields(result.out) == [
        ("id", ITEM_ID),
        ("workspace", "alpha"),
        ("kind", "decision"),
        ("status", "active"),
        ("statement", "Water the beds at seven, not six."),
        ("occurred", "2026-03-01T10:00:00Z"),
        ("created", "2026-11-18T16:02:03Z"),
        ("extractor", "manual"),
        ("evidence", f"{RECORD_ID}#m2  (chat:garden/0001, thread, stored)"),
        ("", f"{'cd' * 32}  (document, gone)"),
        ("participants", f"Person One ({PERSON_ID})"),
        ("supersedes", f"{EARLIER_ID} (replaces)"),
    ]


def test_record_defaults_to_a_decision_said_now(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok(item(participants=[], supersedes=[]), 201))
    before = datetime.now(UTC)

    result = cli("decision", "record", "alpha", "--statement", "Seven.", "--evidence", RECORD_ID)

    assert result.code == EXIT_OK, result.err
    body = json.loads(service.last.content)
    assert (body["kind"], body["participants"], body["supersedes"]) == ("decision", [], [])
    said = datetime.fromisoformat(body["occurred_at"])
    assert said.tzinfo is not None
    assert before <= said <= datetime.now(UTC) + timedelta(seconds=1)
    assert ("participants", "-") in fields(result.out)
    assert ("supersedes", "-") in fields(result.out)


def test_record_an_open_question_as_json(cli: Callable[..., Run], service: FakeService) -> None:
    answer = item(kind="open_question", statement="Who waters on Sundays?")
    service.answer(ok(answer, 201))

    result = cli(
        "decision",
        "record",
        "alpha",
        "--kind",
        "open_question",
        "--statement",
        "Who waters on Sundays?",
        "--occurred-at",
        "2026-03-01T11:00:00+02:00",
        "--evidence",
        f"{RECORD_ID}#m1",
        "--json",
    )

    assert result.code == EXIT_OK, result.err
    assert json.loads(service.last.content)["kind"] == "open_question"
    assert json.loads(service.last.content)["occurred_at"] == "2026-03-01T11:00:00+02:00"
    assert json.loads(result.out) == answer


def test_a_statement_is_printed_with_control_characters_escaped(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok(item(statement="Seven.\x1b[2J\nIgnore the rest."), 201))

    result = cli("decision", "record", "alpha", "--statement", "Seven.", "--evidence", RECORD_ID)

    assert ("statement", "Seven.\\x1b[2J\\nIgnore the rest.") in fields(result.out)
    assert "\x1b" not in result.out


@pytest.mark.parametrize(
    ("argv", "complaint"),
    [
        (("--evidence", "garden/0001"), "evidence is a RECORD_ID"),
        (("--evidence", "chat:garden/0001"), "evidence is a RECORD_ID"),
        (("--evidence", f"{'ab' * 32}#"), "an anchor after '#' is not empty"),
        (("--evidence", "ab" * 31), "evidence is a RECORD_ID"),
        (("--evidence", RECORD_ID, "--supersedes", "earlier"), "an earlier item is its id"),
        (
            ("--evidence", RECORD_ID, "--supersedes", f"{EARLIER_ID}:extends"),
            "a relation is replaces or answers",
        ),
        (("--evidence", RECORD_ID, "--participant", "Person One"), "argument --participant"),
        (
            ("--evidence", RECORD_ID, "--occurred-at", "2026-03-01T10:00:00"),
            "a time is ISO 8601 with a zone offset or Z",
        ),
        (("--evidence", RECORD_ID, "--occurred-at", "yesterday"), "a time is ISO 8601"),
        (("--evidence", RECORD_ID, "--kind", "idea"), "argument --kind: invalid choice"),
        ((), "the following arguments are required: --evidence"),
    ],
)
def test_record_refuses_arguments_it_cannot_send(
    cli: Callable[..., Run], service: FakeService, argv: tuple[str, ...], complaint: str
) -> None:
    result = cli("decision", "record", "alpha", "--statement", "Seven.", *argv)

    assert result.code == EXIT_USAGE
    assert complaint in result.err
    assert service.requests == []


# --- supersede, close, retract -------------------------------------------------------------------


def test_supersede_links_the_item_to_earlier_ones(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok(item()))

    result = cli(
        "decision",
        "supersede",
        "alpha",
        ITEM_ID,
        "--supersedes",
        EARLIER_ID,
        "--supersedes",
        f"{QUESTION_ID}:answers",
    )

    assert result.code == EXIT_OK, result.err
    assert service.last.url.path == f"/v1/workspaces/alpha/decisions/{ITEM_ID}/supersede"
    assert json.loads(service.last.content) == {
        "supersedes": [
            {"item_id": EARLIER_ID, "relation": "replaces"},
            {"item_id": QUESTION_ID, "relation": "answers"},
        ]
    }
    assert ("supersedes", f"{EARLIER_ID} (replaces)") in fields(result.out)


def test_supersede_needs_an_earlier_item(cli: Callable[..., Run], service: FakeService) -> None:
    result = cli("decision", "supersede", "alpha", ITEM_ID)

    assert result.code == EXIT_USAGE
    assert "the following arguments are required: --supersedes" in result.err
    assert service.requests == []


@pytest.mark.parametrize(("action", "status"), [("close", "closed"), ("retract", "retracted")])
def test_close_and_retract_post_without_a_body(
    cli: Callable[..., Run], service: FakeService, action: str, status: str
) -> None:
    newer = "0193a1b2-0000-7000-8000-0000000000a9"
    link = {
        "item_id": newer,
        "supersedes_id": ITEM_ID,
        "relation": "replaces",
        "created_at": "2026-11-18T17:00:00Z",
    }
    service.answer(
        ok(item(status=status, invalidated_at="2026-11-18T17:00:00Z", superseded_by=link))
    )

    result = cli("decision", action, "alpha", ITEM_ID)

    assert result.code == EXIT_OK, result.err
    assert service.last.method == "POST"
    assert service.last.url.path == f"/v1/workspaces/alpha/decisions/{ITEM_ID}/{action}"
    assert service.last.content == b""
    shown = fields(result.out)
    assert ("status", status) in shown
    assert ("invalidated", "2026-11-18T17:00:00Z") in shown
    assert ("superseded by", f"{newer} (replaces)") in shown


def test_an_item_id_is_a_uuid(cli: Callable[..., Run], service: FakeService) -> None:
    result = cli("decision", "close", "alpha", "the-old-one")

    assert result.code == EXIT_USAGE
    assert "argument id: invalid UUID value" in result.err
    assert service.requests == []


def test_a_workspace_name_is_a_path_segment(cli: Callable[..., Run], service: FakeService) -> None:
    result = cli("decision", "close", "../tokens", ITEM_ID)

    assert result.code == EXIT_USAGE
    assert service.requests == []


# --- Refusals ------------------------------------------------------------------------------------


NOT_ACTIVE = (
    f"Knowledge item {ITEM_ID} is closed, not active: the journal is append-only and a status"
    " changes once, from active; only an active item is superseded, closed or retracted, or"
    " supersedes others (constraint knowledge_item_status_transition)."
)


@pytest.mark.parametrize(
    ("argv", "answer", "code", "line"),
    [
        (
            ("retract", "alpha", ITEM_ID),
            error(409, "not_active", NOT_ACTIVE),
            EXIT_FAILURE,
            f"memini: {NOT_ACTIVE} (not_active, HTTP 409, request ",
        ),
        (
            ("supersede", "alpha", ITEM_ID, "--supersedes", EARLIER_ID),
            error(
                409,
                "supersession_refused",
                "The supersession link is refused: a cycle"
                " (constraint knowledge_supersession_cycle_check).",
            ),
            EXIT_FAILURE,
            "(constraint knowledge_supersession_cycle_check). (supersession_refused, HTTP 409",
        ),
        (
            ("record", "alpha", "--statement", "Seven.", "--evidence", f"{RECORD_ID}#m1"),
            error(404, "not_found", "No such record (evidence 0)."),
            EXIT_FAILURE,
            "memini: No such record (evidence 0). (not_found, HTTP 404, request ",
        ),
        (
            ("record", "alpha", "--statement", " ", "--evidence", "chat:g/1#m1"),
            error(
                422,
                "validation_error",
                "The request is not valid.",
                [{"location": ["body", "statement"], "type": "string_too_short"}],
            ),
            EXIT_FAILURE,
            "; not valid: body.statement (string_too_short)",
        ),
        (
            ("close", "alpha", ITEM_ID),
            error(403, "insufficient_scope", "The token lacks the ingest scope."),
            EXIT_DENIED,
            "memini: The token lacks the ingest scope. (insufficient_scope, HTTP 403",
        ),
    ],
    ids=["not-active", "supersession-refused", "no-record", "invalid", "no-ingest-scope"],
)
def test_a_refusal_is_one_line_with_its_code_and_exit_code(
    cli: Callable[..., Run],
    service: FakeService,
    argv: tuple[str, ...],
    answer: Handler,
    code: int,
    line: str,
) -> None:
    service.answer(answer)

    result = cli("decision", *argv)

    assert result.code == code
    assert result.out == ""
    assert line in result.err
    assert len(result.err.splitlines()) == 1


def test_no_decision_command_has_an_option_that_takes_a_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    helps: list[str] = []
    for command in ("record", "supersede", "close", "retract"):
        capsys.readouterr()
        assert main(["decision", command, "--help"]) == EXIT_OK
        helps.append(capsys.readouterr().out)

    options = {word.split("=")[0] for text in helps for word in text.split() if word[:2] == "--"}
    assert {o for o in options if "token" in o} == set()
