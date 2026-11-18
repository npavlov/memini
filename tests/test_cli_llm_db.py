"""``memini workspace llm`` against a real Postgres: run with `make test-integration`.

Names and URLs are synthetic; no provider is ever called.
"""

import asyncio
from dataclasses import dataclass

import pytest

from memini.cli import main
from memini.cli.common import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from memini.config import environment_variables
from memini.db import connect
from memini.workspaces import create_workspace, ensure_principal

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> Run:
    capsys.readouterr()
    code = main(list(argv))
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


@pytest.fixture
def alpha(
    monkeypatch: pytest.MonkeyPatch, empty_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A migrated test database with the workspace ``alpha``; no other MEMINI_* setting."""
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", empty_database_url)
    assert invoke(capsys, "migrate").code == EXIT_OK

    async def create_alpha() -> None:
        # The CLI's principal; the workspace comes from the library, as `workspace create` is a
        # client of the API, and `workspace llm` is an operator command that runs without it.
        async with connect(empty_database_url) as conn:
            owner = await ensure_principal(conn, "owner")
            await create_workspace(conn, owner_id=owner, name="alpha")

    asyncio.run(create_alpha())


def with_providers(monkeypatch: pytest.MonkeyPatch, *, cloud: bool = True) -> None:
    monkeypatch.setenv("MEMINI_LLM_LOCAL_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("MEMINI_LLM_LOCAL_MODEL", "synthetic-chat:7b")
    if cloud:
        monkeypatch.setenv("MEMINI_LLM_CLOUD_URL", "https://llm.example.com/v1")
        monkeypatch.setenv("MEMINI_LLM_CLOUD_MODEL", "synthetic-cloud-chat")


def test_shows_the_default_policy_and_that_no_provider_is_configured(
    alpha: None, capsys: pytest.CaptureFixture[str]
) -> None:
    result = invoke(capsys, "workspace", "llm", "alpha")

    assert result.code == EXIT_OK
    assert result.out == (
        "workspace alpha: llm_policy local\n"
        "  local provider: not configured\n"
        "  cloud provider: not configured\n"
    )


def test_opting_in_to_cloud_says_where_content_may_go(
    alpha: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with_providers(monkeypatch)

    result = invoke(capsys, "workspace", "llm", "alpha", "cloud")
    shown = invoke(capsys, "workspace", "llm", "alpha")

    assert result.code == EXIT_OK
    assert result.out == "workspace alpha: llm_policy local -> cloud\n"
    assert result.err == (
        "memini: content of alpha whose sources all have llm_policy cloud may now go to the"
        " cloud provider, model synthetic-cloud-chat at https://llm.example.com\n"
    )
    assert shown.out == (
        "workspace alpha: llm_policy cloud\n"
        "  local provider: model synthetic-chat:7b at http://127.0.0.1:11434\n"
        "  cloud provider: model synthetic-cloud-chat at https://llm.example.com\n"
    )


def test_opting_in_without_a_cloud_provider_says_content_stays_local(
    alpha: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with_providers(monkeypatch, cloud=False)

    result = invoke(capsys, "workspace", "llm", "alpha", "cloud")

    assert result.code == EXIT_OK
    assert result.err == (
        "memini: no cloud provider is configured (MEMINI_LLM_CLOUD_URL); until one is, content"
        " of alpha goes to the local provider only\n"
    )


def test_none_turns_every_llm_off(alpha: None, capsys: pytest.CaptureFixture[str]) -> None:
    result = invoke(capsys, "workspace", "llm", "alpha", "none")

    assert result.code == EXIT_OK
    assert result.out == "workspace alpha: llm_policy local -> none\n"
    assert result.err == "memini: no LLM reads content of alpha from now on\n"
    assert invoke(capsys, "workspace", "llm", "alpha").out.startswith(
        "workspace alpha: llm_policy none\n"
    )


def test_an_unknown_workspace_or_policy_is_refused(
    alpha: None, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = invoke(capsys, "workspace", "llm", "gamma", "cloud")
    bad = invoke(capsys, "workspace", "llm", "alpha", "remote")

    assert missing.code == EXIT_FAILURE
    assert missing.err == "memini: no workspace named gamma; see `memini workspace list`\n"
    assert bad.code == EXIT_USAGE
    assert "invalid choice: 'remote'" in bad.err
    assert invoke(capsys, "workspace", "llm", "alpha").out.startswith(
        "workspace alpha: llm_policy local\n"
    )
