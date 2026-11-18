"""Tests for the ``memini`` command that need no database. Every value here is synthetic."""

import argparse
import asyncio
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from importlib.metadata import entry_points
from pathlib import Path

import psycopg
import pytest

import memini
from memini.api import create_app
from memini.cli import admin, main
from memini.cli.common import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    DatabaseUrl,
    format_time,
    print_table,
)
from memini.cli.serve import run_http_server
from memini.config import Settings, environment_variables
from memini.db import MigrationError
from memini.embeddings import EmbeddingError, EmbeddingsNotConfiguredError, EmbeddingSpec
from memini.index import IndexFingerprint, IndexMismatchError, IndexState

# A distinctive password that must never be printed.
CANARY = "canary-not-a-real-secret-2d7e51"
SECRET_URL = f"postgresql://memini:{CANARY}@db.invalid:5432/memini"

STALE_STATE = IndexState(
    workspace_id=uuid.UUID(int=1),
    workspace="acme",
    fingerprint="0" * 64,
    built_with=IndexFingerprint(
        EmbeddingSpec(provider="openai_compatible", model="synthetic-embed", dimension=8), "1"
    ),
    created_at=datetime(2026, 1, 2, tzinfo=UTC),
    updated_at=datetime(2026, 1, 2, tzinfo=UTC),
)


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


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No MEMINI_* setting from the developer's shell reaches the command."""
    for name in (*environment_variables(), "MEMINI_PRINCIPAL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def unreachable(clean_env: pytest.MonkeyPatch) -> str:
    """A database URL with a distinctive password, on a local port where nothing listens."""
    target = f"127.0.0.1:{free_port()}"
    clean_env.setenv("MEMINI_DATABASE_URL", f"postgresql://memini:{CANARY}@{target}/memini")
    return target


# --- Entry points and arguments ------------------------------------------------------------------


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    result = invoke(capsys, "--version")

    assert result.code == EXIT_OK
    assert result.out == f"memini {memini.__version__}\n"


def test_console_script_runs_main() -> None:
    (script,) = entry_points(group="console_scripts", name="memini")

    assert script.load() is main


def test_python_dash_m_runs_main() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "memini", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stdout == f"memini {memini.__version__}\n"


@pytest.mark.parametrize(
    ("argv", "complaint"),
    [
        ((), "<command>"),
        (("workspace",), "<command>"),
        (("token", "create"), "--scope"),
        (("token", "create", "--scope", "write"), "invalid choice: 'write'"),
        (("token", "create", "--scope", "read", "--expires-in", "soon"), "a duration is"),
        (("token", "create", "--scope", "read", "--expires-in", "0h"), "longer than zero"),
        (("token", "create", "--scope", "read", "--name", "  "), "a token name"),
        (("token", "create", "--scope", "read", "--workspace", "No Such"), "a workspace name"),
        (("workspace", "create", "Alpha"), "a workspace name is"),
        (("token", "revoke"), "id-or-prefix"),
        (("serve", "--port", "1"), "unrecognized arguments"),
        (("reindex",), "--workspace"),
        (("reindex", "--workspace", "No Such"), "a workspace name"),
    ],
)
def test_wrong_usage_exits_2_before_reading_settings(
    capsys: pytest.CaptureFixture[str],
    clean_env: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    complaint: str,
) -> None:
    # No database URL is set: arguments are checked first.
    result = invoke(capsys, *argv)

    assert result.code == EXIT_USAGE
    assert result.out == ""
    assert complaint in result.err


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    result = invoke(capsys, "token", "create", "--help")

    assert result.code == EXIT_OK
    assert "--expires-in DURATION" in result.out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90d", timedelta(days=90)),
        ("12h", timedelta(hours=12)),
        ("30m", timedelta(minutes=30)),
        ("45s", timedelta(seconds=45)),
        ("1w2d", timedelta(days=9)),
        ("1D12H", timedelta(days=1, hours=12)),
        ("36525d", admin.MAX_TOKEN_LIFETIME),
    ],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert admin.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "d", "5", "-5d", "1.5h", "5y", "1d 2h", "0s", "36526d"])
def test_parse_duration_refuses(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        admin.parse_duration(text)


def test_parse_duration_refuses_what_does_not_fit_a_timedelta() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="at most"):
        admin.parse_duration("9999999999d")


# --- Configuration errors ------------------------------------------------------------------------


def test_missing_database_url_exits_2(
    capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch
) -> None:
    result = invoke(capsys, "token", "list")

    assert result.code == EXIT_USAGE
    assert result.out == ""
    assert result.err.startswith("memini: invalid configuration:\n")
    assert "MEMINI_DATABASE_URL: required" in result.err
    assert "Traceback" not in result.err


def test_configuration_error_prints_no_secret(
    capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("MEMINI_DATABASE_URL", f"postgresql://memini:{CANARY}@db.invalid/memini")
    clean_env.setenv("MEMINI_HTTP_PORT", CANARY)

    result = invoke(capsys, "--debug", "serve")

    assert result.code == EXIT_USAGE
    assert "MEMINI_HTTP_PORT: invalid value" in result.err
    assert CANARY not in result.out + result.err


def test_config_option_sets_the_config_file(
    capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv("MEMINI_DATABASE_URL", "postgresql://fake:fake@db.invalid/memini")
    config = tmp_path / "memini.toml"
    config.write_text('principal = "Not A Handle"\n', encoding="utf-8")

    result = invoke(capsys, "--config", str(config), "workspace", "list")

    assert result.code == EXIT_USAGE
    assert f"principal (config file {config}): invalid value" in result.err


@pytest.mark.parametrize("debug", [False, True])
def test_unparsable_database_url_prints_no_secret(
    capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch, debug: bool
) -> None:
    # libpq quotes the bad part of a URL in its message: here, the password.
    clean_env.setenv("MEMINI_DATABASE_URL", f"postgresql://memini:{CANARY}%zz@127.0.0.1/memini")

    result = invoke(capsys, *(["--debug"] if debug else []), "migrate")

    assert result.code == EXIT_USAGE
    assert "not a valid Postgres connection URL" in result.err
    assert CANARY not in result.out + result.err


def test_reindex_without_an_embedding_provider_exits_2_before_the_database(
    capsys: pytest.CaptureFixture[str], unreachable: str
) -> None:
    result = invoke(capsys, "reindex", "--workspace", "acme")

    assert result.code == EXIT_USAGE
    assert result.out == ""
    assert result.err == (
        "memini: no embedding provider is configured: set MEMINI_EMBEDDING_PROVIDER"
        " (openai_compatible or local) and the settings it needs\n"
    )


# --- Errors at run time --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("migrate",),
        ("serve",),
        ("serve", "--migrate"),
        ("workspace", "list"),
        ("workspace", "create", "alpha"),
        ("token", "create", "--scope", "read"),
        ("token", "list"),
        ("token", "revoke", "memini_abcdefgh"),
    ],
)
def test_unreachable_database_exits_3_naming_host_and_port_only(
    capsys: pytest.CaptureFixture[str], unreachable: str, argv: tuple[str, ...]
) -> None:
    result = invoke(capsys, *argv)

    assert result.code == EXIT_UNAVAILABLE
    assert result.out == ""
    assert result.err.startswith(f"memini: cannot use the database at {unreachable}: ")
    assert len(result.err.splitlines()) == 1
    assert CANARY not in result.err


def test_debug_adds_the_traceback_without_the_secret(
    capsys: pytest.CaptureFixture[str], unreachable: str
) -> None:
    result = invoke(capsys, "--debug", "token", "list")

    assert result.code == EXIT_UNAVAILABLE
    assert result.err.startswith(f"memini: cannot use the database at {unreachable}:")
    assert "Traceback (most recent call last):" in result.err
    assert "psycopg.OperationalError" in result.err
    assert CANARY not in result.err


@pytest.mark.parametrize(
    ("error", "code", "text"),
    [
        (
            MigrationError(f"0009_x.sql failed: {SECRET_URL}"),
            EXIT_FAILURE,
            "memini: migration failed: 0009_x.sql failed: ***\n",
        ),
        (
            psycopg.errors.UndefinedTable(SECRET_URL),
            EXIT_FAILURE,
            "memini: database error: UndefinedTable\n",
        ),
        (
            RuntimeError(f"broke near {SECRET_URL}"),
            EXIT_FAILURE,
            "memini: unexpected error: RuntimeError: broke near ***"
            " (run with --debug for the traceback)\n",
        ),
        (KeyboardInterrupt(), EXIT_INTERRUPTED, "memini: interrupted\n"),
        (
            EmbeddingsNotConfiguredError(),
            EXIT_USAGE,
            "memini: no embedding provider is configured: set MEMINI_EMBEDDING_PROVIDER"
            " (openai_compatible or local) and the settings it needs\n",
        ),
        (
            EmbeddingError(f"model synthetic-embed failed near {SECRET_URL}"),
            EXIT_FAILURE,
            "memini: embedding failed: model synthetic-embed failed near ***\n",
        ),
        (
            IndexMismatchError(
                STALE_STATE, IndexFingerprint(STALE_STATE.built_with.embedding, "2")
            ),
            EXIT_FAILURE,
            "memini: the index of workspace acme is out of date: it was built with model"
            " synthetic-embed (openai_compatible, dimension 8), chunker 1, but the service is"
            " configured for model synthetic-embed (openai_compatible, dimension 8), chunker 2"
            " (changed: chunker version). Search and indexing in this workspace are blocked until"
            " the index is rebuilt: run `memini reindex --workspace acme`\n",
        ),
    ],
)
def test_errors_map_to_exit_codes_and_one_redacted_line(
    capsys: pytest.CaptureFixture[str],
    clean_env: pytest.MonkeyPatch,
    error: BaseException,
    code: int,
    text: str,
) -> None:
    async def failing(args: argparse.Namespace, settings: Settings) -> int:
        raise error

    clean_env.setenv("MEMINI_DATABASE_URL", SECRET_URL)
    clean_env.setattr(admin, "migrate_command", failing)

    result = invoke(capsys, "migrate")

    assert result.code == code
    assert result.err == text


# --- Redaction -----------------------------------------------------------------------------------


def database_url(url: str) -> DatabaseUrl:
    return DatabaseUrl(Settings.model_validate({"database_url": url}))


def test_redact_removes_the_url_and_the_password_in_both_encodings() -> None:
    url = f"postgresql://memini:{CANARY}%2F1@127.0.0.1:55432/memini"
    database = database_url(url)

    text = database.redact(f"a {url} b {CANARY}/1 c {CANARY}%2F1 d")

    assert text == "a *** b *** c *** d"
    assert database.target == "127.0.0.1:55432"


def test_redact_keyword_form() -> None:
    database = database_url(f"host=db.invalid port=6000 password={CANARY} dbname=memini")

    assert database.redact(f"password {CANARY}") == "password ***"
    assert database.target == "db.invalid:6000"


def test_target_defaults() -> None:
    assert database_url("postgresql:///memini").target == "localhost:5432"


def test_unavailable_keeps_the_cause_of_a_libpq_message() -> None:
    database = database_url(f"postgresql://memini:{CANARY}@127.0.0.1:55432/memini")
    error = psycopg.OperationalError(
        'connection failed: connection to server at "127.0.0.1", port 55432 failed:'
        f' FATAL:  password authentication failed for user "memini" ({CANARY})\n'
        "Multiple connection attempts failed."
    )

    assert database.unavailable(error) == (
        "cannot use the database at 127.0.0.1:55432:"
        ' password authentication failed for user "memini" (***)'
    )


def test_traceback_redacts_messages_of_the_whole_chain_and_keeps_frames() -> None:
    url = f"postgresql://memini:{CANARY}@127.0.0.1:55432/memini"
    database = database_url(url)
    try:
        try:
            raise ConnectionError(f"cannot connect to {url}")
        except ConnectionError as inner:
            raise RuntimeError(f"wrapped, password {CANARY}") from inner
    except RuntimeError as error:
        text = database.format_traceback(error)

    assert CANARY not in text
    assert "ConnectionError: cannot connect to ***" in text
    assert "RuntimeError: wrapped, password ***" in text
    assert text.index("ConnectionError") < text.index("RuntimeError")
    assert __file__ in text


# --- Output --------------------------------------------------------------------------------------


def test_format_time() -> None:
    moment = datetime(2026, 11, 18, 16, 2, 59, tzinfo=UTC)

    assert format_time(moment) == "2026-11-18T16:02Z"
    assert format_time(moment.astimezone(timezone(timedelta(hours=2)))) == "2026-11-18T16:02Z"
    assert format_time(None) == "never"


def test_print_table_aligns_columns(capsys: pytest.CaptureFixture[str]) -> None:
    print_table(("NAME", "ID"), [("alpha", "1"), ("a", "22")])

    assert capsys.readouterr().out == "NAME   ID\nalpha  1\na      22\n"


# --- serve ---------------------------------------------------------------------------------------


def test_run_http_server_reports_a_failed_start(settings: Settings) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port: int = taken.getsockname()[1]
        busy = settings.model_copy(update={"http_port": port})

        started = asyncio.run(run_http_server(create_app(busy), busy))

    assert started is False
