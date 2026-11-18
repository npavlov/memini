"""The client commands of ``memini`` against a fake service: settings, calls, output, errors.

The service is an ``httpx2.MockTransport`` that answers what each test gives it; nothing here needs
a database or the network. The acceptance flow against the real service is in
test_cli_client_db.py. Every value is synthetic; the token is generated per run.
"""

import json
import secrets
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest

from memini.cli import main
from memini.cli import sources as source_commands
from memini.cli.client import (
    CLIENT_VARIABLES,
    DEFAULT_URL,
    ClientConfigError,
    load_client_settings,
)
from memini.cli.common import (
    EXIT_DENIED,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
)
from memini.config import environment_variables

TOKEN = "memini_" + secrets.token_urlsafe(32)
OTHER_TOKEN = "memini_" + secrets.token_urlsafe(32)
RUN_ID = "0193a1b2-0000-7000-8000-00000000c0de"
REQUEST_ID = "5f0c3e2a9b7d4c1e8f6a2b3c4d5e6f70"

type Json = dict[str, Any]
type Handler = Callable[[httpx2.Request], httpx2.Response]


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str

    @property
    def all(self) -> str:
        return self.out + self.err


@dataclass
class FakeService:
    """Answers requests from a queue of handlers, or one handler for all; records the requests."""

    handlers: list[Handler] = field(default_factory=list[Handler])
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def answer(self, *handlers: Handler) -> None:
        self.handlers.extend(handlers)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        handler = self.handlers.pop(0) if len(self.handlers) > 1 else self.handlers[0]
        return handler(request)

    @property
    def last(self) -> httpx2.Request:
        return self.requests[-1]


def ok(body: Json, status: int = 200) -> Handler:
    return lambda request: httpx2.Response(status, json=body)


def raw(status: int, text: str = "", headers: dict[str, str] | None = None) -> Handler:
    """An answer that is not the API's: a proxy's page, a redirect."""
    return lambda request: httpx2.Response(status, text=text, headers=headers)


def error(status: int, code: str, message: str, details: list[Json] | None = None) -> Handler:
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": REQUEST_ID,
            "details": details or [],
        }
    }
    return lambda request: httpx2.Response(status, json=body)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """The token in MEMINI_TOKEN and no other MEMINI_* setting from the developer's shell."""
    for name in (*environment_variables(), *CLIENT_VARIABLES, "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_TOKEN", TOKEN)
    return monkeypatch


@pytest.fixture
def service(env: pytest.MonkeyPatch) -> FakeService:
    return FakeService()


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], service: FakeService) -> Callable[..., Run]:
    def invoke(*argv: str) -> Run:
        capsys.readouterr()
        code = main(list(argv), transport=httpx2.MockTransport(service))
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return invoke


# --- Settings ------------------------------------------------------------------------------------


def test_settings_default_to_the_local_service() -> None:
    settings = load_client_settings({"MEMINI_TOKEN": TOKEN})

    assert settings.url == DEFAULT_URL
    assert settings.token.get_secret_value() == TOKEN
    assert settings.origin == "MEMINI_TOKEN"
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize(
    ("given", "url"),
    [
        ("https://memini.example.invalid/", "https://memini.example.invalid"),
        ("http://127.0.0.1:16364", "http://127.0.0.1:16364"),
        (" https://example.invalid/memini/ ", "https://example.invalid/memini"),
    ],
)
def test_settings_take_the_url_from_the_environment(given: str, url: str) -> None:
    assert load_client_settings({"MEMINI_URL": given, "MEMINI_TOKEN": TOKEN}).url == url


def test_the_url_option_wins_over_the_environment() -> None:
    settings = load_client_settings(
        {"MEMINI_URL": "http://127.0.0.1:1", "MEMINI_TOKEN": TOKEN}, url="http://127.0.0.1:2"
    )

    assert settings.url == "http://127.0.0.1:2"


@pytest.mark.parametrize(
    ("url", "complaint"),
    [
        ("ftp://example.invalid", "MEMINI_URL: invalid"),
        ("127.0.0.1:6364", "MEMINI_URL: invalid"),
        ("http://", "MEMINI_URL: invalid"),
        ("http://127.0.0.1:99999", "MEMINI_URL: invalid"),
        ("http://127.0.0.1:6364/?q=1", "MEMINI_URL: has a query or a fragment"),
        ("http://exa mple.invalid", "MEMINI_URL: invalid"),
        ("http://a\x1b[2J.invalid", "MEMINI_URL: invalid"),
        ("https://ops:hunter2-secret@example.invalid", "MEMINI_URL: holds a user name or pass"),
        (f"https://{TOKEN}@example.invalid", "MEMINI_URL: holds a user name or password"),
    ],
)
def test_an_invalid_url_is_refused_without_its_value(url: str, complaint: str) -> None:
    with pytest.raises(ClientConfigError) as refused:
        load_client_settings({"MEMINI_URL": url, "MEMINI_TOKEN": TOKEN})

    assert str(refused.value).startswith(complaint)
    assert "hunter2" not in str(refused.value)
    assert TOKEN not in str(refused.value)


def test_the_token_comes_from_a_file(tmp_path: Path) -> None:
    file = tmp_path / "token"
    file.write_text(f"{TOKEN}\n", encoding="utf-8")

    from_env = load_client_settings({"MEMINI_TOKEN_FILE": str(file)})
    from_option = load_client_settings({"MEMINI_TOKEN": OTHER_TOKEN}, token_file=file)

    assert from_env.token.get_secret_value() == TOKEN
    assert from_env.origin == "MEMINI_TOKEN_FILE"
    assert from_option.token.get_secret_value() == TOKEN
    assert from_option.origin == f"--token-file {file}"


@pytest.mark.parametrize(
    ("environ", "complaint"),
    [
        ({}, "no token: set MEMINI_TOKEN or MEMINI_TOKEN_FILE"),
        ({"MEMINI_TOKEN": ""}, "no token: set MEMINI_TOKEN or MEMINI_TOKEN_FILE"),
        (
            {"MEMINI_TOKEN": TOKEN, "MEMINI_TOKEN_FILE": "/nonexistent"},
            "MEMINI_TOKEN, MEMINI_TOKEN_FILE: both are set",
        ),
        ({"MEMINI_TOKEN_FILE": "/nonexistent/token"}, "MEMINI_TOKEN_FILE: cannot read"),
        ({"MEMINI_TOKEN": TOKEN[:-1]}, "MEMINI_TOKEN: not a Memini token"),
        ({"MEMINI_TOKEN": "Bearer " + TOKEN}, "MEMINI_TOKEN: not a Memini token"),
    ],
)
def test_a_missing_or_malformed_token_is_refused_without_its_value(
    environ: dict[str, str], complaint: str
) -> None:
    with pytest.raises(ClientConfigError) as refused:
        load_client_settings(environ)

    assert str(refused.value).startswith(complaint)
    assert TOKEN[8:] not in str(refused.value)


def test_a_client_command_without_a_token_exits_2(
    cli: Callable[..., Run], env: pytest.MonkeyPatch, service: FakeService
) -> None:
    env.delenv("MEMINI_TOKEN")

    result = cli("workspace", "list")

    assert result.code == EXIT_USAGE
    assert result.out == ""
    assert result.err.startswith("memini: no token: set MEMINI_TOKEN or MEMINI_TOKEN_FILE.")
    assert "memini bootstrap" in result.err
    assert service.requests == []


def test_a_client_command_needs_no_database_url(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok({"workspaces": []}))

    result = cli("workspace", "list")

    assert result.code == EXIT_OK


def test_config_is_refused_for_a_client_command(
    cli: Callable[..., Run], service: FakeService
) -> None:
    result = cli("--config", "/nonexistent.toml", "workspace", "list")

    assert result.code == EXIT_USAGE
    assert "--config is for operator commands" in result.err
    assert service.requests == []


# --- No token on the command line ----------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("--token", TOKEN, "workspace", "list"),
        ("workspace", "list", "--token", TOKEN),
        ("workspace", "list", f"--token={TOKEN}"),
        ("token", "create", "--scope", "read", "--token", TOKEN),
    ],
)
def test_there_is_no_token_option_and_a_token_given_is_not_repeated(
    cli: Callable[..., Run], service: FakeService, argv: tuple[str, ...]
) -> None:
    result = cli(*argv)

    assert result.code == EXIT_USAGE
    assert TOKEN not in result.all
    assert TOKEN[8:30] not in result.all
    assert service.requests == []


def test_no_command_has_an_option_that_takes_a_token(capsys: pytest.CaptureFixture[str]) -> None:
    helps: list[str] = []
    for argv in (
        (),
        ("workspace", "create"),
        ("workspace", "list"),
        ("source", "add"),
        ("source", "list"),
        ("source", "show"),
        ("source", "edit"),
        ("source", "remove"),
        ("streams",),
        ("sync",),
        ("runs",),
        ("run",),
        ("token", "create"),
        ("token", "list"),
        ("token", "revoke"),
        ("search",),
        ("serve",),
        ("migrate",),
        ("bootstrap",),
        ("workspace", "llm"),
        ("reindex",),
        ("eval",),
        ("credentials", "set"),
        ("credentials", "list"),
        ("credentials", "delete"),
        ("credentials", "rotate-key"),
    ):
        capsys.readouterr()
        assert main([*argv, "--help"]) == EXIT_OK
        helps.append(capsys.readouterr().out)

    options = {word.split("=")[0] for text in helps for word in text.split() if word[:2] == "--"}
    assert {o for o in options if "token" in o} == {"--token-file"}


# --- Calls ---------------------------------------------------------------------------------------


def test_a_call_sends_the_token_to_the_service_url_and_path(
    cli: Callable[..., Run], env: pytest.MonkeyPatch, service: FakeService
) -> None:
    env.setenv("MEMINI_URL", "https://memini.example.invalid/prefix/")
    service.answer(ok({"sources": []}))

    result = cli("source", "list", "acme")

    request = service.last
    assert result.code == EXIT_OK
    assert request.method == "GET"
    assert str(request.url) == "https://memini.example.invalid/prefix/v1/workspaces/acme/sources"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["User-Agent"].startswith("memini-cli/")
    assert "workspace acme has no sources" in result.err


def test_workspace_create_and_list(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(
        ok({"id": "0193-a", "name": "alpha", "created_at": "2026-11-18T16:02:59Z"}, 201),
        ok(
            {
                "workspaces": [
                    {"id": "0193-a", "name": "alpha", "created_at": "2026-11-18T16:02:59Z"},
                    {"id": "0193-b", "name": "beta", "created_at": "2026-11-18T17:00:00+00:00"},
                ]
            }
        ),
    )

    created = cli("workspace", "create", "alpha")
    listed = cli("workspace", "list")

    assert json.loads(service.requests[0].content) == {"name": "alpha"}
    assert created.out == "created workspace alpha (0193-a)\n"
    assert listed.out.splitlines() == [
        "NAME   CREATED            ID",
        "alpha  2026-11-18T16:02Z  0193-a",
        "beta   2026-11-18T17:00Z  0193-b",
    ]


SOURCE: Json = {
    "id": "0193-s",
    "workspace": "acme",
    "name": "shop",
    "kind": "git",
    "location": "https://git.example.invalid/team/shop.git",
    "refs": ["refs/heads/main", "refs/tags/v1"],
    "schedule_seconds": 5400.0,
    "include": ["src/**"],
    "exclude": [],
    "builtin_excludes": True,
    "visibility": "private",
    "retention_days": None,
    "llm_policy": "local",
    "created_at": "2026-11-18T16:00:00Z",
    "updated_at": "2026-11-18T16:00:00Z",
}


def test_source_add_sends_the_settings_and_shows_the_source(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok(SOURCE, 201))

    result = cli(
        "source",
        "add",
        "acme",
        "shop",
        "--location",
        "https://git.example.invalid/team/shop.git",
        "--ref",
        "main",
        "--ref",
        "refs/tags/v1",
        "--schedule",
        "1h30m",
        "--include",
        "src/**",
        "--llm-policy",
        "none",
    )

    assert result.code == EXIT_OK
    assert service.last.method == "POST"
    assert service.last.url.path == "/v1/workspaces/acme/sources"
    assert json.loads(service.last.content) == {
        "kind": "git",
        "name": "shop",
        "location": "https://git.example.invalid/team/shop.git",
        "refs": ["main", "refs/tags/v1"],
        "schedule_seconds": 5400,
        "include": ["src/**"],
        "exclude": [],
        "builtin_excludes": True,
        "llm_policy": "none",
    }
    fields = {line[:18].strip(): line[18:] for line in result.out.splitlines()}
    assert fields["name"] == "shop"
    assert fields["refs"] == "refs/heads/main, refs/tags/v1"
    assert fields["schedule"] == "every 1h30m"
    assert fields["builtin excludes"] == "yes"
    assert fields["retention"] == "keep records"


def test_source_add_push_sends_no_git_settings(
    cli: Callable[..., Run], service: FakeService
) -> None:
    push: Json = {
        **SOURCE,
        "kind": "push",
        "name": "notes",
        "location": None,
        "refs": list[str](),
        "schedule_seconds": None,
        "include": list[str](),
        "builtin_excludes": None,
    }
    service.answer(ok(push, 201))

    result = cli("source", "add", "acme", "notes", "--kind", "push", "--retention-days", "30")

    assert result.code == EXIT_OK
    assert json.loads(service.last.content) == {
        "kind": "push",
        "name": "notes",
        "retention_days": 30,
    }
    assert "POST /v1/workspaces/acme/sources/notes/records" in result.err


@pytest.mark.parametrize(
    ("argv", "complaint"),
    [
        ((), "a git source needs --location and --ref"),
        (("--location", "/srv/shop"), "a git source needs --ref"),
        (("--kind", "push", "--ref", "main"), "--ref: only for a git source"),
        (
            ("--kind", "push", "--location", "/srv/shop", "--no-builtin-excludes"),
            "--location, --no-builtin-excludes: only for a git source",
        ),
    ],
)
def test_source_add_checks_the_options_of_its_kind(
    cli: Callable[..., Run], service: FakeService, argv: tuple[str, ...], complaint: str
) -> None:
    result = cli("source", "add", "acme", "shop", *argv)

    assert result.code == EXIT_USAGE
    assert complaint in result.err
    assert service.requests == []


def test_source_show_as_json_is_the_answer(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(SOURCE))

    result = cli("source", "show", "acme", "shop", "--json")

    assert json.loads(result.out) == SOURCE


def test_source_edit_sends_only_what_changes(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(SOURCE))

    result = cli(
        "source",
        "edit",
        "acme",
        "shop",
        "--no-schedule",
        "--no-exclude",
        "--retention-days",
        "30",
        "--no-builtin-excludes",
    )

    assert result.code == EXIT_OK
    assert service.last.method == "PATCH"
    assert json.loads(service.last.content) == {
        "schedule_seconds": None,
        "exclude": [],
        "builtin_excludes": False,
        "retention_days": 30,
    }


def test_source_edit_with_nothing_to_change_exits_2(
    cli: Callable[..., Run], service: FakeService
) -> None:
    result = cli("source", "edit", "acme", "shop")

    assert result.code == EXIT_USAGE
    assert result.err == "memini: nothing to change; see `memini source edit --help`\n"
    assert service.requests == []


def test_source_remove(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(raw(204))

    result = cli("source", "remove", "acme", "shop")

    assert result.code == EXIT_OK
    assert service.last.method == "DELETE"
    assert service.last.url.path == "/v1/workspaces/acme/sources/shop"
    assert result.out.startswith("removed source shop of workspace acme")


def stream(pending: Json | None, last_run: Json | None) -> Json:
    return {
        "id": "0193-st",
        "key": "refs/heads/main",
        "schedule_seconds": None,
        "next_run_at": None,
        "checked_until": "2026-11-18T16:00:00Z" if last_run else None,
        "lag_seconds": 120.0 if last_run else None,
        "current": None,
        "last_run": last_run,
        "pending_sync": pending,
    }


JOB = "0193-j0b"
PENDING: Json = {
    "id": JOB,
    "kind": "git_sync",
    "status": "running",
    "attempts": 1,
    "run_after": "2026-11-18T16:00:00Z",
    "last_error": None,
}
LAST_RUN: Json = {
    "id": RUN_ID,
    "kind": "git_sync",
    "status": "succeeded",
    "started_at": "2026-11-18T16:00:00Z",
    "finished_at": "2026-11-18T16:00:02Z",
    "error": None,
}


def test_streams_shows_lag_last_run_and_the_queued_sync(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok({"streams": [stream(PENDING, LAST_RUN)]}))

    result = cli("streams", "acme", "shop")

    header, row = result.out.splitlines()
    assert header.split()[:3] == ["STREAM", "SCHEDULE", "CHECKED"]
    assert row.split() == [
        "refs/heads/main",
        "when",
        "triggered",
        "2026-11-18T16:00Z",
        "2m",
        "-",
        "succeeded",
        "2026-11-18T16:00Z",
        "running",
        RUN_ID,
    ]


def test_sync_enqueues_and_says_how_to_follow(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(
        ok({"jobs": [{"stream": "refs/heads/main", "job_id": JOB, "created": True}]}, 202)
    )

    result = cli("sync", "acme", "shop", "--stream", "refs/heads/main")

    assert result.code == EXIT_OK
    assert json.loads(service.last.content) == {"streams": ["refs/heads/main"]}
    assert result.out == f"refs/heads/main: sync queued, job {JOB}\n"
    assert "memini runs acme --source shop" in result.err


@pytest.mark.parametrize(("status", "code"), [("succeeded", EXIT_OK), ("failed", EXIT_FAILURE)])
def test_sync_wait_reports_the_run(
    cli: Callable[..., Run],
    service: FakeService,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    code: int,
) -> None:
    monkeypatch.setattr(source_commands, "POLL_INTERVAL", 0.0)
    retried = {**PENDING, "status": "queued", "attempts": 2, "last_error": "GitError: timed out"}
    finished = {**LAST_RUN, "status": status, "error": None if status == "succeeded" else "boom"}
    service.answer(
        ok({"jobs": [{"stream": "refs/heads/main", "job_id": JOB, "created": False}]}, 202),
        ok({"streams": [stream(PENDING, None)]}),
        ok({"streams": [stream(retried, None)]}),
        ok({"streams": [stream(None, finished)]}),
    )

    result = cli("sync", "acme", "shop", "--wait")

    assert result.code == code
    assert body_of(service.requests[0]) is None
    lines = result.out.splitlines()
    assert lines[0] == f"refs/heads/main: sync already queued or running, job {JOB}"
    assert lines[1] == (
        f"refs/heads/main: run {RUN_ID} succeeded"
        if status == "succeeded"
        else f"refs/heads/main: run {RUN_ID} failed: boom"
    )
    assert "attempt 2 failed, retried: GitError: timed out" in result.err


def test_sync_wait_gives_up_after_its_timeout(
    cli: Callable[..., Run], service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_commands, "POLL_INTERVAL", 0.0)
    service.answer(
        ok({"jobs": [{"stream": "refs/heads/main", "job_id": JOB, "created": True}]}, 202),
        ok({"streams": [stream(PENDING, None)]}),
    )

    result = cli("sync", "acme", "shop", "--wait", "--wait-timeout", "1s")

    assert result.code == EXIT_FAILURE
    assert "still syncing after 1s: refs/heads/main" in result.err


def body_of(request: httpx2.Request) -> object:
    return json.loads(request.content) if request.content else None


RUN: Json = {
    "id": RUN_ID,
    "kind": "git_sync",
    "status": "partial",
    "source": "shop",
    "stream": "refs/heads/main",
    "started_at": "2026-11-18T16:00:00.250Z",
    "finished_at": "2026-11-18T16:00:01.770Z",
    "error": "3 files were \x1b[31mtoo large",
    "workspace": "acme",
    "report": {
        "version": 1,
        "duration_ms": 1520.4,
        "steps": [
            {
                "name": "fetch",
                "started_at": "2026-11-18T16:00:00.300Z",
                "duration_ms": 1200.1,
                "status": "succeeded",
            },
            {
                "name": "index",
                "started_at": "2026-11-18T16:00:01.500Z",
                "duration_ms": 270.0,
                "status": "failed",
                "error": "ValueError",
            },
        ],
        "steps_omitted": 2,
        "counts": {"files_listed": 12, "records_stored": 9},
        "llm": {
            "calls": 2,
            "input_tokens": 1200,
            "output_tokens": 80,
            "by_model": {"local-model": {"calls": 2, "input_tokens": 1200, "output_tokens": 80}},
        },
    },
}


def test_run_prints_the_report_readably(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(RUN))

    result = cli("run", RUN_ID)

    assert result.code == EXIT_OK
    assert service.last.url.path == f"/v1/runs/{RUN_ID}"
    assert result.out.splitlines() == [
        f"run        {RUN_ID}",
        "kind       git_sync",
        "status     partial",
        "workspace  acme",
        "source     shop",
        "stream     refs/heads/main",
        "started    2026-11-18T16:00:00Z",
        "finished   2026-11-18T16:00:01Z",
        "duration   1.5 s",
        "error      3 files were \\x1b[31mtoo large",
        "",
        "steps",
        "  NAME   STATUS     STARTED               DURATION  ERROR",
        "  fetch  succeeded  2026-11-18T16:00:00Z  1.2 s",
        "  index  failed     2026-11-18T16:00:01Z  270 ms    ValueError",
        "  … and 2 more steps, timed but not listed",
        "",
        "counts",
        "  files_listed    12",
        "  records_stored  9",
        "",
        "llm: 2 calls, 1200 input tokens, 80 output tokens",
        "  local-model: 2 calls, 1200 in, 80 out",
    ]


def test_run_json_is_the_answer(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(RUN))

    result = cli("run", RUN_ID, "--json")

    assert json.loads(result.out) == RUN
    assert "\x1b" not in result.out


def test_a_report_of_another_version_is_shown_as_json(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok({**RUN, "report": {"version": 2, "phases": ["a"]}}))

    result = cli("run", RUN_ID)

    assert "report (version 2, shown as JSON):" in result.out
    assert '"phases": [' in result.out


def test_runs_lists_runs_and_the_next_page(cli: Callable[..., Run], service: FakeService) -> None:
    summary = {k: v for k, v in RUN.items() if k not in ("workspace", "report")}
    service.answer(ok({"runs": [summary], "next_before": "cursor-2"}))

    result = cli("runs", "acme", "--source", "shop", "--limit", "1")

    assert service.last.url.params.multi_items() == [("source", "shop"), ("limit", "1")]
    header, row = result.out.splitlines()
    assert header.split() == [
        "ID",
        "KIND",
        "STATUS",
        "SOURCE",
        "STREAM",
        "STARTED",
        "DURATION",
        "ERROR",
    ]
    assert row.split()[:7] == [
        RUN_ID,
        "git_sync",
        "partial",
        "shop",
        "refs/heads/main",
        "2026-11-18T16:00Z",
        "1.5",
    ]
    assert "more runs: add --before cursor-2" in result.err


def test_search_prints_hits_with_escaped_snippets(
    cli: Callable[..., Run], service: FakeService
) -> None:
    hit: Json = {
        "workspace": "acme",
        "source": "shop",
        "kind": "code_file",
        "locator": "shop@3f2a1b2:src/lamp.py#L1-L9",
        "occurred_at": "2026-01-02T12:00:00Z",
        "snippet": "\n".join(f"line {n}\x1b[2J" for n in range(1, 9)),
        "truncated": False,
    }
    service.answer(ok({"hits": [hit, {**hit, "snippet": "short"}]}))

    result = cli("search", "--code", "lamp", "--mode", "exact", "--language", "python")

    assert service.last.url.path == "/v1/search/code"
    assert service.last.url.params.multi_items() == [
        ("query", "lamp"),
        ("limit", "10"),
        ("mode", "exact"),
        ("languages", "python"),
    ]
    lines = result.out.splitlines()
    assert lines[0] == "shop@3f2a1b2:src/lamp.py#L1-L9  (code_file, acme/shop, 2026-01-02T12:00Z)"
    assert lines[1] == "    line 1\\x1b[2J"
    assert lines[7] == "    …"
    assert lines[9:] == [lines[0], "    short"]
    assert "\x1b" not in result.out


def test_search_prints_the_link_of_a_hit_inside_a_thread(
    cli: Callable[..., Run], service: FakeService
) -> None:
    hit: Json = {
        "workspace": "acme",
        "source": "chat",
        "kind": "thread",
        "locator": "chat:greenhouse/1#msg-2",
        "anchor": "msg-2",
        "link": "https://chat.example.invalid/m/2\x1b[2J",
        "occurred_at": "2026-03-03T18:01:00Z",
        "at": "2026-03-02T12:10:00Z",
        "snippet": "Bought the hose.",
        "truncated": False,
    }
    service.answer(ok({"hits": [hit]}))

    result = cli("search", "hose", "--kind", "thread")

    assert result.out.splitlines() == [
        "chat:greenhouse/1#msg-2  (thread, acme/chat, 2026-03-03T18:01Z)",
        "  link: https://chat.example.invalid/m/2\\x1b[2J",
        "    Bought the hose.",
    ]


@pytest.mark.parametrize(
    ("argv", "complaint"),
    [
        (("search", "lamp", "--mode", "exact"), "--mode exact is for search --code"),
        (("search", "--code", "lamp", "--mode", "hybrid"), "--mode hybrid is for search"),
        (("search", "lamp", "--path", "src/**"), "--path is for search --code only"),
        (("search", "--code", "lamp", "--kind", "thread"), "--kind is for search only"),
    ],
)
def test_search_refuses_options_of_the_other_tool(
    cli: Callable[..., Run], service: FakeService, argv: tuple[str, ...], complaint: str
) -> None:
    result = cli(*argv)

    assert result.code == EXIT_USAGE
    assert complaint in result.err
    assert service.requests == []


# --- token ---------------------------------------------------------------------------------------


def token_out(**changes: Any) -> Json:
    return {
        "id": "0193a1b2-0000-7000-8000-0000000000a1",
        "name": "laptop agent",
        "prefix": OTHER_TOKEN[:15],
        "scopes": ["read"],
        "workspace": "acme",
        "status": "active",
        "created_at": "2026-11-18T16:00:00Z",
        "expires_at": None,
        "revoked_at": None,
        "last_used_at": None,
        **changes,
    }


def test_token_create_prints_the_token_alone_on_stdout(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok({**token_out(), "token": OTHER_TOKEN}, 201))

    result = cli(
        "token", "create", "--scope", "read", "--workspace", "acme", "--name", "laptop agent",
        "--expires-in", "1d",
    )  # fmt: skip

    assert result.code == EXIT_OK
    assert json.loads(service.last.content) == {
        "scopes": ["read"],
        "name": "laptop agent",
        "workspace": "acme",
        "expires_in_seconds": 86400,
    }
    assert result.out == f"{OTHER_TOKEN}\n"
    assert OTHER_TOKEN[15:] not in result.err
    assert TOKEN not in result.all
    assert "named 'laptop agent': prefix" in result.err
    assert "workspace acme" in result.err
    assert "cannot be shown again" in result.err


def test_token_create_prints_nothing_without_a_token_in_the_answer(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok(token_out(), 201))

    result = cli("token", "create", "--scope", "read")

    assert result.code == EXIT_FAILURE
    assert result.out == ""


def test_token_list_shows_no_secret(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok({"tokens": [token_out(), token_out(workspace=None, status="revoked")]}))

    result = cli("token", "list")

    lines = result.out.splitlines()
    assert lines[1].split()[1:7] == [OTHER_TOKEN[:15], "laptop", "agent", "read", "acme", "active"]
    assert lines[2].split()[5:7] == ["*", "revoked"]
    assert OTHER_TOKEN[15:] not in result.all
    assert TOKEN not in result.all


@pytest.mark.parametrize("by", ["prefix", "id", "whole token"])
def test_token_revoke_finds_the_token_and_revokes_it_by_id(
    cli: Callable[..., Run], service: FakeService, by: str
) -> None:
    listed = token_out()
    reference = {"prefix": OTHER_TOKEN[:15], "id": listed["id"], "whole token": OTHER_TOKEN}[by]
    service.answer(ok({"tokens": [listed]}), ok({**listed, "revoked_at": "2026-11-18T17:00:00Z"}))

    result = cli("token", "revoke", reference)

    assert result.code == EXIT_OK
    assert service.last.method == "DELETE"
    assert service.last.url.path == f"/v1/tokens/{listed['id']}"
    assert result.out == f"revoked token {listed['id']} ({OTHER_TOKEN[:15]}, 'laptop agent')\n"
    assert OTHER_TOKEN[15:] not in result.all


def test_token_revoke_refuses_an_unknown_token(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(ok({"tokens": [token_out()]}))

    result = cli("token", "revoke", "memini_zzzzzzzz")

    assert result.code == EXIT_FAILURE
    assert result.err == "memini: no token memini_zzzzzzzz; see `memini token list`\n"
    assert len(service.requests) == 1


# --- forget --------------------------------------------------------------------------------------

RECORD = "ab" * 32
FORGOTTEN: Json = {
    "id": "0193a1b2-0000-7000-8000-00000000f0f0",
    "workspace": "acme",
    "record_id": RECORD,
    "source_id": "0193a1b2-0000-7000-8000-00000000c0c0",
    "source": "chat",
    "record_kind": "thread",
    "forgotten_at": "2026-11-18T16:00:00Z",
    "removed": {"chunks": 3, "symbols": 0, "raw_responses": 0},
    "citing_items": 1,
}


def test_forget_by_record_id(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(FORGOTTEN))

    result = cli("forget", "acme", "--record", RECORD)

    assert result.code == EXIT_OK, result.err
    assert service.last.method == "POST"
    assert service.last.url.path == "/v1/workspaces/acme/forget"
    assert json.loads(service.last.content) == {"record_id": RECORD}
    lines = [line.split(None, 1) for line in result.out.splitlines()]
    assert ["forgot", RECORD] in lines
    assert [line for line in result.out.splitlines() if line.startswith("chunks deleted")] == [
        "chunks deleted             3"
    ]
    assert "knowledge items citing it  1" in result.out


def test_forget_by_source_and_external_id(cli: Callable[..., Run], service: FakeService) -> None:
    service.answer(ok(FORGOTTEN))

    result = cli(
        "forget", "acme", "--source", "chat", "--external-id", "garden/100", "--kind", "thread",
        "--json",
    )  # fmt: skip

    assert result.code == EXIT_OK, result.err
    assert json.loads(service.last.content) == {
        "source": "chat",
        "external_id": "garden/100",
        "record_kind": "thread",
    }
    assert json.loads(result.out) == FORGOTTEN


@pytest.mark.parametrize(
    "argv",
    [
        ("forget", "acme"),
        ("forget", "acme", "--record", "XYZ"),
        ("forget", "acme", "--source", "chat"),
        ("forget", "acme", "--record", RECORD, "--kind", "thread"),
        ("forget", "acme", "--record", RECORD, "--external-id", "garden/100"),
        ("forget", "acme", "--record", RECORD, "--source", "chat"),
        ("forget", "acme", "--external-id", "garden/100"),
    ],
)
def test_forget_names_its_record_one_way(
    cli: Callable[..., Run], service: FakeService, argv: tuple[str, ...]
) -> None:
    result = cli(*argv)

    assert result.code == EXIT_USAGE
    assert service.requests == []


# --- Errors --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "code", "line"),
    [
        (
            error(404, "not_found", "No such workspace."),
            EXIT_FAILURE,
            f"No such workspace. (not_found, HTTP 404, request {REQUEST_ID})",
        ),
        (
            error(409, "conflict", "A workspace of that name exists."),
            EXIT_FAILURE,
            f"A workspace of that name exists. (conflict, HTTP 409, request {REQUEST_ID})",
        ),
        (
            error(
                422,
                "validation_error",
                "The request is not valid.",
                [{"location": ["body", "name"], "type": "string_pattern_mismatch"}],
            ),
            EXIT_FAILURE,
            f"The request is not valid. (validation_error, HTTP 422, request {REQUEST_ID});"
            " not valid: body.name (string_pattern_mismatch)",
        ),
        (
            error(401, "invalid_token", "The bearer token is not valid."),
            EXIT_DENIED,
            f"The bearer token is not valid. (invalid_token, HTTP 401, request {REQUEST_ID});"
            " check MEMINI_TOKEN",
        ),
        (
            error(403, "insufficient_scope", "The token lacks the admin scope."),
            EXIT_DENIED,
            "The token lacks the admin scope. (insufficient_scope, HTTP 403, request"
            f" {REQUEST_ID})",
        ),
        (
            error(403, "workspace_limited", "The token is limited to one workspace."),
            EXIT_DENIED,
            "The token is limited to one workspace. (workspace_limited, HTTP 403, request"
            f" {REQUEST_ID})",
        ),
        (
            error(503, "unavailable", "The database is not available."),
            EXIT_UNAVAILABLE,
            f"The database is not available. (unavailable, HTTP 503, request {REQUEST_ID})",
        ),
        (
            error(503, "index_mismatch", "The index is stale; run `memini reindex`."),
            EXIT_FAILURE,
            "The index is stale; run `memini reindex`. (index_mismatch, HTTP 503, request"
            f" {REQUEST_ID})",
        ),
        (
            error(500, "internal_error", "Internal server error."),
            EXIT_FAILURE,
            f"Internal server error. (internal_error, HTTP 500, request {REQUEST_ID})",
        ),
        (
            raw(502, "<html>Bad Gateway</html>"),
            EXIT_UNAVAILABLE,
            "the service at http://127.0.0.1:6364 is unavailable (HTTP 502)",
        ),
        (
            raw(404, "Not Found"),
            EXIT_FAILURE,
            "the service at http://127.0.0.1:6364 answered HTTP 404 without the API's error"
            " body; is MEMINI_URL the Memini service?",
        ),
        (
            raw(301, headers={"Location": "https://elsewhere.invalid"}),
            EXIT_USAGE,
            "the service at http://127.0.0.1:6364 answered with a redirect (HTTP 301), which is"
            " not followed; check MEMINI_URL",
        ),
        (
            raw(200, "<html>hello</html>"),
            EXIT_FAILURE,
            "the service at http://127.0.0.1:6364 answered HTTP 200 with something other than a"
            " JSON object; is MEMINI_URL the Memini service?",
        ),
    ],
)
def test_answers_map_to_exit_codes_and_one_line(
    cli: Callable[..., Run], service: FakeService, handler: Handler, code: int, line: str
) -> None:
    service.answer(handler)

    result = cli("workspace", "list")

    assert result.code == code
    assert result.out == ""
    assert result.err == f"memini: {line}\n"


def test_an_error_message_never_shows_the_token(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.answer(error(400, "bad_request", f"Echoed {TOKEN} and Bearer {TOKEN[7:]}\x1b[2J"))

    result = cli("workspace", "list")

    assert result.code == EXIT_FAILURE
    assert TOKEN not in result.all
    assert TOKEN[7:] not in result.all
    assert "\x1b" not in result.all


@pytest.mark.parametrize("debug", [False, True])
def test_an_unexpected_error_is_one_redacted_line(
    cli: Callable[..., Run], service: FakeService, debug: bool
) -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError(f"broke near {request.headers['Authorization']}")

    service.answer(broken)

    result = cli(*(["--debug"] if debug else []), "workspace", "list")

    assert result.code == EXIT_FAILURE
    assert result.err.startswith("memini: unexpected error: RuntimeError: broke near ")
    assert TOKEN not in result.all
    assert ("Traceback (most recent call last):" in result.err) is debug


@pytest.mark.parametrize(
    ("exception", "line"),
    [
        (
            httpx2.ConnectError("[Errno 61] Connection refused"),
            "cannot reach the service at http://127.0.0.1:6364: [Errno 61] Connection refused",
        ),
        (
            httpx2.ReadTimeout("timed out"),
            "the service at http://127.0.0.1:6364 did not answer within 60 seconds",
        ),
    ],
)
def test_a_service_that_cannot_be_reached_exits_3(
    cli: Callable[..., Run], service: FakeService, exception: Exception, line: str
) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise exception

    service.answer(fail)

    result = cli("workspace", "list")

    assert result.code == EXIT_UNAVAILABLE
    assert result.err == f"memini: {line}\n"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def test_a_real_connection_to_nothing_exits_3(
    capsys: pytest.CaptureFixture[str], env: pytest.MonkeyPatch
) -> None:
    url = f"http://127.0.0.1:{free_port()}"
    env.setenv("MEMINI_URL", url)

    code = main(["workspace", "list"])

    err = capsys.readouterr().err
    assert code == EXIT_UNAVAILABLE
    assert err.startswith(f"memini: cannot reach the service at {url}: ")
    assert len(err.splitlines()) == 1
    assert TOKEN not in err
