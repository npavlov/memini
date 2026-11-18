"""The ``memini`` command against a real Postgres: run with `make test-integration`.

Commands run in this process through ``main``, except ``serve``, which runs in a child process so
that it can be stopped with a signal. Names are synthetic; tokens are generated per test.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from uuid import UUID

import httpx2
import pytest

from memini.auth import AuthenticationError, Principal, authenticate
from memini.cli import main
from memini.cli.common import EXIT_FAILURE, EXIT_OK
from memini.config import environment_variables
from memini.db import connect, load_migrations

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, empty_database_url: str) -> str:
    """The test database in ``MEMINI_DATABASE_URL``; no other MEMINI_* setting from the shell."""
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", empty_database_url)
    return empty_database_url


@pytest.fixture
def migrated(cli_env: str, capsys: pytest.CaptureFixture[str]) -> str:
    """The test database, migrated with ``memini migrate``."""
    assert invoke(capsys, "migrate").code == EXIT_OK
    return cli_env


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> Run:
    capsys.readouterr()
    code = main(list(argv))
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def create_token(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    result = invoke(capsys, "token", "create", *argv)
    assert result.code == EXIT_OK, result.err
    return result.out.strip()


def authenticate_token(url: str, token: str) -> Principal:
    async def resolve() -> Principal:
        async with connect(url) as conn:
            return await authenticate(conn, token)

    return asyncio.run(resolve())


def workspace_id(url: str, name: str) -> UUID:
    async def find() -> UUID:
        async with connect(url) as conn:
            cur = await conn.execute("SELECT id FROM workspace WHERE name = %s", (name,))
            row = await cur.fetchone()
            assert row is not None
            found: UUID = row[0]
            return found

    return asyncio.run(find())


# --- migrate -------------------------------------------------------------------------------------


def test_migrate_applies_every_migration_then_has_nothing_to_do(
    cli_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    first = invoke(capsys, "migrate")
    second = invoke(capsys, "migrate")

    assert first.code == EXIT_OK
    assert first.out.splitlines() == [f"applied {m.file_name}" for m in load_migrations()]
    assert second.code == EXIT_OK
    assert second.out == "no pending migrations; the database is up to date\n"


@pytest.mark.parametrize(
    ("argv", "hint"),
    [
        (("workspace", "list"), "run `memini migrate` first"),
        (("token", "create", "--scope", "read"), "run `memini migrate` first"),
        (("serve",), "run `memini migrate`, or start with `memini serve --migrate`"),
    ],
)
def test_commands_refuse_a_database_with_pending_migrations(
    cli_env: str, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], hint: str
) -> None:
    result = invoke(capsys, *argv)

    first = load_migrations()[0].file_name
    assert result.code == EXIT_FAILURE
    assert result.out == ""
    assert result.err.startswith(f"memini: the database has pending migrations ({first}, ")
    assert result.err.endswith(f"{hint}\n")


# --- workspace -----------------------------------------------------------------------------------


def test_workspace_create_and_list(migrated: str, capsys: pytest.CaptureFixture[str]) -> None:
    empty = invoke(capsys, "workspace", "list")
    created = [invoke(capsys, "workspace", "create", name) for name in ("beta", "alpha")]
    listed = invoke(capsys, "workspace", "list")

    assert empty.code == EXIT_OK
    assert empty.out == ""
    assert "no workspaces yet" in empty.err
    assert [r.code for r in created] == [EXIT_OK, EXIT_OK]
    assert created[0].out == f"created workspace beta ({workspace_id(migrated, 'beta')})\n"
    assert listed.code == EXIT_OK
    lines = listed.out.splitlines()
    assert lines[0].split() == ["NAME", "CREATED", "ID"]
    assert [line.split()[0] for line in lines[1:]] == ["alpha", "beta"]
    assert lines[1].split()[2] == str(workspace_id(migrated, "alpha"))


def test_workspace_create_refuses_a_taken_name(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke(capsys, "workspace", "create", "alpha")

    again = invoke(capsys, "workspace", "create", "alpha")

    assert again.code == EXIT_FAILURE
    assert again.err == "memini: a workspace named alpha exists\n"


def test_workspaces_and_tokens_belong_to_the_configured_principal(
    migrated: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    invoke(capsys, "workspace", "create", "alpha")
    create_token(capsys, "--scope", "read")

    monkeypatch.setenv("MEMINI_PRINCIPAL", "someone-else")
    other_workspaces = invoke(capsys, "workspace", "list")
    other_tokens = invoke(capsys, "token", "list")
    limited = invoke(capsys, "token", "create", "--scope", "read", "--workspace", "alpha")

    assert (other_workspaces.out, other_tokens.out) == ("", "")
    assert limited.code == EXIT_FAILURE
    assert limited.err == "memini: no workspace named alpha; see `memini workspace list`\n"


# --- token ---------------------------------------------------------------------------------------


def test_token_create_prints_the_token_alone_and_once(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result = invoke(capsys, "token", "create", "--scope", "read")

    token = result.out.removesuffix("\n")
    assert result.code == EXIT_OK
    assert result.out == f"{token}\n"
    assert token.startswith("memini_")
    assert token not in result.err
    assert token[15:] not in result.err
    assert "cannot be shown again" in result.err
    assert f"prefix {token[:15]}" in result.err

    principal = authenticate_token(migrated, token)
    assert principal.scopes == frozenset({"read"})
    assert principal.workspace_id is None


def test_token_create_with_workspace_scopes_name_and_expiry(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke(capsys, "workspace", "create", "alpha")

    result = invoke(
        capsys,
        "token",
        "create",
        "--scope",
        "ingest",
        "--scope",
        "read",
        "--workspace",
        "alpha",
        "--name",
        "test agent",
        "--expires-in",
        "1d12h",
    )

    assert result.code == EXIT_OK
    assert "named 'test agent'" in result.err
    assert "scopes read, ingest" in result.err
    assert "workspace alpha" in result.err
    principal = authenticate_token(migrated, result.out.strip())
    assert principal.scopes == frozenset({"read", "ingest"})
    assert principal.workspace_id == workspace_id(migrated, "alpha")


def test_token_list_shows_tokens_without_secrets(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke(capsys, "workspace", "create", "alpha")
    every = create_token(capsys, "--scope", "read", "--scope", "admin")
    limited = create_token(
        capsys, "--scope", "read", "--workspace", "alpha", "--name", "test agent"
    )
    authenticate_token(migrated, every)

    result = invoke(capsys, "token", "list")

    assert result.code == EXIT_OK
    lines = result.out.splitlines()
    assert lines[0].split() == [
        "ID",
        "PREFIX",
        "NAME",
        "SCOPES",
        "WORKSPACE",
        "STATUS",
        "CREATED",
        "EXPIRES",
        "LAST",
        "USED",
    ]
    first, second = lines[1].split(), lines[2].split()
    assert first[1:6] == [every[:15], "cli", "read,admin", "*", "active"]
    assert first[7] == "never"  # expires
    assert first[8] != "never"  # last used: authenticated once, above
    assert second[1:7] == [limited[:15], "test", "agent", "read", "alpha", "active"]
    for token in (every, limited):
        assert token not in result.out + result.err
        assert token[15:] not in result.out + result.err


@pytest.mark.parametrize("by", ["prefix", "id", "whole token"])
def test_token_revoke(migrated: str, capsys: pytest.CaptureFixture[str], by: str) -> None:
    token = create_token(capsys, "--scope", "read")
    kept = create_token(capsys, "--scope", "read")
    token_id = authenticate_token(migrated, token).token_id
    reference = {"prefix": token[:15], "id": str(token_id), "whole token": token}[by]

    result = invoke(capsys, "token", "revoke", reference)

    assert result.code == EXIT_OK
    assert result.out == f"revoked token {token_id} ({token[:15]}, 'cli')\n"
    assert token[15:] not in result.out + result.err
    with pytest.raises(AuthenticationError) as refused:
        authenticate_token(migrated, token)
    assert refused.value.reason == "revoked"
    assert authenticate_token(migrated, kept).token_id != token_id

    again = invoke(capsys, "token", "revoke", reference)
    listed = invoke(capsys, "token", "list")

    assert again.code == EXIT_OK
    assert again.out.startswith(f"token {token_id} ({token[:15]}) was revoked already, at ")
    assert listed.out.splitlines()[1].split()[5] == "revoked"


def test_token_revoke_refuses_an_unknown_token(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    create_token(capsys, "--scope", "read")

    by_prefix = invoke(capsys, "token", "revoke", "memini_zzzzzzzz")
    by_id = invoke(capsys, "token", "revoke", "00000000-0000-7000-8000-0000000000e0")

    assert (by_prefix.code, by_id.code) == (EXIT_FAILURE, EXIT_FAILURE)
    assert by_prefix.err == "memini: no token memini_zzzzzzzz; see `memini token list`\n"


def test_token_create_refuses_an_unknown_workspace(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result = invoke(capsys, "token", "create", "--scope", "read", "--workspace", "alpha")

    assert result.code == EXIT_FAILURE
    assert result.out == ""
    assert result.err == "memini: no workspace named alpha; see `memini workspace list`\n"


# --- serve ---------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@dataclass
class Service:
    process: subprocess.Popen[str]
    base_url: str

    def stop(self, signal_number: int) -> tuple[int, str]:
        self.process.send_signal(signal_number)
        _, err = self.process.communicate(timeout=30)
        return self.process.returncode, err


def start_service(url: str, *argv: str) -> Service:
    """Start ``memini serve`` in a child process and wait until ``/readyz`` answers 200."""
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMINI_")}
    env |= {
        "MEMINI_DATABASE_URL": url,
        "MEMINI_HTTP_HOST": "127.0.0.1",
        "MEMINI_HTTP_PORT": str(port),
        "MEMINI_LOG_LEVEL": "INFO",
    }
    process = subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments
        [sys.executable, "-m", "memini", "serve", *argv],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    service = Service(process, f"http://127.0.0.1:{port}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _, err = process.communicate()
            pytest.fail(f"memini serve exited with {process.returncode}:\n{err}")
        try:
            if httpx2.get(f"{service.base_url}/readyz", timeout=1).status_code == 200:
                return service
        except httpx2.TransportError:
            pass
        time.sleep(0.1)
    process.kill()
    _, err = process.communicate()
    pytest.fail(f"memini serve was not ready within 30 seconds:\n{err}")


def test_serve_migrates_answers_and_stops_on_sigterm(
    cli_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    service = start_service(cli_env, "--migrate")
    try:
        invoke(capsys, "workspace", "create", "alpha")
        token = create_token(capsys, "--scope", "read")

        listed = httpx2.get(
            f"{service.base_url}/v1/workspaces",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        anonymous = httpx2.get(f"{service.base_url}/v1/workspaces", timeout=5)
    finally:
        code, err = service.stop(signal.SIGTERM)

    assert listed.status_code == 200
    assert [w["name"] for w in listed.json()["workspaces"]] == ["alpha"]
    assert anonymous.status_code == 401
    assert code == 0, err
    assert "Traceback" not in err
    assert "memini stopped" in err  # logged after the pool closed
    assert "migrations applied" in err
    lines = [json.loads(line) for line in err.splitlines() if line.strip()]
    assert all({"timestamp", "level", "logger", "message"} <= line.keys() for line in lines)
    assert token not in err


def test_serve_stops_on_sigint(migrated: str) -> None:
    service = start_service(migrated)

    code, err = service.stop(signal.SIGINT)

    assert code == 0, err
    assert "Traceback" not in err
    assert "KeyboardInterrupt" not in err
    assert "memini stopped" in err
