"""Tests for the ``memini`` command that need no database: arguments, the operator commands' errors
and redaction. The client commands are in test_cli_client.py. Every value here is synthetic."""

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
from memini.cli import admin, arguments, main
from memini.cli.client import CLIENT_VARIABLES
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
from memini.token_admin import MAX_TOKEN_LIFETIME

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
    for name in (*environment_variables(), *CLIENT_VARIABLES, "MEMINI_PRINCIPAL"):
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
        (("token", "create", "--scope", "read", "--expires-in", "36526d"), "at most 36525d"),
        (("source",), "<command>"),
        (("source", "add", "acme", "shop", "--kind", "svn"), "invalid choice: 'svn'"),
        (("source", "add", "acme", "Shop", "--location", "/srv/shop", "--ref", "main"), "source"),
        (
            (
                "source",
                "add",
                "acme",
                "shop",
                "--location",
                "x",
                "--ref",
                "main",
                "--schedule",
                "30s",
            ),
            "a schedule is from 1m to 366d",
        ),
        (("source", "edit", "acme", "shop", "--schedule", "1h", "--no-schedule"), "not allowed"),
        (("source", "edit", "acme", "shop", "--include", "a", "--no-include"), "not allowed"),
        (("source", "remove", "acme"), "source"),
        (("streams", "acme"), "source"),
        (("sync", "acme"), "source"),
        (("sync", "acme", "shop", "--wait-timeout", "soon"), "a duration is"),
        (("runs",), "workspace"),
        (("runs", "acme", "--limit", "0"), "a number from 1 to 200"),
        (("run", "not-a-run-id"), "invalid UUID value"),
        (("search",), "query"),
        (("search", "lamp", "--kind", "letter"), "invalid choice: 'letter'"),
        (("search", "lamp", "--limit", "1000"), "a number from 1 to 100"),
        (("serve", "--port", "1"), "unrecognized arguments"),
        (("bootstrap", "--scope", "read"), "unrecognized arguments"),
        (("bootstrap", "--expires-in", "0h"), "longer than zero"),
        (("reindex",), "--workspace"),
        (("reindex", "--workspace", "No Such"), "a workspace name"),
        (("replay", "--workspace", "acme", "--source", "hearth"), "--recipe"),
        (("replay", "--workspace", "acme", "--source", "x", "--recipe", "r", "--run", "1"), "UUID"),
        (("--token-f", "/dev/null", "workspace", "list"), "invalid choice"),
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


def test_help_lists_client_and_operator_commands_apart(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = invoke(capsys, "--help")

    assert result.code == EXIT_OK
    client, operator = result.out.split("\nclient commands", 1)[1].split("\noperator commands")
    listed = {line.split()[0] for line in client.splitlines() if line.startswith("  ")}
    assert listed == {
        "workspace",
        "source",
        "streams",
        "sync",
        "runs",
        "run",
        "status",
        "token",
        "search",
        "meetings",
        "messages",
        "decision",
        "extract",
        "forget",
        "person",
    }
    operator_part = operator.split("\n\n")[0]
    listed = {line.split()[0] for line in operator_part.splitlines() if line.startswith("  ")}
    assert listed == {
        "workspace",
        "serve",
        "migrate",
        "bootstrap",
        "reindex",
        "replay",
        "eval",
        "credentials",
    }
    assert "  workspace llm " in operator_part
    assert "docker compose exec memini memini" in " ".join(operator.split())
    assert "4 eval: a metric fell below the baseline; 5 the service refused the token" in (
        " ".join(result.out.split())
    )


def test_an_operator_command_says_so_in_its_help(capsys: pytest.CaptureFixture[str]) -> None:
    result = invoke(capsys, "bootstrap", "--help")

    assert result.code == EXIT_OK
    assert result.out.startswith("usage: memini bootstrap ")
    assert "An operator command" in result.out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90d", timedelta(days=90)),
        ("12h", timedelta(hours=12)),
        ("30m", timedelta(minutes=30)),
        ("45s", timedelta(seconds=45)),
        ("1w2d", timedelta(days=9)),
        ("1D12H", timedelta(days=1, hours=12)),
        ("36525d", MAX_TOKEN_LIFETIME),
    ],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert arguments.parse_duration(text) == expected
    assert arguments.parse_lifetime(text) == expected


@pytest.mark.parametrize("text", ["", "d", "5", "-5d", "1.5h", "5y", "1d 2h", "0s", "36526d"])
def test_parse_duration_refuses(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        arguments.parse_duration(text)


def test_parse_duration_refuses_what_does_not_fit_a_timedelta() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="at most"):
        arguments.parse_lifetime("9999999999d")


@pytest.mark.parametrize(
    ("text", "expected"), [("1m", timedelta(minutes=1)), ("366d", timedelta(days=366))]
)
def test_parse_schedule(text: str, expected: timedelta) -> None:
    assert arguments.parse_schedule(text) == expected


@pytest.mark.parametrize("text", ["59s", "367d"])
def test_parse_schedule_refuses(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="from 1m to 366d"):
        arguments.parse_schedule(text)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (45, "45s"), (60, "1m"), (3600, "1h"), (5400, "1h30m"), (694861, "1w1d1h1m1s")],
)
def test_format_duration_reads_back(seconds: int, text: str) -> None:
    assert arguments.format_duration(seconds) == text
    if seconds:
        assert arguments.parse_duration(text) == timedelta(seconds=seconds)


# --- Configuration errors ------------------------------------------------------------------------


def test_missing_database_url_exits_2(
    capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch
) -> None:
    result = invoke(capsys, "bootstrap")

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

    result = invoke(capsys, "--config", str(config), "bootstrap")

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
        ("bootstrap",),
        ("bootstrap", "--force", "--name", "ops"),
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
    result = invoke(capsys, "--debug", "bootstrap")

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


@pytest.mark.parametrize(
    "argv", [("--url", "http://127.0.0.1:1", "migrate"), ("--token-file", "/dev/null", "serve")]
)
def test_client_options_are_refused_for_an_operator_command(
    capsys: pytest.CaptureFixture[str], unreachable: str, argv: tuple[str, ...]
) -> None:
    result = invoke(capsys, *argv)

    assert result.code == EXIT_USAGE
    assert result.err == (
        "memini: --url and --token-file are for client commands; an operator command works on"
        " the database with the service's settings\n"
    )


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


# --- Offline commands ----------------------------------------------------------------------------


def test_recipe_validate_runs_without_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    for name in ("MEMINI_DATABASE_URL", "MEMINI_DATABASE_URL_FILE", "MEMINI_URL", "MEMINI_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    valid = Path(__file__).parent / "fixtures" / "recipes" / "hearth.toml"
    broken = tmp_path / "broken.toml"
    broken.write_text('format = 1\nname = "x"\n', encoding="utf-8")

    assert main(["recipe", "validate", str(valid)]) == 0
    assert main(["recipe", "validate", str(broken)]) == 1
    assert "server: required" in capsys.readouterr().out


def test_offline_commands_refuse_the_global_service_options() -> None:
    valid = Path(__file__).parent / "fixtures" / "recipes" / "hearth.toml"
    assert main(["--url", "http://127.0.0.1:1", "recipe", "validate", str(valid)]) == 2
