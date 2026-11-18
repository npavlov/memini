"""The operator commands of ``memini`` against a real Postgres: run with `make test-integration`.

Commands run in this process through ``main``, except ``serve``, which runs in a child process so
that it can be stopped with a signal; client commands talk to it over HTTP. Names are synthetic;
tokens are generated per test.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx2
import pytest

from memini.auth import Principal, authenticate, create_token, revoke_token
from memini.cli import main
from memini.cli.client import CLIENT_VARIABLES
from memini.cli.common import EXIT_DENIED, EXIT_FAILURE, EXIT_OK
from memini.config import environment_variables
from memini.db import connect, load_migrations
from memini.workspaces import create_workspace, ensure_principal

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, empty_database_url: str) -> str:
    """The test database in ``MEMINI_DATABASE_URL``; no other MEMINI_* setting from the shell."""
    for name in (*environment_variables(), *CLIENT_VARIABLES, "MEMINI_PRINCIPAL"):
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


def authenticate_token(url: str, token: str) -> Principal:
    async def resolve() -> Principal:
        async with connect(url) as conn:
            return await authenticate(conn, token)

    return asyncio.run(resolve())


def principal_id(url: str, name: str) -> UUID:
    async def find() -> UUID:
        async with connect(url) as conn:
            cur = await conn.execute("SELECT id FROM principal WHERE name = %s", (name,))
            row = await cur.fetchone()
            assert row is not None
            found: UUID = row[0]
            return found

    return asyncio.run(find())


def token_expiry(url: str, token_id: UUID) -> datetime | None:
    async def find() -> datetime | None:
        async with connect(url) as conn:
            cur = await conn.execute("SELECT expires_at FROM token WHERE id = %s", (token_id,))
            row = await cur.fetchone()
            assert row is not None
            found: datetime | None = row[0]
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
        (("bootstrap",), "run `memini migrate` first"),
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


# --- bootstrap -----------------------------------------------------------------------------------


def bootstrap(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    result = invoke(capsys, "bootstrap", *argv)
    assert result.code == EXIT_OK, result.err
    return result.out.strip()


def test_bootstrap_prints_the_first_admin_token_alone_and_once(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result = invoke(capsys, "bootstrap")

    token = result.out.removesuffix("\n")
    assert result.code == EXIT_OK
    assert result.out == f"{token}\n"
    assert token.startswith("memini_")
    assert token[15:] not in result.err
    assert "named 'bootstrap' for principal owner" in result.err
    assert f"prefix {token[:15]}, scopes read, admin, every workspace, expires never" in result.err
    assert "cannot be shown again" in result.err

    principal = authenticate_token(migrated, token)
    assert principal.scopes == frozenset({"read", "admin"})
    assert principal.workspace_id is None
    assert principal.principal_id == principal_id(migrated, "owner")


def test_bootstrap_refuses_while_an_admin_token_exists_unless_forced(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    first = bootstrap(capsys)

    again = invoke(capsys, "bootstrap")
    forced = invoke(capsys, "bootstrap", "--force", "--name", "second operator")

    assert again.code == EXIT_FAILURE
    assert again.out == ""
    assert again.err == (
        f"memini: an active admin token exists ({first[:15]}); manage tokens with `memini token`"
        " through the API, or pass --force to create another one\n"
    )
    assert forced.code == EXIT_OK
    second = forced.out.strip()
    assert second != first
    assert authenticate_token(migrated, second).scopes == frozenset({"read", "admin"})
    assert (
        authenticate_token(migrated, first).token_id
        != authenticate_token(migrated, second).token_id
    )


def test_bootstrap_counts_only_active_admin_tokens_for_every_workspace(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A revoked or expired admin token, one limited to a workspace, a read token or another
    principal's token does not stand in the way: none of them can set up what the first token is
    for."""

    async def tokens() -> None:
        async with connect(migrated) as conn:
            owner = await ensure_principal(conn, "owner")
            alpha = await create_workspace(conn, owner_id=owner, name="alpha")
            revoked = await create_token(conn, owner_id=owner, name="old", scopes=["admin"])
            await revoke_token(conn, owner_id=owner, token_id=revoked.info.id)
            await create_token(
                conn, owner_id=owner, name="alpha admin", scopes=["admin"], workspace_id=alpha.id
            )
            await create_token(conn, owner_id=owner, name="agent", scopes=["read", "ingest"])
            await create_token(
                conn,
                owner_id=owner,
                name="expiring",
                scopes=["admin"],
                expires_at=datetime.now(UTC) + timedelta(seconds=1),
            )
            other = await ensure_principal(conn, "someone-else")
            await create_token(conn, owner_id=other, name="theirs", scopes=["admin"])

    asyncio.run(tokens())
    time.sleep(1.5)  # the expiring token expires

    result = invoke(capsys, "bootstrap")

    assert result.code == EXIT_OK, result.err


def test_bootstrap_acts_as_the_configured_principal_with_an_expiry(
    migrated: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap(capsys)
    monkeypatch.setenv("MEMINI_PRINCIPAL", "ops")

    result = invoke(capsys, "bootstrap", "--expires-in", "1d")

    assert result.code == EXIT_OK, result.err
    assert "for principal ops" in result.err
    principal = authenticate_token(migrated, result.out.strip())
    assert principal.principal_id == principal_id(migrated, "ops")
    expires_at = token_expiry(migrated, principal.token_id)
    assert expires_at is not None
    assert timedelta(hours=23) < expires_at - datetime.now(UTC) <= timedelta(days=1)


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
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = start_service(cli_env, "--migrate")
    try:
        admin = bootstrap(capsys)
        token_file = tmp_path / "admin.token"
        token_file.write_text(f"{admin}\n", encoding="utf-8")
        # The client commands need the service's URL and a token, and no database URL.
        monkeypatch.delenv("MEMINI_DATABASE_URL")
        monkeypatch.setenv("MEMINI_URL", service.base_url)
        monkeypatch.setenv("MEMINI_TOKEN_FILE", str(token_file))
        created = invoke(capsys, "workspace", "create", "alpha")
        token = invoke(capsys, "token", "create", "--scope", "read", "--name", "agent").out.strip()
        listed = invoke(capsys, "workspace", "list")
        monkeypatch.delenv("MEMINI_TOKEN_FILE")
        monkeypatch.setenv("MEMINI_TOKEN", token)
        read_only = invoke(capsys, "workspace", "create", "beta")

        anonymous = httpx2.get(f"{service.base_url}/v1/workspaces", timeout=5)
    finally:
        code, err = service.stop(signal.SIGTERM)

    assert created.code == EXIT_OK, created.err
    assert created.out.startswith("created workspace alpha (")
    assert [line.split()[0] for line in listed.out.splitlines()] == ["NAME", "alpha"]
    assert read_only.code == EXIT_DENIED
    assert "(insufficient_scope, HTTP 403" in read_only.err
    assert anonymous.status_code == 401
    assert code == 0, err
    assert "Traceback" not in err
    assert "memini stopped" in err  # logged after the pool closed
    assert "migrations applied" in err
    lines = [json.loads(line) for line in err.splitlines() if line.strip()]
    assert all({"timestamp", "level", "logger", "message"} <= line.keys() for line in lines)
    assert token not in err
    assert admin not in err


def test_serve_stops_on_sigint(migrated: str) -> None:
    service = start_service(migrated)

    code, err = service.stop(signal.SIGINT)

    assert code == 0, err
    assert "Traceback" not in err
    assert "KeyboardInterrupt" not in err
    assert "memini stopped" in err
