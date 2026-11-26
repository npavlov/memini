"""``memini person …`` against a fake service: the calls each command makes and what it prints.

The service is an ``httpx2.MockTransport``; nothing here needs a database or the network. The flow
against the real service is in test_cli_persons_db.py. Every value is synthetic; the token is
generated per run.
"""

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest

from memini.cli import main
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from memini.config import environment_variables

TOKEN = "memini_" + secrets.token_urlsafe(32)
ANN = "0193a1b2-0000-7000-8000-0000000000a1"
ANN_CHAT = "0193a1b2-0000-7000-8000-0000000000a2"
ALIAS = "0193a1b2-0000-7000-8000-0000000000b1"
PERSONS = "/v1/workspaces/acme/persons"

type Json = dict[str, Any]


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


@dataclass
class FakeService:
    """Answers every request with one handler and records the requests."""

    answer: Callable[[httpx2.Request], httpx2.Response] = field(
        default=lambda request: httpx2.Response(500)
    )
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.answer(request)

    def replies(self, body: Json | None, status: int = 200) -> None:
        if body is None:
            self.answer = lambda request: httpx2.Response(status)
        else:
            self.answer = lambda request: httpx2.Response(status, json=body)

    @property
    def last(self) -> httpx2.Request:
        return self.requests[-1]

    def sent(self) -> Json:
        body: Json = json.loads(self.last.content)
        return body


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    for name in (*environment_variables(), *CLIENT_VARIABLES):
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


def person(person_id: str = ANN, **fields: Any) -> Json:
    body: Json = {
        "id": person_id,
        "workspace": "acme",
        "display_name": "Ann Example",
        "canonical_id": person_id,
        "merged_into": None,
        "merged_at": None,
        "members": [],
        "aliases": [
            {
                "id": ALIAS,
                "person_id": person_id,
                "kind": "email",
                "value": "ann@example.test",
                "normalized_value": "ann@example.test",
                "created_at": "2026-03-01T09:00:00Z",
            }
        ],
        "is_me": False,
        "created_at": "2026-03-01T09:00:00Z",
    }
    return body | fields


def test_list_prints_persons_and_the_next_page(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.replies({"items": [person(is_me=True)], "next_cursor": "Y3Vyc29y"})

    run = cli("person", "list", "acme", "--query", "ann", "--merged", "--limit", "5")

    assert run.code == EXIT_OK, run.err
    request = service.last
    assert (request.method, request.url.path) == ("GET", PERSONS)
    assert dict(request.url.params) == {"query": "ann", "merged": "true", "limit": "5"}
    assert ANN in run.out
    assert "email:ann@example.test" in run.out
    assert "--cursor Y3Vyc29y" in run.err
    assert "Authorization" in request.headers
    assert TOKEN not in run.out + run.err


def test_show_and_lookup_print_the_person(cli: Callable[..., Run], service: FakeService) -> None:
    service.replies(person())

    shown = cli("person", "show", "acme", ANN)
    assert shown.code == EXIT_OK, shown.err
    assert service.last.url.path == f"{PERSONS}/{ANN}"
    assert "Ann Example" in shown.out
    assert "this person" in shown.out

    found = cli("person", "lookup", "acme", "chat_user_id", "U-ANN", "--json")
    assert found.code == EXIT_OK, found.err
    assert service.last.url.path == f"{PERSONS}/lookup"
    assert dict(service.last.url.params) == {"kind": "chat_user_id", "value": "U-ANN"}
    assert json.loads(found.out)["id"] == ANN

    assert cli("person", "show", "acme", "not-an-id").code == EXIT_USAGE
    assert cli("person", "lookup", "acme", "phone", "1").code == EXIT_USAGE


def test_alias_add_says_when_it_merged(cli: Callable[..., Run], service: FakeService) -> None:
    merged = person(members=[ANN_CHAT])
    service.replies(
        {"person": merged, "alias": merged["aliases"][0], "created": False, "merged": ANN_CHAT}
    )

    run = cli("person", "alias", "add", "acme", ANN, "chat_user_id", "U-ANN")

    assert run.code == EXIT_OK, run.err
    assert (service.last.method, service.last.url.path) == ("POST", f"{PERSONS}/{ANN}/aliases")
    assert service.sent() == {"kind": "chat_user_id", "value": "U-ANN"}
    assert f"memini person split acme {ANN_CHAT}" in run.err


def test_alias_remove_merge_and_split_call_their_routes(
    cli: Callable[..., Run], service: FakeService
) -> None:
    service.replies(person())

    assert cli("person", "alias", "remove", "acme", ANN, ALIAS).code == EXIT_OK
    assert (service.last.method, service.last.url.path) == (
        "DELETE",
        f"{PERSONS}/{ANN}/aliases/{ALIAS}",
    )
    assert cli("person", "merge", "acme", ANN_CHAT, "--into", ANN).code == EXIT_OK
    assert (service.last.method, service.last.url.path) == ("POST", f"{PERSONS}/{ANN_CHAT}/merge")
    assert service.sent() == {"into": ANN}
    assert cli("person", "split", "acme", ANN_CHAT).code == EXIT_OK
    assert (service.last.method, service.last.url.path) == ("POST", f"{PERSONS}/{ANN_CHAT}/split")
    assert cli("person", "merge", "acme", ANN_CHAT).code == EXIT_USAGE  # --into is required


def test_add_and_rename(cli: Callable[..., Run], service: FakeService) -> None:
    service.replies(person(), 201)

    run = cli("person", "add", "acme", "Ann Example", "--alias", "email=ann@example.test")
    assert run.code == EXIT_OK, run.err
    assert service.sent() == {
        "display_name": "Ann Example",
        "aliases": [{"kind": "email", "value": "ann@example.test"}],
    }
    assert cli("person", "add", "acme", "Ann", "--alias", "phone=1").code == EXIT_USAGE

    service.replies(person(display_name="A. Example"))
    assert cli("person", "rename", "acme", ANN, "A. Example").code == EXIT_OK
    assert (service.last.method, service.sent()) == ("PATCH", {"display_name": "A. Example"})


def test_me_shows_sets_and_unsets(cli: Callable[..., Run], service: FakeService) -> None:
    service.replies(person(is_me=True))
    assert cli("person", "me", "acme").code == EXIT_OK
    assert (service.last.method, service.last.url.path) == ("GET", f"{PERSONS}/me")

    assert cli("person", "me", "acme", "--set", ANN).code == EXIT_OK
    assert (service.last.method, service.sent()) == ("PUT", {"person_id": ANN})

    service.replies(None, 204)
    assert cli("person", "me", "acme", "--unset").code == EXIT_OK
    assert service.last.method == "DELETE"
    assert cli("person", "me", "acme", "--set", ANN, "--unset").code == EXIT_USAGE


def test_a_refusal_is_one_line_and_exit_1(cli: Callable[..., Run], service: FakeService) -> None:
    service.replies(
        {
            "error": {
                "code": "already_merged",
                "message": "The person is merged into another already; split it first.",
                "request_id": "5f0c3e2a9b7d4c1e8f6a2b3c4d5e6f70",
                "details": [],
            }
        },
        409,
    )

    run = cli("person", "merge", "acme", ANN_CHAT, "--into", ANN)

    assert run.code == EXIT_FAILURE
    assert "already_merged, HTTP 409" in run.err
    assert run.out == ""
