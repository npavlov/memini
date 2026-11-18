"""Tests for memini.config. Every value here is synthetic."""

import logging
import traceback
from pathlib import Path

import pytest
from pydantic import ValidationError

from memini.config import (
    CONFIG_FILE_VAR,
    HANDLE_REGEX,
    ConfigError,
    environment_variables,
    load_settings,
)
from memini.workspaces import HANDLE_PATTERN

FAKE_URL = "postgresql://fake:fake@db.invalid:5432/memini"
# A distinctive value that must never leave the settings object.
CANARY = "canary-not-a-real-secret-7f3a9c"


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def config_error(env: dict[str, str]) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        load_settings(env)
    return info.value


def test_defaults_apply_when_only_the_required_key_is_set() -> None:
    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL})

    assert settings.database_url.get_secret_value() == FAKE_URL
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 6364
    assert settings.log_level == "INFO"
    assert settings.principal == "owner"


def test_reads_the_environment() -> None:
    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_HTTP_HOST": "0.0.0.0",  # noqa: S104 - a value under test, nothing binds to it
            "MEMINI_HTTP_PORT": "7001",
            "MEMINI_LOG_LEVEL": "debug",
            "MEMINI_PRINCIPAL": "someone-else",
            "UNRELATED": "ignored",
            "MEMINI_SOMETHING_ELSE": "ignored",
        }
    )

    assert settings.http_host == "0.0.0.0"  # noqa: S104
    assert settings.http_port == 7001
    assert settings.log_level == "DEBUG"
    assert settings.principal == "someone-else"


def test_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in environment_variables():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMINI_DATABASE_URL", FAKE_URL)
    monkeypatch.setenv("MEMINI_HTTP_PORT", "7002")

    assert load_settings().http_port == 7002


def test_empty_environment_value_counts_as_not_set() -> None:
    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_HTTP_PORT": ""})

    assert settings.http_port == 6364


def test_reads_the_config_file(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", 'http_host = "::1"\nhttp_port = 7003\n')

    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    assert settings.http_host == "::1"
    assert settings.http_port == 7003
    assert settings.log_level == "INFO"


def test_environment_overrides_the_config_file(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", 'http_port = 7004\nlog_level = "WARNING"\n')

    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            CONFIG_FILE_VAR: str(config),
            "MEMINI_HTTP_PORT": "7005",
        }
    )

    assert settings.http_port == 7005
    assert settings.log_level == "WARNING"


def test_reads_a_secret_from_a_file(tmp_path: Path) -> None:
    secret = write(tmp_path / "database_url", FAKE_URL + "\n")

    settings = load_settings({"MEMINI_DATABASE_URL_FILE": str(secret)})

    assert settings.database_url.get_secret_value() == FAKE_URL


def test_settings_are_frozen() -> None:
    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL})

    with pytest.raises(ValidationError):
        settings.http_port = 1


def test_missing_required_key_is_named() -> None:
    error = config_error({})

    assert error.problems == (
        "MEMINI_DATABASE_URL: required, not set; "
        "set MEMINI_DATABASE_URL or MEMINI_DATABASE_URL_FILE",
    )
    assert "MEMINI_DATABASE_URL" in str(error)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MEMINI_HTTP_PORT", "not-a-port"),
        ("MEMINI_HTTP_PORT", "0"),
        ("MEMINI_HTTP_PORT", "65536"),
        ("MEMINI_LOG_LEVEL", "LOUD"),
        ("MEMINI_PRINCIPAL", "Owner"),
        ("MEMINI_PRINCIPAL", "-owner"),
        ("MEMINI_PRINCIPAL", "owner\n"),
        ("MEMINI_PRINCIPAL", "o" * 64),
    ],
)
def test_invalid_value_is_named(key: str, value: str) -> None:
    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, key: value})

    assert len(error.problems) == 1
    assert error.problems[0].startswith(f"{key}: invalid value (")


def test_every_problem_is_reported_at_once() -> None:
    error = config_error({"MEMINI_HTTP_PORT": "x", "MEMINI_LOG_LEVEL": "LOUD"})

    assert [problem.split(":")[0] for problem in error.problems] == [
        "MEMINI_DATABASE_URL",
        "MEMINI_HTTP_PORT",
        "MEMINI_LOG_LEVEL",
    ]


def test_invalid_value_in_the_config_file_names_key_and_file(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", 'http_port = "x"\n')

    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    assert error.problems[0].startswith(f"http_port (config file {config}): invalid value (")


def test_unknown_key_in_the_config_file_is_named(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", "http_prot = 7006\n")

    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    assert error.problems == (f"http_prot (config file {config}): unknown setting",)


def test_missing_config_file_is_named(tmp_path: Path) -> None:
    absent = tmp_path / "absent.toml"

    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(absent)})

    assert error.problems == (
        f"{CONFIG_FILE_VAR}: cannot read {absent}: No such file or directory",
    )


def test_malformed_config_file_reports_position_only(tmp_path: Path) -> None:
    unterminated = f'log_level = "{CANARY}'
    config = write(tmp_path / "memini.toml", f"http_port = 7007\n{unterminated}\n")

    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    # tomllib stops at the line break that ends the unterminated string.
    column = len(unterminated) + 1
    assert error.problems == (
        f"{CONFIG_FILE_VAR}: {config} is not valid TOML (at line 2, column {column})",
    )


def test_missing_secret_file_is_named_once(tmp_path: Path) -> None:
    absent = tmp_path / "absent"

    error = config_error({"MEMINI_DATABASE_URL_FILE": str(absent)})

    assert error.problems == (
        f"MEMINI_DATABASE_URL_FILE: cannot read {absent}: No such file or directory",
    )


def test_empty_secret_file_is_invalid(tmp_path: Path) -> None:
    secret = write(tmp_path / "database_url", "\n")

    error = config_error({"MEMINI_DATABASE_URL_FILE": str(secret)})

    assert error.problems[0].startswith("MEMINI_DATABASE_URL_FILE: invalid value (")


def test_secret_set_twice_is_refused(tmp_path: Path) -> None:
    secret = write(tmp_path / "database_url", CANARY)

    error = config_error({"MEMINI_DATABASE_URL": CANARY, "MEMINI_DATABASE_URL_FILE": str(secret)})

    assert error.problems == (
        "MEMINI_DATABASE_URL, MEMINI_DATABASE_URL_FILE: both are set; set only one",
    )


def test_secret_in_the_config_file_is_refused(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", f'database_url = "{CANARY}"\n')

    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    assert error.problems == (
        f"database_url (config file {config}): a secret is not read from the config file; "
        "set MEMINI_DATABASE_URL or MEMINI_DATABASE_URL_FILE",
    )
    assert CANARY not in str(error)


def test_secret_never_reaches_error_text_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Acceptance check: with a secret loaded, an invalid start leaks it nowhere.

    The secret sits in the database URL, and is also pasted by mistake into two settings that are
    not secret, which is how a value usually reaches an error message (pydantic's input_value).
    The error is then logged with its traceback, as the CLI would on a failed start.
    """
    caplog.set_level(logging.DEBUG)
    env = {
        "MEMINI_DATABASE_URL": f"postgresql://memini:{CANARY}@db.invalid:5432/memini",
        "MEMINI_HTTP_PORT": CANARY,
        "MEMINI_LOG_LEVEL": CANARY,
    }

    error = config_error(env)
    logging.getLogger("memini").error("cannot start: %s", error, exc_info=error)

    assert [problem.split(":")[0] for problem in error.problems] == [
        "MEMINI_HTTP_PORT",
        "MEMINI_LOG_LEVEL",
    ]
    assert error.__cause__ is None
    assert error.__context__ is None
    assert CANARY not in str(error)
    assert CANARY not in repr(error)
    assert CANARY not in "".join(traceback.format_exception(error))
    assert "cannot start" in caplog.text
    assert CANARY not in caplog.text


def test_secret_is_hidden_in_repr_and_dumps(tmp_path: Path) -> None:
    secret = write(tmp_path / "database_url", f"postgresql://memini:{CANARY}@db.invalid/memini")

    settings = load_settings({"MEMINI_DATABASE_URL_FILE": str(secret)})

    assert CANARY in settings.database_url.get_secret_value()
    assert CANARY not in repr(settings)
    assert CANARY not in str(settings)
    assert CANARY not in settings.model_dump_json()
    assert CANARY not in str(settings.model_dump())


def test_environment_variable_names_do_not_collide() -> None:
    names = environment_variables()

    assert len(names) == len(set(names))
    assert {"MEMINI_CONFIG_FILE", "MEMINI_DATABASE_URL", "MEMINI_DATABASE_URL_FILE"} <= set(names)


def test_principal_names_follow_the_handle_pattern_of_the_database() -> None:
    assert f"^{HANDLE_PATTERN.pattern}$" == HANDLE_REGEX
