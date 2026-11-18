"""Tests for memini.config. Every value here is synthetic."""

import logging
import re
import traceback
from pathlib import Path

import pytest
from pydantic import ValidationError

from memini.config import (
    CONFIG_FILE_VAR,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_URL,
    DEFAULT_MODEL_SETTINGS,
    HANDLE_REGEX,
    ConfigError,
    Settings,
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


def test_openai_compatible_alone_means_the_default_model_on_local_ollama() -> None:
    settings = load_settings(
        {"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_EMBEDDING_PROVIDER": "openai_compatible"}
    )

    assert settings.embedding_url == DEFAULT_EMBEDDING_URL == "http://127.0.0.1:11434/v1"
    assert settings.embedding_model == DEFAULT_EMBEDDING_MODEL == "qwen3-embedding:0.6b"
    assert settings.embedding_dimension == 1024
    assert settings.embedding_digest == DEFAULT_MODEL_SETTINGS["embedding_digest"]
    assert settings.embedding_query_prefix == DEFAULT_MODEL_SETTINGS["embedding_query_prefix"]
    assert settings.embedding_query_prefix.startswith("Instruct: ")
    assert settings.embedding_query_prefix.endswith("\nQuery:")
    assert settings.embedding_document_prefix == ""


def test_the_defaults_of_the_default_model_also_apply_when_it_is_named() -> None:
    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_EMBEDDING_PROVIDER": "openai_compatible",
            "MEMINI_EMBEDDING_MODEL": DEFAULT_EMBEDDING_MODEL,
            "MEMINI_EMBEDDING_URL": "http://host.docker.internal:11434/v1",
        }
    )

    assert settings.embedding_url == "http://host.docker.internal:11434/v1"
    assert settings.embedding_dimension == 1024
    assert settings.embedding_digest == DEFAULT_MODEL_SETTINGS["embedding_digest"]


def test_set_values_win_over_the_defaults_of_the_default_model(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", 'embedding_query_prefix = ""\n')

    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            CONFIG_FILE_VAR: str(config),
            "MEMINI_EMBEDDING_PROVIDER": "openai_compatible",
            "MEMINI_EMBEDDING_DIGEST": "synthetic-ollama-digest",
            "MEMINI_EMBEDDING_DIMENSION": "512",
        }
    )

    assert settings.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert settings.embedding_digest == "synthetic-ollama-digest"
    assert settings.embedding_dimension == 512
    # An empty prefix in the config file is a value; in the environment it would count as unset.
    assert settings.embedding_query_prefix == ""


def test_another_model_gets_the_default_url_but_nothing_of_the_default_model() -> None:
    settings = load_settings(
        {key: value for key, value in OPENAI_ENV.items() if key != "MEMINI_EMBEDDING_URL"}
    )

    assert settings.embedding_url == DEFAULT_EMBEDDING_URL
    assert settings.embedding_model == "synthetic-embed:v1"
    assert settings.embedding_dimension == 768
    assert settings.embedding_digest is None
    assert settings.embedding_query_prefix == ""


def test_the_local_provider_gets_no_default_model(tmp_path: Path) -> None:
    error = config_error(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_EMBEDDING_PROVIDER": "local",
            "MEMINI_EMBEDDING_DIMENSION": "1024",
            "MEMINI_EMBEDDING_DIGEST": LOCAL_DIGEST,
            "MEMINI_EMBEDDING_MODEL_DIR": str(tmp_path),
        }
    )

    assert error.problems == (
        "MEMINI_EMBEDDING_MODEL: required when embedding_provider is local;"
        " set MEMINI_EMBEDDING_MODEL or embedding_model in the config file",
    )


def test_env_memini_example_shows_what_the_provider_alone_implies() -> None:
    # .env.memini.example lists the defaults of the default model, commented out; they must stay
    # the ones the provider and the URL of the host's Ollama get. Compose reads `\n` in a
    # double-quoted value as a line break.
    text = (Path(__file__).parent.parent / ".env.memini.example").read_text(encoding="utf-8")
    listed = {
        key: value[1:-1].replace("\\n", "\n") if value.startswith('"') else value
        for key, value in re.findall(r"(?m)^# (MEMINI_EMBEDDING_[A-Z_]+)=(.*)$", text)
    }

    assert {"PROVIDER", "URL", "MODEL", "DIMENSION", "DIGEST", "QUERY_PREFIX"} <= {
        key.removeprefix("MEMINI_EMBEDDING_") for key in listed
    }
    documented = load_settings({"MEMINI_DATABASE_URL": FAKE_URL} | listed)
    implied = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_EMBEDDING_PROVIDER": "openai_compatible",
            # set with the provider, not the service's default: inside a container 127.0.0.1 is
            # the container
            "MEMINI_EMBEDDING_URL": "http://host.docker.internal:11434/v1",
        }
    )
    assert documented == implied


def test_the_default_query_prefix_fits_its_setting() -> None:
    prefix = DEFAULT_MODEL_SETTINGS["embedding_query_prefix"]

    assert isinstance(prefix, str)
    field = Settings.model_fields["embedding_query_prefix"]
    assert any(getattr(rule, "max_length", 0) >= len(prefix) for rule in field.metadata)


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
    ("provider", "given", "missing"),
    [
        # Only the default model has a default dimension.
        ("openai_compatible", {"MEMINI_EMBEDDING_MODEL": "synthetic-embed:v1"}, ["DIMENSION"]),
        ("local", {}, ["MODEL", "DIMENSION", "DIGEST", "MODEL_DIR"]),
    ],
)
def test_what_a_provider_needs_is_named(
    provider: str, given: dict[str, str], missing: list[str]
) -> None:
    error = config_error(
        {"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_EMBEDDING_PROVIDER": provider} | given
    )

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


# --- Git connector settings ----------------------------------------------------------------------

GIT_TOKEN = "fake-git-token-for-config-tests-41aa"  # noqa: S105 - fake, for leak checks


def test_git_settings_default_to_the_safe_choice() -> None:
    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL})

    assert settings.git_mirror_dir is None
    assert settings.git_fetch_timeout == 600
    assert settings.git_max_mirror_size == 5 * 1024**3
    assert settings.git_max_blob_size == 1024**2
    assert settings.git_allow_http is False
    assert settings.git_local_roots == ()
    assert settings.git_https_token is None
    assert settings.git_https_token_hosts == ()
    assert settings.git_ssh_key_path is None
    assert settings.git_ssh_known_hosts_path is None


def test_git_settings_from_the_environment(tmp_path: Path) -> None:
    token_file = write(tmp_path / "git_token", GIT_TOKEN + "\n")

    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_GIT_MIRROR_DIR": "/var/lib/memini/mirrors",
            "MEMINI_GIT_FETCH_TIMEOUT": "120",
            "MEMINI_GIT_MAX_MIRROR_SIZE": "2G",
            "MEMINI_GIT_MAX_BLOB_SIZE": "512 KiB",
            "MEMINI_GIT_ALLOW_HTTP": "true",
            "MEMINI_GIT_LOCAL_ROOTS": "/srv/repos, /home/someone/src,",
            "MEMINI_GIT_HTTPS_TOKEN_FILE": str(token_file),
            "MEMINI_GIT_HTTPS_TOKEN_HOSTS": "Git.Example.test,code.example.test",
            "MEMINI_GIT_SSH_KEY_PATH": "/run/secrets/git_ssh_key",
            "MEMINI_GIT_SSH_KNOWN_HOSTS_PATH": "/etc/memini/known_hosts",
        }
    )

    assert settings.git_mirror_dir == Path("/var/lib/memini/mirrors")
    assert settings.git_fetch_timeout == 120
    assert settings.git_max_mirror_size == 2 * 1024**3
    assert settings.git_max_blob_size == 512 * 1024
    assert settings.git_allow_http is True
    assert settings.git_local_roots == (Path("/srv/repos"), Path("/home/someone/src"))
    assert settings.git_https_token is not None
    assert settings.git_https_token.get_secret_value() == GIT_TOKEN
    assert settings.git_https_token_hosts == ("git.example.test", "code.example.test")
    assert settings.git_ssh_key_path == Path("/run/secrets/git_ssh_key")
    assert GIT_TOKEN not in repr(settings)


def test_git_lists_and_sizes_from_the_config_file(tmp_path: Path) -> None:
    config = write(
        tmp_path / "memini.toml",
        'git_local_roots = ["/srv/repos"]\ngit_max_mirror_size = 1048576\n'
        'git_max_blob_size = "2M"\n',
    )

    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    assert settings.git_local_roots == (Path("/srv/repos"),)
    assert settings.git_max_mirror_size == 1024**2
    assert settings.git_max_blob_size == 2 * 1024**2


def test_git_token_is_never_read_from_the_config_file(tmp_path: Path) -> None:
    config = write(tmp_path / "memini.toml", f'git_https_token = "{GIT_TOKEN}"\n')

    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    assert error.problems[0].startswith("git_https_token (config file")
    assert GIT_TOKEN not in str(error)


def test_git_token_needs_the_hosts_it_may_go_to() -> None:
    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_GIT_HTTPS_TOKEN": GIT_TOKEN})

    assert len(error.problems) == 1
    assert error.problems[0].startswith("MEMINI_GIT_HTTPS_TOKEN_HOSTS: invalid value (")
    assert GIT_TOKEN not in str(error)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MEMINI_GIT_MIRROR_DIR", "relative/mirrors"),
        ("MEMINI_GIT_FETCH_TIMEOUT", "0"),
        ("MEMINI_GIT_MAX_MIRROR_SIZE", "5 parsecs"),
        ("MEMINI_GIT_MAX_MIRROR_SIZE", "0"),
        ("MEMINI_GIT_MAX_BLOB_SIZE", "-1"),
        ("MEMINI_GIT_ALLOW_HTTP", "perhaps"),
        ("MEMINI_GIT_LOCAL_ROOTS", "/srv/repos,relative"),
        ("MEMINI_GIT_HTTPS_TOKEN", "fake token with spaces"),
        ("MEMINI_GIT_HTTPS_USERNAME", "fake login"),
        ("MEMINI_GIT_HTTPS_TOKEN_HOSTS", "-bad.example.test"),
        ("MEMINI_GIT_HTTPS_TOKEN_HOSTS", "host.example.test:443"),
        ("MEMINI_GIT_SSH_KEY_PATH", "id_ed25519"),
        ("MEMINI_GIT_SSH_KNOWN_HOSTS_PATH", "~/.ssh/known_hosts"),
    ],
)
def test_invalid_git_value_is_named_without_the_value(key: str, value: str) -> None:
    error = config_error({"MEMINI_DATABASE_URL": FAKE_URL, key: value})

    assert len(error.problems) == 1
    assert error.problems[0].startswith(f"{key}: invalid value (")
    if len(value) > 2:
        assert value not in str(error)
