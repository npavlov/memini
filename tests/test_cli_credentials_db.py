"""``memini credentials`` against a real Postgres. Keys, tokens and names are synthetic."""

import asyncio
import base64
import io
import json
import uuid
from dataclasses import dataclass
from typing import Final

import pytest
from pydantic import SecretStr

from memini.cli import main
from memini.cli.common import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from memini.config import environment_variables
from memini.credentials import CredentialKey, Keyring, key_id, read_api_key, read_oauth
from memini.db import connect
from memini.workspaces import create_workspace, ensure_principal

pytestmark = pytest.mark.integration

KEY_A: Final = bytes(range(1, 33))
KEY_B: Final = bytes(range(33, 65))
TEXT_A: Final = base64.b64encode(KEY_A).decode()
TEXT_B: Final = base64.b64encode(KEY_B).decode()


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


@pytest.fixture
def migrated(
    monkeypatch: pytest.MonkeyPatch, empty_database_url: str, capsys: pytest.CaptureFixture[str]
) -> str:
    """A migrated test database with the workspace ``acme`` and its sources ``chat`` and
    ``mail``; no MEMINI_* setting from the shell."""
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", empty_database_url)
    assert invoke(capsys, monkeypatch, "migrate").code == EXIT_OK

    async def add_sources() -> None:
        async with connect(empty_database_url) as conn:
            # The CLI's principal; the workspace comes from the library, as `workspace create`
            # is a client of the API, and these operator commands run without the service.
            owner = await ensure_principal(conn, "owner")
            await create_workspace(conn, owner_id=owner, name="acme")
            for name in ("chat", "mail"):
                await conn.execute(
                    "INSERT INTO source (workspace_id, owner_id, name, kind)"
                    " SELECT id, owner_id, %s, 'mcp' FROM workspace WHERE name = 'acme'",
                    (name,),
                )
            await conn.commit()

    asyncio.run(add_sources())
    return empty_database_url


def invoke(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *argv: str,
    stdin: str = "",
) -> Run:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    capsys.readouterr()
    code = main(list(argv))
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def source_id(url: str, name: str) -> uuid.UUID:
    async def find() -> uuid.UUID:
        async with connect(url) as conn:
            cur = await conn.execute("SELECT id FROM source WHERE name = %s", (name,))
            row = await cur.fetchone()
            assert row is not None
            found: uuid.UUID = row[0]
            return found

    return asyncio.run(find())


def secret(label: str) -> str:
    return f"fake-{label}-{uuid.uuid4().hex}"


SET_CHAT = ("credentials", "set", "--workspace", "acme", "--source", "chat")
DELETE_CHAT = ("credentials", "delete", "--workspace", "acme", "--source", "chat")


def test_set_list_and_delete_an_api_key_without_printing_it(
    migrated: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMINI_CREDENTIAL_KEY", TEXT_A)
    api_key = secret("api-key")

    stored = invoke(capsys, monkeypatch, *SET_CHAT, "--kind", "api_key", stdin=api_key + "\n")
    listed = invoke(capsys, monkeypatch, "credentials", "list")

    chat = source_id(migrated, "chat")

    async def read() -> str:
        async with connect(migrated) as conn:
            found = await read_api_key(conn, Keyring(CredentialKey(KEY_A)), source_id=chat)
            return found.api_key.get_secret_value()

    assert stored.code == EXIT_OK, stored.err
    assert stored.out == (
        f"stored the api_key credential of source chat in workspace acme (generation 1,"
        f" key {key_id(KEY_A)})\n"
    )
    assert asyncio.run(read()) == api_key
    assert listed.code == EXIT_OK
    assert listed.out.splitlines()[0].split() == [
        "WORKSPACE",
        "SOURCE",
        "KIND",
        "GENERATION",
        "KEY",
        "EXPIRES",
        "UPDATED",
        "REFRESHED",
    ]
    assert listed.out.splitlines()[1].split()[:6] == [
        "acme",
        "chat",
        "api_key",
        "1",
        key_id(KEY_A),
        "-",
    ]

    deleted = invoke(capsys, monkeypatch, *DELETE_CHAT)
    gone = invoke(capsys, monkeypatch, *DELETE_CHAT)

    assert deleted.code == EXIT_OK
    assert gone.code == EXIT_FAILURE
    assert gone.err == "memini: source chat in workspace acme has no credential\n"
    for run in (stored, listed, deleted, gone):
        assert api_key not in run.out + run.err


def test_set_oauth_tokens_from_a_token_response(
    migrated: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMINI_CREDENTIAL_KEY", TEXT_A)
    access, refresh, client_secret = secret("access"), secret("refresh"), secret("client")
    response = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": refresh,
        "token_endpoint": "https://auth.example.test/token",
        "client_id": "client-1",
        "client_secret": client_secret,
        "scope": "read",
    }

    run = invoke(capsys, monkeypatch, *SET_CHAT, "--kind", "oauth", stdin=json.dumps(response))

    chat = source_id(migrated, "chat")

    async def read() -> tuple[str, str | None, str | None]:
        async with connect(migrated) as conn:
            found = await read_oauth(conn, Keyring(CredentialKey(KEY_A)), source_id=chat)
        refresh_token = found.tokens.refresh_token
        return (
            found.tokens.access_token.get_secret_value(),
            None if refresh_token is None else refresh_token.get_secret_value(),
            found.client.token_endpoint,
        )

    assert run.code == EXIT_OK, run.err
    assert asyncio.run(read()) == (access, refresh, "https://auth.example.test/token")
    for value in (access, refresh, client_secret):
        assert value not in run.out + run.err


@pytest.mark.parametrize(
    ("stdin", "error"),
    [
        ("not json", "stdin is not a JSON object"),
        ('{"refresh_token": "x"}', "access_token: required"),
        ('{"access_token": "a", "fake-token-in-a-key": 1}', "stdin holds 1 unknown fields"),
        ('{"access_token": "a", "expires_in": -1}', "expires_in: must be a whole number"),
        ('{"access_token": "a b"}', "access_token: holds whitespace or control characters"),
        (
            '{"access_token": "a", "token_endpoint": "https://u:fake-pass@auth.example.test/t"}',
            "token_endpoint: holds credentials",
        ),
    ],
)
def test_invalid_oauth_input_is_refused_without_echoing_it(
    migrated: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
    error: str,
) -> None:
    monkeypatch.setenv("MEMINI_CREDENTIAL_KEY", TEXT_A)

    run = invoke(capsys, monkeypatch, *SET_CHAT, "--kind", "oauth", stdin=stdin)

    assert run.code == EXIT_USAGE
    assert run.err.startswith(f"memini: {error}")
    assert "fake-" not in run.err


def test_without_a_key_the_commands_that_need_one_exit_2(
    migrated: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = invoke(capsys, monkeypatch, *SET_CHAT, "--kind", "api_key", stdin=secret("k"))
    rotated = invoke(capsys, monkeypatch, "credentials", "rotate-key")
    listed = invoke(capsys, monkeypatch, "credentials", "list")

    for run in (stored, rotated):
        assert run.code == EXIT_USAGE
        assert "MEMINI_CREDENTIAL_KEY or MEMINI_CREDENTIAL_KEY_FILE" in run.err
    assert listed.code == EXIT_OK  # listing needs no key


def test_an_unknown_source_or_workspace_is_named(
    migrated: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMINI_CREDENTIAL_KEY", TEXT_A)
    args = ("credentials", "set", "--kind", "api_key")

    no_source = invoke(
        capsys, monkeypatch, *args, "--workspace", "acme", "--source", "nope", stdin=secret("k")
    )
    no_workspace = invoke(
        capsys, monkeypatch, *args, "--workspace", "nope", "--source", "chat", stdin=secret("k")
    )

    assert (no_source.code, no_source.err) == (
        EXIT_FAILURE,
        "memini: no source named nope in workspace acme\n",
    )
    assert no_workspace.code == EXIT_FAILURE
    assert no_workspace.err.startswith("memini: no workspace named nope")


def test_rotate_key_re_encrypts_under_the_new_key(
    migrated: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMINI_CREDENTIAL_KEY", TEXT_A)
    keys = {name: secret(f"api-key-{name}") for name in ("chat", "mail")}
    for name, value in keys.items():
        args = ("credentials", "set", "--workspace", "acme", "--source", name)
        run = invoke(capsys, monkeypatch, *args, "--kind", "api_key", stdin=value)
        assert run.code == EXIT_OK, run.err

    monkeypatch.setenv("MEMINI_CREDENTIAL_KEY", TEXT_B)
    blocked = invoke(capsys, monkeypatch, "credentials", "rotate-key")
    monkeypatch.setenv("MEMINI_CREDENTIAL_OLD_KEYS", TEXT_A)
    rotated = invoke(capsys, monkeypatch, "credentials", "rotate-key", "--batch-size", "1")
    monkeypatch.delenv("MEMINI_CREDENTIAL_OLD_KEYS")
    again = invoke(capsys, monkeypatch, "credentials", "rotate-key")

    ids = {name: source_id(migrated, name) for name in keys}

    async def read(name: str) -> SecretStr:
        async with connect(migrated) as conn:
            found = await read_api_key(conn, Keyring(CredentialKey(KEY_B)), source_id=ids[name])
            return found.api_key

    assert blocked.code == EXIT_FAILURE
    assert f"{key_id(KEY_A)} (2)" in blocked.err
    assert "MEMINI_CREDENTIAL_OLD_KEYS" in blocked.err
    assert rotated.code == EXIT_OK, rotated.err
    assert rotated.out == (
        f"re-encrypted 2 credentials under key {key_id(KEY_B)}; 0 were under it already\n"
    )
    assert "MEMINI_CREDENTIAL_OLD_KEYS can be removed" in rotated.err
    assert again.code == EXIT_OK
    assert again.out.startswith("re-encrypted 0 credentials")
    for name, value in keys.items():
        assert asyncio.run(read(name)).get_secret_value() == value
    for run in (blocked, rotated, again):
        for value in (*keys.values(), TEXT_A, TEXT_B):
            assert value not in run.out + run.err
