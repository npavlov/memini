"""``memini push`` against a fake service: reading JSON and JSON lines, the checks, batching,
retries, the summary and the dry run.

The service is an ``httpx2.MockTransport``; nothing here needs a database or the network, and the
waits between attempts are recorded, not slept. The flow against the real service is in
test_cli_push_db.py. Every value is synthetic; the token is generated per run.
"""

import io
import json
import re
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest

from memini.cli import main
from memini.cli import push as push_command
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_DENIED, EXIT_FAILURE, EXIT_OK, EXIT_UNAVAILABLE, EXIT_USAGE
from memini.cli.push import (
    MAX_ATTEMPTS,
    InputError,
    Item,
    Record,
    batch_body,
    check,
    check_all,
    plan,
    read_input,
)
from memini.config import environment_variables

REPOSITORY: Final = Path(__file__).resolve().parents[1]
EXAMPLES: Final = REPOSITORY / "docs" / "examples"
INTAKE: Final = REPOSITORY / "docs" / "intake.md"
SKILL: Final = REPOSITORY / "integrations" / "claude-code" / "skills" / "memini-push" / "SKILL.md"

TOKEN = "memini_" + secrets.token_urlsafe(32)
RECORDS: Final = "/v1/workspaces/acme/sources/chat/records"

type Json = dict[str, Any]
type Answer = httpx2.Response | Exception


def thread(external_id: str, text: str = "Shall we ship on Friday?", minute: int = 0) -> Json:
    at = f"2026-11-02T09:{minute:02d}:00Z"
    return {
        "kind": "thread",
        "external_id": external_id,
        "occurred_at": at,
        "payload": {
            "channel": "general",
            "messages": [{"id": "m1", "author": "Ann Example", "sent_at": at, "text": text}],
        },
    }


def tombstone(external_id: str) -> Json:
    return {"kind": "thread", "external_id": external_id, "deleted": True}


def lines(*records: Json) -> bytes:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()


def accepted(inserted: int = 0, unchanged: int = 0, **counts: int) -> httpx2.Response:
    every = {"updated": 0, "revived": 0, "deleted": 0, "reconciled": 0, "forgotten": 0}
    every |= {"inserted": inserted, "unchanged": unchanged, **counts}
    body = {
        "run_id": "0193a1b2-0000-7000-8000-000000000001",
        "stream": None,
        "window_complete": None,
        "counts": every,
        "checkpoint": None,
        "index": {
            "indexed": True,
            "records_not_indexed": 0,
            "chunks_inserted": 0,
            "chunks_updated": 0,
            "chunks_unchanged": 0,
            "chunks_deleted": 0,
            "texts_embedded": 0,
            "vectors_reused": 0,
            "note": None,
        },
    }
    return httpx2.Response(200, json=body)


def refused(
    status: int,
    code: str,
    message: str = "Refused.",
    *,
    details: list[Json] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    body = {
        "error": {"code": code, "message": message, "request_id": "req-1", "details": details or []}
    }
    return httpx2.Response(status, json=body, headers=headers)


@dataclass
class FakeService:
    """Answers the requests in turn from ``answers`` (the last one again when they run out) and
    records each request's body."""

    answers: list[Answer] = field(default_factory=list[Answer])
    bodies: list[Json] = field(default_factory=list[Json])
    paths: list[str] = field(default_factory=list[str])

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.paths.append(request.url.path)
        self.bodies.append(json.loads(request.content))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sent(self) -> list[list[str]]:
        """The external ids of each request, in order."""
        return [[record["external_id"] for record in body["records"]] for body in self.bodies]


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    for name in (*environment_variables(), *CLIENT_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_TOKEN", TOKEN)
    return FakeService([accepted()])


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    found: list[float] = []
    monkeypatch.setattr(push_command, "sleep", found.append)
    return found


@pytest.fixture
def cli(
    capsys: pytest.CaptureFixture[str], service: FakeService, waits: list[float]
) -> Callable[..., Run]:
    del waits  # no test sleeps

    def invoke(*argv: str) -> Run:
        capsys.readouterr()
        code = main(list(argv), transport=httpx2.MockTransport(service))
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return invoke


def write(tmp_path: Path, data: bytes, name: str = "records.jsonl") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def push(cli: Callable[..., Run], *argv: str) -> Run:
    return cli("push", "--workspace", "acme", "--source", "chat", *argv)


# --- Reading the input ---------------------------------------------------------------------------


def test_the_input_is_a_record_an_array_a_batch_or_json_lines() -> None:
    one, two = thread("C1/1"), thread("C1/2")

    single = read_input(json.dumps(one).encode())
    assert [item.position for item in single.items] == ["record 1"]
    array = read_input(json.dumps([one, two], indent=2).encode())
    assert [item.position for item in array.items] == ["record 1", "record 2"]
    batch = read_input(json.dumps({"stream": "chat/general", "records": [one, two]}).encode())
    assert (batch.stream, [item.value for item in batch.items]) == ("chat/general", [one, two])
    crlf = lines(two).replace(b"\n", b"\r\n")
    jsonl = read_input(b"\xef\xbb\xbf" + lines(one) + b"  \n" + crlf)  # a BOM, a blank line
    assert [item.position for item in jsonl.items] == ["line 1", "line 3"]
    assert [item.value for item in jsonl.items] == [one, two]
    assert read_input(b"  \n").items == ()
    assert read_input(b"[]").items == ()


@pytest.mark.parametrize(
    ("data", "says"),
    [
        (b'{"kind": "thread",', "not valid JSON at line 1, column 19"),
        (
            b'{"kind": "thread"}\n{"kind": "thread"}\n{"kind": ',
            "line 3: not valid JSON at column 10",
        ),
        (b'{"kind": "thread", "kind": "meeting"}', "the key 'kind' twice"),
        (b'{"a": 1}\n{"b": NaN}\n', "line 2: NaN is not a JSON number"),
        (b'{"records": [], "window": {"from": "x"}}', "claims no window"),
        (b'{"records": [], "cursor": 1}', "a batch has no field 'cursor'"),
        (b'{"records": {}}', "records is not an array"),
        (b'"a thread"', "found a JSON string"),
        (b"\xff\xfe", "not UTF-8"),
    ],
)
def test_input_that_holds_no_records_is_refused_saying_where(data: bytes, says: str) -> None:
    with pytest.raises(InputError, match=re.escape(says)):
        read_input(data)


# --- Checks --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "where", "reason"),
    [
        ("a thread", None, "not a JSON object"),
        ({**thread("C1/1"), "kind": "chat"}, "kind", "Input should be"),
        ({**thread("C1/1"), "colour": "red"}, "colour", "Extra inputs are not permitted"),
        ({**thread("C1/1"), "occurred_at": "2026-11-02"}, "occurred_at", "timezone"),
        ({**tombstone("C1/1"), "payload": {}}, None, "a tombstone has a kind and an external id"),
        ({**thread("C1/1"), "external_url": "ftp://chat.example.test/1"}, "external_url", "http"),
    ],
)
def test_a_record_the_service_would_refuse_is_rejected_with_the_field_and_why(
    value: object, where: str | None, reason: str
) -> None:
    result = check(Item("line 4", value))
    assert not isinstance(result, Record)
    assert (result.position, result.field, result.by) == ("line 4", where, "check")
    assert reason in result.reason


def test_a_payload_without_the_fields_of_its_kind_is_rejected() -> None:
    record = thread("C1/1")
    del record["payload"]["messages"][0]["author"]
    result = check(Item("record 2", record))
    assert not isinstance(result, Record)
    assert (result.kind, result.external_id) == ("thread", "C1/1")
    assert result.field == "payload.messages[0].author"


def test_a_record_that_comes_again_is_sent_once_as_its_last_version() -> None:
    items = [
        Item("line 1", thread("C1/1", "first")),
        Item("line 2", thread("C1/2")),
        Item("line 3", thread("C1/1", "edited")),
    ]
    kept, rejected, repeated = check_all(items)
    assert [record.position for record in kept] == ["line 2", "line 3"]
    assert rejected == []
    assert [record.position for record in repeated] == ["line 1"]
    assert json.loads(kept[1].body)["payload"]["messages"][0]["text"] == "edited"


def test_a_record_is_sent_as_it_was_read() -> None:
    record = thread("C1/1", "Приняли: релиз в пятницу.")
    record["payload"]["unknown_field"] = {"kept": True}
    (kept,), _, _ = check_all([Item("line 1", record)])
    assert json.loads(kept.body) == record
    assert "пятницу".encode() in kept.body


def test_batches_hold_at_most_the_records_and_bytes_given() -> None:
    kept, _, _ = check_all(Item(f"line {n}", thread(f"C1/{n}")) for n in range(1, 8))
    assert [len(batch) for batch in plan(kept, None, max_records=3)] == [3, 3, 1]
    size = len(batch_body("chat/general", kept[:2]))
    by_bytes = plan(kept, "chat/general", max_records=100, max_bytes=size)
    assert [len(batch) for batch in by_bytes] == [2, 2, 2, 1]
    assert all(len(batch_body("chat/general", batch)) <= size for batch in by_bytes)
    alone = plan(kept, None, max_records=100, max_bytes=10)
    assert [len(batch) for batch in alone] == [1] * 7  # a record too large still goes, alone


# --- The command ---------------------------------------------------------------------------------


def test_push_sends_the_records_in_batches_and_prints_what_was_stored(
    cli: Callable[..., Run], service: FakeService, tmp_path: Path
) -> None:
    service.answers = [accepted(inserted=2), accepted(unchanged=1, deleted=1)]
    path = write(tmp_path, lines(thread("C1/1"), thread("C1/2"), thread("C1/3"), tombstone("C1/0")))

    run = push(cli, "--stream", "chat/general", "--batch-size", "2", path)

    assert run.code == EXIT_OK, run.err
    assert service.paths == [RECORDS, RECORDS]
    assert service.sent() == [["C1/1", "C1/2"], ["C1/3", "C1/0"]]
    assert {body["stream"] for body in service.bodies} == {"chat/general"}
    assert service.bodies[0]["records"][0] == thread("C1/1")
    assert run.out.splitlines() == [
        "sent 3 records and 1 tombstone in 2 batches to acme/chat (stream chat/general)",
        "stored 2 (inserted 2, updated 0, revived 0), deleted 1, unchanged 1",
    ]


def test_push_reads_stdin_and_the_stream_of_a_batch(
    cli: Callable[..., Run], service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = {"stream": "chat/general", "records": [thread("C1/1")]}
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(batch).encode()), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    service.answers = [accepted(inserted=1)]

    run = push(cli, "-")

    assert run.code == EXIT_OK, run.err
    assert service.bodies == [{"stream": "chat/general", "records": [thread("C1/1")]}]
    assert "inserted 1" in run.out


def test_the_example_batch_of_the_install_guide_is_pushed_as_it_is(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answers = [accepted(inserted=1)]
    run = push(cli, str(EXAMPLES / "thread.json"))
    assert run.code == EXIT_OK, run.err
    assert service.bodies == [json.loads((EXAMPLES / "thread.json").read_text(encoding="utf-8"))]


def test_a_rejected_record_is_reported_and_the_others_are_sent(
    cli: Callable[..., Run], service: FakeService, tmp_path: Path
) -> None:
    broken = thread("C1/2")
    broken["payload"]["messages"][0]["sent_at"] = "yesterday"
    service.answers = [accepted(inserted=2)]
    path = write(tmp_path, lines(thread("C1/1"), broken, thread("C1/3")))

    run = push(cli, path)

    assert run.code == EXIT_FAILURE
    assert service.sent() == [["C1/1", "C1/3"]]
    assert "rejected 1:" in run.out
    assert "  line 2 (thread C1/2): payload.messages[0].sent_at: a time with a zone" in run.out


def test_a_record_the_service_refuses_is_left_out_and_the_batch_sent_again(
    cli: Callable[..., Run], service: FakeService, tmp_path: Path
) -> None:
    message = (
        "The batch is not valid: records[1].payload.messages[0].text: a string of valid"
        " Unicode, such as 2026-03-01T09:30:00Z."
    )
    service.answers = [refused(422, "invalid_batch", message), accepted(inserted=2)]
    path = write(tmp_path, lines(thread("C1/1"), thread("C1/2"), thread("C1/3")))

    run = push(cli, "--json", path)

    assert run.code == EXIT_FAILURE
    assert service.sent() == [["C1/1", "C1/2", "C1/3"], ["C1/1", "C1/3"]]
    summary = json.loads(run.out)
    assert summary["counts"]["inserted"] == 2
    assert summary["rejected"] == [
        {
            "position": "line 2",
            "kind": "thread",
            "external_id": "C1/2",
            "field": "payload.messages[0].text",
            "reason": "a string of valid Unicode, such as 2026-03-01T09:30:00Z",
            "by": "service",
        }
    ]


def test_a_field_that_fails_the_service_s_validation_names_the_record(
    cli: Callable[..., Run], service: FakeService, tmp_path: Path
) -> None:
    details = [{"location": ["body", "records", 0, "occurred_at"], "type": "missing"}]
    service.answers = [refused(422, "validation_error", details=details), accepted(inserted=1)]
    path = write(tmp_path, lines(thread("C1/1"), thread("C1/2")))

    run = push(cli, path)

    assert run.code == EXIT_FAILURE
    assert service.sent() == [["C1/1", "C1/2"], ["C1/2"]]
    assert "  line 1 (thread C1/1): occurred_at: missing" in run.out


def test_a_batch_too_large_for_the_service_is_split_and_later_ones_made_smaller(
    cli: Callable[..., Run], service: FakeService, tmp_path: Path
) -> None:
    service.answers = [refused(413, "too_many_records"), accepted(inserted=2)]
    path = write(tmp_path, lines(*(thread(f"C1/{n}") for n in range(1, 7))))

    run = push(cli, "--batch-size", "4", path)

    assert run.code == EXIT_OK, run.err
    assert [len(ids) for ids in service.sent()] == [4, 2, 2, 2]
    assert "inserted 6" in run.out
    assert "smaller batches" in run.err


def test_a_busy_or_unavailable_service_is_tried_again_after_a_wait(
    cli: Callable[..., Run], service: FakeService, waits: list[float], tmp_path: Path
) -> None:
    service.answers = [
        refused(429, "rate_limited", headers={"Retry-After": "7"}),
        refused(503, "unavailable"),
        refused(409, "ingest_conflict"),
        httpx2.ConnectError("connection refused"),
        accepted(inserted=1),
    ]
    run = push(cli, write(tmp_path, lines(thread("C1/1"))))

    assert run.code == EXIT_OK, run.err
    assert len(service.bodies) == 5
    assert waits == [7.0, 2.0, 4.0, 8.0]
    assert "sending the batch again in 7 s (attempt 2 of 6)" in run.err
    assert "inserted 1" in run.out


def test_the_retries_end_and_the_summary_says_how_far_the_push_got(
    cli: Callable[..., Run], service: FakeService, waits: list[float], tmp_path: Path
) -> None:
    service.answers = [accepted(inserted=1), refused(503, "unavailable")]
    path = write(tmp_path, lines(thread("C1/1"), thread("C1/2")))

    run = push(cli, "--batch-size", "1", path)

    assert run.code == EXIT_UNAVAILABLE
    assert len(service.bodies) == 1 + MAX_ATTEMPTS
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert run.out.splitlines()[0] == "sent 1 record in 1 batch to acme/chat"
    assert "unavailable, HTTP 503" in run.err


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (refused(503, "index_mismatch", "Nothing was stored: run memini reindex."), EXIT_FAILURE),
        (refused(404, "not_found", "No such source."), EXIT_FAILURE),
        (refused(409, "not_ingestible"), EXIT_FAILURE),
        (refused(401, "invalid_token"), EXIT_DENIED),
        (refused(403, "insufficient_scope"), EXIT_DENIED),
    ],
)
def test_a_refusal_no_retry_fixes_stops_the_push_at_once(
    cli: Callable[..., Run],
    service: FakeService,
    waits: list[float],
    tmp_path: Path,
    answer: httpx2.Response,
    code: int,
) -> None:
    service.answers = [answer]
    run = push(cli, write(tmp_path, lines(thread("C1/1"))))
    assert run.code == code
    assert len(service.bodies) == 1
    assert waits == []
    assert run.out.startswith("sent nothing in 0 batches to acme/chat")


def test_a_dry_run_checks_and_lists_the_records_and_sends_nothing(
    cli: Callable[..., Run], service: FakeService, tmp_path: Path
) -> None:
    broken = {**thread("C1/9"), "occurred_at": None}
    path = write(tmp_path, lines(thread("C1/1"), tombstone("C1/0"), broken))

    run = push(cli, "--dry-run", path)

    assert run.code == EXIT_FAILURE  # a record would be rejected
    assert service.bodies == []
    assert run.out.splitlines() == [
        "dry run, nothing sent: would send 1 record and 1 tombstone in 1 batch to acme/chat",
        "  thread    C1/1  2026-11-02T09:00Z  1 message, general",
        "  thread    C1/0  -  deleted",
        "rejected 1:",
        "  line 3 (thread C1/9): occurred_at: a time with a zone",
    ]

    clean = push(cli, "--dry-run", "--json", write(tmp_path, lines(thread("C1/1")), "ok.jsonl"))
    assert clean.code == EXIT_OK, clean.err
    summary = json.loads(clean.out)
    assert (summary["dry_run"], summary["records"], summary["batch_sizes"]) == (True, 1, [1])
    assert service.bodies == []


def test_input_that_is_not_valid_sends_nothing(
    cli: Callable[..., Run], service: FakeService, tmp_path: Path
) -> None:
    run = push(cli, write(tmp_path, lines(thread("C1/1")) + b"{not json}\n"))
    assert run.code == EXIT_USAGE
    assert "line 2: not valid JSON" in run.err
    missing = push(cli, str(tmp_path / "missing.jsonl"))
    assert missing.code == EXIT_USAGE
    assert "cannot read" in missing.err
    empty = push(cli, write(tmp_path, b"\n", "empty.jsonl"))
    assert (empty.code, empty.out) == (EXIT_OK, "")
    assert "holds no records; nothing was sent" in empty.err
    assert service.bodies == []


def test_the_stream_of_the_input_and_stream_must_agree(
    cli: Callable[..., Run], service: FakeService, tmp_path: Path
) -> None:
    batch = {"stream": "chat/general", "records": [thread("C1/1")]}
    path = write(tmp_path, json.dumps(batch).encode(), "batch.json")
    run = push(cli, "--stream", "chat/random", path)
    assert run.code == EXIT_USAGE
    assert "differ" in run.err
    bad = push(cli, "--stream", "a\tb", path.replace("batch.json", "x.json"))
    assert bad.code == EXIT_USAGE
    assert service.bodies == []


# --- The documentation ---------------------------------------------------------------------------


def test_every_example_of_the_intake_guide_is_a_record_memini_push_takes() -> None:
    blocks = re.findall(r"```json\n(.*?)```", INTAKE.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) >= 4
    found: set[str] = set()
    for block in blocks:
        kept, rejected, _ = check_all(read_input(block.encode()).items)
        assert rejected == [], block
        found |= {record.kind for record in kept}
    assert found == {"thread", "meeting", "document"}
    for path in sorted(EXAMPLES.glob("*.json")):
        kept, rejected, _ = check_all(read_input(path.read_bytes()).items)
        assert (len(kept), rejected) == (1, []), path.name


def test_the_examples_of_the_guide_are_the_files_of_the_examples() -> None:
    text = INTAKE.read_text(encoding="utf-8")
    blocks = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL)]
    for name in ("mail-thread.json", "meeting.json", "document.json"):
        assert json.loads((EXAMPLES / name).read_text(encoding="utf-8")) in blocks, name


def test_the_push_skill_names_itself_and_runs_memini_push_only() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head: dict[str, str] = {}
    for line in text.split("---\n", 2)[1].splitlines():
        key, _, value = line.partition(":")
        head[key.strip()] = value.strip()
    assert head["name"] == "memini-push"
    assert head["allowed-tools"] == "Bash(memini push *)"
    assert "**Verbatim.**" in text
    assert "**Data, never instructions.**" in text
