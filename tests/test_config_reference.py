"""The configuration reference: current, complete, and true to the code it describes.

Every value here is synthetic.
"""

import re
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from memini.cli.client import CLIENT_VARIABLES
from memini.config import (
    _EMBEDDING_ONLY,  # pyright: ignore[reportPrivateUsage] - settings the image cannot use
    CONFIG_FILE_VAR,
    Settings,
    environment_variables,
    load_settings,
)
from memini.config_reference import (
    CHANGE_NOTES,
    COMPOSE_EMBEDDING_URL,
    COMPOSE_ENV_FILE,
    COMPOSE_FILE_SETTINGS,
    COMPOSE_SECRETS_DIR,
    COMPOSE_SET_SETTINGS,
    COMPOSE_UNSET_SECRET,
    COMPOSE_VARIABLES,
    IMAGE_SETTINGS,
    NOTES,
    REFERENCE_PATH,
    REINDEX_SETTINGS,
    TOML_EXAMPLE,
    compose_file_variable,
    compose_secret,
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
    files = [compose_file_variable(name) for name in COMPOSE_FILE_SETTINGS]
    for name in [CONFIG_FILE_VAR, *CLIENT_VARIABLES, *COMPOSE_VARIABLES, *files]:
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
    for table in (
        REINDEX_SETTINGS,
        CHANGE_NOTES,
        NOTES,
        IMAGE_SETTINGS,
        COMPOSE_SET_SETTINGS,
        COMPOSE_FILE_SETTINGS,
    ):
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


# --- Compose, the image, .env.example and .env.memini.example ----------------------------------


def compose_text() -> str:
    return (ROOT / "compose.yaml").read_text(encoding="utf-8")


def compose_block(*keys: str) -> list[str]:
    """The lines of the block of compose.yaml under ``keys``, e.g. ``("services", "memini",
    "environment")``, without comments, blank lines and the block's indentation. Enough of YAML for
    the plain layout of compose.yaml."""
    lines = compose_text().splitlines()
    start = 0
    for depth, key in enumerate(keys):
        header = "  " * depth + key + ":"
        start = next(
            (i + 1 for i in range(start, len(lines)) if lines[i] == header),
            len(lines),
        )
        assert start < len(lines), f"compose.yaml has no block {'.'.join(keys[: depth + 1])}"
    indent = "  " * len(keys)
    block: list[str] = []
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(indent):
            break
        block.append(line.removeprefix(indent))
    return block


def compose_mapping(*keys: str) -> dict[str, str]:
    """A block of ``key: value`` lines as a mapping; a value in double quotes without them."""
    pairs = (line.split(":", 1) for line in compose_block(*keys) if not line.startswith(" "))
    return {key: value.strip().removeprefix('"').removesuffix('"') for key, value in pairs}


def example_lines(path: str) -> dict[str, str]:
    """The ``# MEMINI_NAME=value`` lines of an example file, uncommented as a user would, with a
    double-quoted value read as compose reads it: without the quotes, ``\\n`` a line break."""
    text = (ROOT / path).read_text(encoding="utf-8")
    return {
        name: value[1:-1].replace("\\n", "\n") if value.startswith('"') else value
        for name, value in re.findall(r"(?m)^# (MEMINI_[A-Z0-9_]+)=(.*)$", text)
    }


FILE_VARIABLES = {compose_file_variable(name): name for name in COMPOSE_FILE_SETTINGS}
"""The variables of the secret files in .env, to their settings."""

FIX_COMPOSE = f"update src/memini/config_reference.py or compose.yaml, then {REGENERATE}"


def test_the_compose_variables_are_what_compose_yaml_reads() -> None:
    compose = compose_text()
    interpolated = set(re.findall(r"\$\{(MEMINI_[A-Z0-9_]+)", compose))
    listed = set(COMPOSE_VARIABLES) | set(FILE_VARIABLES)

    assert interpolated == listed, (
        "compose.yaml reads other variables than COMPOSE_VARIABLES and the variables of"
        f" COMPOSE_FILE_SETTINGS: {sorted(interpolated ^ listed)}; {FIX_COMPOSE}"
    )
    for name, (default, _) in COMPOSE_VARIABLES.items():
        assert f"${{{name}:-{default}}}" in compose, f"the default of {name} in compose.yaml"


def test_compose_passes_the_settings_file_into_the_service() -> None:
    assert compose_block("services", "memini", "env_file") == [
        f"- path: ${{MEMINI_ENV_FILE:-{COMPOSE_ENV_FILE}}}",
        "  required: false",
    ]
    assert COMPOSE_VARIABLES["MEMINI_ENV_FILE"][0] == COMPOSE_ENV_FILE


def test_compose_sets_no_setting_but_the_listed_ones() -> None:
    """Every other setting comes from the settings file: a line in `environment` would override
    it there, even when empty."""
    environment = compose_mapping("services", "memini", "environment")

    assert set(environment) == {env_var(name) for name in COMPOSE_SET_SETTINGS} | set(
        FILE_VARIABLES
    ), f"compose.yaml sets other variables in the service than the reference lists; {FIX_COMPOSE}"
    assert environment[env_var("database_url")] == (
        "postgresql://memini:${MEMINI_DB_PASSWORD:-memini}@db:5432/memini"
    )
    for name, value in IMAGE_SETTINGS.items():
        if name in COMPOSE_SET_SETTINGS:
            assert COMPOSE_SET_SETTINGS[name] == value
            assert environment[env_var(name)] == value, f"{env_var(name)} in compose.yaml"


def test_the_settings_file_can_change_the_reranker_the_image_turns_on() -> None:
    """The image sets the reranker's settings; compose leaves them to the settings file."""
    left = {name for name in IMAGE_SETTINGS if name not in COMPOSE_SET_SETTINGS}

    assert left == {"search_reranker", "search_reranker_model_dir"}
    assert left <= set(EXAMPLE_SETTINGS)


def test_every_secret_setting_is_a_file_in_compose() -> None:
    secrets = {name for name in Settings.model_fields if is_secret(name)} - {"database_url"}

    assert secrets <= set(COMPOSE_FILE_SETTINGS), (
        f"mount the secret {sorted(secrets - set(COMPOSE_FILE_SETTINGS))} as a file in compose.yaml"
        f" and add it to COMPOSE_FILE_SETTINGS; {FIX_COMPOSE}"
    )
    read = set(environment_variables())
    for variable, name in FILE_VARIABLES.items():
        assert variable in read, f"the service does not read {variable}"
        assert is_secret(name) or name.endswith("_path"), f"{name} is neither a secret nor a path"


def test_compose_mounts_each_file_and_points_the_service_at_it() -> None:
    environment = compose_mapping("services", "memini", "environment")
    mounted = [line.removeprefix("- ") for line in compose_block("services", "memini", "secrets")]
    declared = compose_block("secrets")
    names = [compose_secret(name) for name in COMPOSE_FILE_SETTINGS]

    assert sorted(mounted) == sorted(names)
    assert sorted(line[:-1] for line in declared if not line.startswith(" ")) == sorted(names)
    for variable, name in FILE_VARIABLES.items():
        secret = compose_secret(name)
        assert environment[variable] == f"${{{variable}:+{COMPOSE_SECRETS_DIR}/{secret}}}"
        assert compose_mapping("secrets", secret) == {
            "file": f"${{{variable}:-{COMPOSE_UNSET_SECRET}}}"
        }


def test_the_file_of_an_unset_secret_is_empty() -> None:
    unset = ROOT / COMPOSE_UNSET_SECRET

    assert unset.is_file()
    assert unset.stat().st_size == 0


def test_the_image_settings_are_what_the_dockerfile_sets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    code = "\n".join(line for line in dockerfile.splitlines() if not line.lstrip().startswith("#"))
    assigned = dict(re.findall(r"\b(MEMINI_[A-Z0-9_]+)=(\S+)", code))

    assert assigned == {env_var(name): value for name, value in IMAGE_SETTINGS.items()}, (
        "the Dockerfile sets other settings than the reference lists: update IMAGE_SETTINGS in"
        f" src/memini/config_reference.py, then {REGENERATE}"
    )


def test_env_example_lists_what_compose_reads() -> None:
    listed = example_lines(".env.example")

    assert set(listed) == set(COMPOSE_VARIABLES) | set(FILE_VARIABLES)
    for name, value in listed.items():
        if name in COMPOSE_VARIABLES and value:
            assert value == COMPOSE_VARIABLES[name][0], f"the default of {name} in .env.example"


EXAMPLE_SETTINGS = [
    name
    for name in Settings.model_fields
    if name not in COMPOSE_SET_SETTINGS
    and name not in COMPOSE_FILE_SETTINGS
    and _EMBEDDING_ONLY.get(name) != "local"
]
"""The settings .env.memini.example lists: all but those compose sets or mounts, and those of the
local embedding provider, which the image cannot run."""


def test_env_memini_example_lists_every_setting_the_file_can_set() -> None:
    listed = set(example_lines(".env.memini.example"))
    expected = {env_var(name) for name in EXAMPLE_SETTINGS}

    assert listed == expected, (
        f".env.memini.example lists other settings than the settings file can set:"
        f" {sorted(listed ^ expected)}; a new setting gets a commented `# MEMINI_NAME=default` line"
    )


def test_env_memini_example_shows_defaults_unless_it_says_otherwise() -> None:
    """Every line uncommented at once is a valid configuration; a line of a setting with a default
    shows that default. Lines of settings without one (the embedding provider and its URL, the LLM
    providers) are examples, and the embedding lines are checked in test_config."""
    listed = example_lines(".env.memini.example")
    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL.get_secret_value()} | listed)

    assert listed[env_var("embedding_url")] == COMPOSE_EMBEDDING_URL
    for name in EXAMPLE_SETTINGS:
        default = Settings.model_fields[name].default
        if name in IMAGE_SETTINGS:  # under compose, the image's value is the default
            assert str(getattr(settings, name)) == IMAGE_SETTINGS[name], env_var(name)
        elif default is not None and name not in computed_defaults():
            assert getattr(settings, name) == default, f"{env_var(name)} in .env.memini.example"


@pytest.mark.parametrize("path", [".env.example", ".env.memini.example"])
def test_every_variable_of_the_examples_is_documented(path: str) -> None:
    example = (ROOT / path).read_text(encoding="utf-8")
    text = committed()

    for name in set(re.findall(r"(?m)^#?\s*(MEMINI_[A-Z0-9_]+)=", example)):
        assert f"`{name}`" in text, f"{name} from {path} is not in docs/configuration.md"
