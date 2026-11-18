"""The configuration reference: current, complete, and true to the code it describes.

Every value here is synthetic.
"""

import re
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from memini.cli.client import CLIENT_VARIABLES
from memini.config import CONFIG_FILE_VAR, Settings, load_settings
from memini.config_reference import (
    CHANGE_NOTES,
    COMPOSE_SERVICE_SETTINGS,
    COMPOSE_VARIABLES,
    IMAGE_SETTINGS,
    NOTES,
    REFERENCE_PATH,
    REINDEX_SETTINGS,
    TOML_EXAMPLE,
    computed_defaults,
    env_var,
    is_secret,
    main,
    reference_text,
)
from memini.embeddings import embedding_spec
from memini.index import current_fingerprint

ROOT = Path(__file__).resolve().parent.parent
REGENERATE = "run `make config-docs` and commit docs/configuration.md"
FAKE_URL = SecretStr("postgresql://fake:fake@db.invalid:5432/memini")

# One settings object per embedding provider, with every setting of the fingerprint given.
OPENAI = Settings(
    database_url=FAKE_URL,
    embedding_provider="openai_compatible",
    embedding_model="synthetic-org/synthetic-model",
    embedding_dimension=384,
    embedding_digest="sha256:" + "ab" * 32,
    embedding_document_prefix="passage: ",
)
LOCAL = Settings(
    database_url=FAKE_URL,
    embedding_provider="local",
    embedding_model="synthetic-org/synthetic-model",
    embedding_dimension=384,
    embedding_digest="sha256:" + "ab" * 32,
    embedding_model_dir=Path("/synthetic/models"),
    embedding_document_prefix="passage: ",
)


def committed() -> str:
    return (ROOT / REFERENCE_PATH).read_text(encoding="utf-8")


# --- The committed file --------------------------------------------------------------------------


def test_the_committed_reference_is_current() -> None:
    assert committed() == reference_text(), f"docs/configuration.md is out of date; {REGENERATE}"


@pytest.mark.parametrize("name", list(Settings.model_fields))
def test_every_setting_is_in_the_reference(name: str) -> None:
    text = committed()
    assert f"`{name}`" in text, f"setting {name} is not in docs/configuration.md; {REGENERATE}"
    assert f"`{env_var(name)}`" in text, f"{env_var(name)} is not documented; {REGENERATE}"
    if is_secret(name):
        assert f"`{env_var(name)}_FILE`" in text, f"{env_var(name)}_FILE is not documented"


def test_the_client_and_compose_variables_are_in_the_reference() -> None:
    text = committed()
    for name in [CONFIG_FILE_VAR, *CLIENT_VARIABLES, *COMPOSE_VARIABLES]:
        assert f"`{name}`" in text, f"{name} is not in docs/configuration.md; {REGENERATE}"


def test_main_writes_the_reference(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "configuration.md"

    assert main([str(target)]) == 0
    assert target.read_text(encoding="utf-8") == reference_text()
    assert main([]) == 0
    assert capsys.readouterr().out == reference_text()
    assert main(["one", "two"]) == 2


# --- What a change needs -------------------------------------------------------------------------


class _Recorder:
    """Stands in for settings and records which of them are read."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.read: set[str] = set()

    def __getattr__(self, name: str) -> object:
        self.read.add(name)
        return getattr(self._settings, name)


def test_the_reindex_settings_are_what_the_fingerprint_reads() -> None:
    """The index fingerprint is computed from embedding_spec(settings) and the chunker version;
    a setting embedding_spec does not read cannot change it."""
    read: set[str] = set()
    for settings in (OPENAI, LOCAL):
        recorder = _Recorder(settings)
        embedding_spec(cast(Settings, recorder))
        read |= recorder.read

    assert read == REINDEX_SETTINGS, (
        "memini.config_reference.REINDEX_SETTINGS must name exactly the settings in the index"
        f" fingerprint; add or remove {sorted(read ^ REINDEX_SETTINGS)}, then {REGENERATE}"
    )


@pytest.mark.parametrize(
    ("name", "base", "changed"),
    [
        pytest.param(name, base, changed, id=name)
        for name, base, changed in [
            ("embedding_provider", OPENAI, LOCAL),
            ("embedding_model", OPENAI, {"embedding_model": "synthetic-org/other-model"}),
            ("embedding_dimension", OPENAI, {"embedding_dimension": 768}),
            ("embedding_digest", OPENAI, {"embedding_digest": "sha256:" + "cd" * 32}),
            ("embedding_document_prefix", OPENAI, {"embedding_document_prefix": "document: "}),
            ("embedding_pooling", LOCAL, {"embedding_pooling": "cls"}),
            ("embedding_max_tokens", LOCAL, {"embedding_max_tokens": 256}),
        ]
    ],
)
def test_each_reindex_setting_changes_the_fingerprint(
    name: str, base: Settings, changed: Settings | dict[str, object]
) -> None:
    other = changed if isinstance(changed, Settings) else base.model_copy(update=changed)

    assert name in REINDEX_SETTINGS
    assert (
        current_fingerprint(embedding_spec(base)).digest
        != current_fingerprint(embedding_spec(other)).digest
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("embedding_query_prefix", "query: "),
        ("embedding_batch_size", 8),
        ("embedding_timeout", 5.0),
        ("embedding_url", "http://synthetic.invalid:8080/v1"),
        ("embedding_api_key", SecretStr("sk-synthetic-not-real")),
    ],
)
def test_other_embedding_settings_leave_the_fingerprint(name: str, value: object) -> None:
    changed = OPENAI.model_copy(update={name: value})

    assert name not in REINDEX_SETTINGS
    assert (
        current_fingerprint(embedding_spec(OPENAI)).digest
        == current_fingerprint(embedding_spec(changed)).digest
    )


def test_the_tables_name_settings_that_exist() -> None:
    fields = set(Settings.model_fields)
    for table in (REINDEX_SETTINGS, CHANGE_NOTES, NOTES, IMAGE_SETTINGS, COMPOSE_SERVICE_SETTINGS):
        assert set(table) <= fields, f"no such setting: {sorted(set(table) - fields)}"


# --- Defaults and examples -----------------------------------------------------------------------


def test_the_computed_defaults_are_what_the_model_fills_in() -> None:
    settings = Settings(database_url=FAKE_URL, embedding_provider="openai_compatible")
    filled = {
        name: getattr(settings, name)
        for name, field in Settings.model_fields.items()
        if not field.is_required()
        and name != "embedding_provider"
        and getattr(settings, name) != field.default
    }

    assert filled == {name: value for name, (_, value) in computed_defaults().items()}


def test_the_model_defaults_apply_to_the_default_model_only() -> None:
    other = Settings(
        database_url=FAKE_URL,
        embedding_provider="openai_compatible",
        embedding_model="synthetic-embed:v1",
    )
    for name, (model_only, _) in computed_defaults().items():
        if model_only:
            assert getattr(other, name) == Settings.model_fields[name].default


def test_the_toml_example_is_a_valid_config_file(tmp_path: Path) -> None:
    path = tmp_path / "memini.toml"
    path.write_text(TOML_EXAMPLE, encoding="utf-8")

    settings = load_settings(
        {"MEMINI_DATABASE_URL": FAKE_URL.get_secret_value(), CONFIG_FILE_VAR: str(path)}
    )

    assert settings.log_level == "DEBUG"
    assert settings.git_max_blob_size == 2 * 1024**2


# --- Compose, the image and .env.example ---------------------------------------------------------


def test_the_compose_variables_are_what_compose_yaml_reads() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    interpolated = set(re.findall(r"\$\{(MEMINI_[A-Z0-9_]+)", compose))
    passed = {env_var(name) for name in COMPOSE_SERVICE_SETTINGS}

    assert interpolated == set(COMPOSE_VARIABLES) | passed, (
        "compose.yaml reads other variables than the reference lists: update COMPOSE_VARIABLES or"
        f" COMPOSE_SERVICE_SETTINGS in src/memini/config_reference.py, then {REGENERATE}"
    )
    for name, (default, _) in COMPOSE_VARIABLES.items():
        assert f"${{{name}:-{default}}}" in compose, f"the default of {name} in compose.yaml"


def test_the_service_settings_compose_passes_are_listed() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    environment = set(re.findall(r"(?m)^\s+(MEMINI_[A-Z0-9_]+):", compose))

    assert environment == {env_var(n) for n in COMPOSE_SERVICE_SETTINGS} | {
        env_var("database_url")
    }, (
        "compose.yaml passes other settings to the service than the reference lists: update"
        f" COMPOSE_SERVICE_SETTINGS in src/memini/config_reference.py, then {REGENERATE}"
    )


def test_the_image_settings_are_what_the_dockerfile_sets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    code = "\n".join(line for line in dockerfile.splitlines() if not line.lstrip().startswith("#"))
    assigned = dict(re.findall(r"\b(MEMINI_[A-Z0-9_]+)=(\S+)", code))

    assert assigned == {env_var(name): value for name, value in IMAGE_SETTINGS.items()}, (
        "the Dockerfile sets other settings than the reference lists: update IMAGE_SETTINGS in"
        f" src/memini/config_reference.py, then {REGENERATE}"
    )


def test_every_variable_of_env_example_is_documented() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = committed()

    for name in set(re.findall(r"(?m)^#?\s*(MEMINI_[A-Z0-9_]+)=", example)):
        assert f"`{name}`" in text, f"{name} from .env.example is not in docs/configuration.md"
