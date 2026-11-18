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


# --- Embeddings ----------------------------------------------------------------------------------

FAKE_EMBEDDING_KEY = "sk-fake-embedding-key-canary-0042"
LOCAL_DIGEST = "sha256:" + "ab" * 32
OPENAI_ENV = {
    "MEMINI_DATABASE_URL": FAKE_URL,
    "MEMINI_EMBEDDING_PROVIDER": "openai_compatible",
    "MEMINI_EMBEDDING_MODEL": "synthetic-embed:v1",
    "MEMINI_EMBEDDING_DIMENSION": "768",
    "MEMINI_EMBEDDING_URL": "http://127.0.0.1:11434/v1",
}


def test_embeddings_are_off_by_default() -> None:
    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL})

    assert settings.embedding_provider is None
    assert settings.embedding_model is None
    assert settings.embedding_api_key is None
    assert settings.embedding_batch_size == 32
    assert settings.embedding_timeout == 60.0
    assert settings.embedding_model_file == "onnx/model.onnx"
    assert settings.embedding_pooling == "mean"
    assert settings.embedding_max_tokens == 512


def test_reads_an_openai_compatible_provider_from_the_environment(tmp_path: Path) -> None:
    key = write(tmp_path / "embedding_key", FAKE_EMBEDDING_KEY + "\n")

    settings = load_settings(
        OPENAI_ENV
        | {
            "MEMINI_EMBEDDING_API_KEY_FILE": str(key),
            "MEMINI_EMBEDDING_DIGEST": "synthetic-ollama-digest",
            "MEMINI_EMBEDDING_BATCH_SIZE": "8",
            "MEMINI_EMBEDDING_TIMEOUT": "2.5",
            "MEMINI_EMBEDDING_QUERY_PREFIX": "search_query: ",
            "MEMINI_EMBEDDING_DOCUMENT_PREFIX": "search_document: ",
        }
    )

    assert settings.embedding_provider == "openai_compatible"
    assert settings.embedding_dimension == 768
    assert settings.embedding_api_key is not None
    assert settings.embedding_api_key.get_secret_value() == FAKE_EMBEDDING_KEY
    assert settings.embedding_batch_size == 8
    assert settings.embedding_timeout == 2.5
    assert settings.embedding_query_prefix == "search_query: "
    assert FAKE_EMBEDDING_KEY not in repr(settings)


def test_reads_a_local_provider_from_the_config_file(tmp_path: Path) -> None:
    config = write(
        tmp_path / "memini.toml",
        'embedding_provider = "local"\n'
        'embedding_model = "synthetic-org/synthetic-model"\n'
        "embedding_dimension = 384\n"
        f'embedding_digest = "{LOCAL_DIGEST}"\n'
        f'embedding_model_dir = "{tmp_path / "models"}"\n'
        'embedding_model_file = "onnx/model_quantized.onnx"\n'
        f'embedding_revision = "{"0" * 40}"\n'
        'embedding_pooling = "cls"\n'
        "embedding_max_tokens = 256\n"
        'embedding_document_prefix = "passage: "\n',
    )

    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    assert settings.embedding_provider == "local"
    assert settings.embedding_model_dir == tmp_path / "models"
    assert settings.embedding_model_file == "onnx/model_quantized.onnx"
    assert settings.embedding_pooling == "cls"
    assert settings.embedding_max_tokens == 256


@pytest.mark.parametrize(
    ("provider", "missing"),
    [
        ("openai_compatible", ["MODEL", "DIMENSION", "URL"]),
        ("local", ["MODEL", "DIMENSION", "DIGEST", "MODEL_DIR"]),
    ],
)
def test_what_a_provider_needs_is_named(provider: str, missing: list[str]) -> None:
    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_EMBEDDING_PROVIDER": provider})

    assert error.problems == tuple(
        f"MEMINI_EMBEDDING_{name}: required when embedding_provider is {provider};"
        f" set MEMINI_EMBEDDING_{name} or embedding_{name.lower()} in the config file"
        for name in missing
    )


def test_an_embedding_setting_without_a_provider_is_refused(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", "embedding_dimension = 384\n")

    error = config_error(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            CONFIG_FILE_VAR: str(config),
            "MEMINI_EMBEDDING_API_KEY": FAKE_EMBEDDING_KEY,
        }
    )

    assert error.problems == (
        f"embedding_dimension (config file {config}): has no effect without embedding_provider;"
        " set MEMINI_EMBEDDING_PROVIDER, or remove it",
        "MEMINI_EMBEDDING_API_KEY: has no effect without embedding_provider;"
        " set MEMINI_EMBEDDING_PROVIDER, or remove it",
    )
    assert FAKE_EMBEDDING_KEY not in str(error)


def test_a_setting_of_the_other_provider_is_refused() -> None:
    error = config_error(
        OPENAI_ENV | {"MEMINI_EMBEDDING_POOLING": "cls", "MEMINI_EMBEDDING_REVISION": "0" * 40}
    )

    assert error.problems == (
        "MEMINI_EMBEDDING_REVISION: applies only when embedding_provider is local",
        "MEMINI_EMBEDDING_POOLING: applies only when embedding_provider is local",
    )


@pytest.mark.parametrize(
    ("key", "value", "problem"),
    [
        ("MEMINI_EMBEDDING_PROVIDER", "cloud", "invalid value (literal_error)"),
        ("MEMINI_EMBEDDING_DIMENSION", "0", "invalid value (greater_than_equal)"),
        ("MEMINI_EMBEDDING_DIMENSION", "16001", "invalid value (less_than_equal)"),
        ("MEMINI_EMBEDDING_BATCH_SIZE", "0", "invalid value (greater_than_equal)"),
        ("MEMINI_EMBEDDING_TIMEOUT", "0", "invalid value (greater_than)"),
        ("MEMINI_EMBEDDING_URL", "127.0.0.1:11434", "invalid value (string_pattern_mismatch)"),
        ("MEMINI_EMBEDDING_URL", "http://host/v1?key=1", "invalid value (string_pattern_mismatch)"),
    ],
)
def test_an_invalid_embedding_value_is_named(key: str, value: str, problem: str) -> None:
    error = config_error(OPENAI_ENV | {key: value})

    assert len(error.problems) == 1
    assert error.problems[0].startswith(f"{key}: {problem}")


@pytest.mark.parametrize(
    ("key", "value", "problem"),
    [
        ("MEMINI_EMBEDDING_MODEL", "no-organization", "expected a Hugging Face repository"),
        ("MEMINI_EMBEDDING_DIGEST", "sha256:short", "expected sha256: and 64 lowercase hex"),
        ("MEMINI_EMBEDDING_REVISION", "main", "invalid value (string_pattern_mismatch)"),
        ("MEMINI_EMBEDDING_MODEL_FILE", "../model.onnx", "invalid value (string_pattern_mismatch)"),
        (
            "MEMINI_EMBEDDING_MODEL_FILE",
            "/abs/model.onnx",
            "invalid value (string_pattern_mismatch)",
        ),
        ("MEMINI_EMBEDDING_POOLING", "max", "invalid value (literal_error)"),
    ],
)
def test_an_invalid_local_value_is_named(key: str, value: str, problem: str) -> None:
    local = {
        "MEMINI_DATABASE_URL": FAKE_URL,
        "MEMINI_EMBEDDING_PROVIDER": "local",
        "MEMINI_EMBEDDING_MODEL": "synthetic-org/synthetic-model",
        "MEMINI_EMBEDDING_DIMENSION": "384",
        "MEMINI_EMBEDDING_DIGEST": LOCAL_DIGEST,
        "MEMINI_EMBEDDING_MODEL_DIR": "/var/lib/memini/models",
    }

    error = config_error(local | {key: value})

    assert len(error.problems) == 1
    assert error.problems[0].startswith(f"{key}: ")
    assert problem in error.problems[0]


def test_credentials_in_the_embedding_url_are_refused_without_echoing_them() -> None:
    error = config_error(
        OPENAI_ENV | {"MEMINI_EMBEDDING_URL": f"https://user:{CANARY}@embeddings.example.test/v1"}
    )

    assert error.problems == (
        "MEMINI_EMBEDDING_URL: holds credentials, but the URL is not a secret;"
        " set MEMINI_EMBEDDING_API_KEY or MEMINI_EMBEDDING_API_KEY_FILE instead",
    )
    assert CANARY not in str(error)


def test_the_embedding_key_is_a_secret(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", f'embedding_api_key = "{CANARY}"\n')

    error = config_error(OPENAI_ENV | {CONFIG_FILE_VAR: str(config)})

    assert error.problems == (
        f"embedding_api_key (config file {config}): a secret is not read from the config file;"
        " set MEMINI_EMBEDDING_API_KEY or MEMINI_EMBEDDING_API_KEY_FILE",
    )
    assert CANARY not in str(error)
    assert {"MEMINI_EMBEDDING_API_KEY", "MEMINI_EMBEDDING_API_KEY_FILE"} <= set(
        environment_variables()
    )
